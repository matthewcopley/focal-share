"""
focal_launcher.py — Focal app launcher

Runs two things:
  • Static file server on port 8080 (serves index.html and any other local files)
  • Manager for wiki_server.py (start/stop via /api/* endpoints)

Usage:
  python3 focal_launcher.py
  then open http://localhost:8080
"""

from flask import Flask, send_from_directory, jsonify, request, Response
from pathlib import Path
from datetime import datetime, timezone
import sqlite3
import subprocess
import threading
import time
import os
import signal
import sys
import json
import urllib.request
import urllib.parse

FOCAL_DIR = Path(__file__).parent
WIKI_SCRIPT = FOCAL_DIR / 'wiki_server.py'

# ---------------------------------------------------------------------------
# Task handoff inbox (external agents/scripts → Focal)
#
# External tools cannot write focal.db directly: Focal is a sql.js app whose
# autosave does DELETE FROM tasks + re-insert from its in-memory array, so any direct
# INSERT is clobbered on Focal's next save. Instead the agent drops a JSON file here; the
# open Focal app drains it via GET /pending-tasks and POST /pending-tasks/ack (assigning
# the real task id in its own model). If Focal is NOT running (no drain poll within
# INBOX_FLUSH_GRACE seconds), the launcher itself flushes the queue into focal.db — safe,
# because nothing is live to clobber it. _db_lock serializes launcher writes against the
# browser's POST /db full-DB saves.
# ---------------------------------------------------------------------------
INBOX_DIR = FOCAL_DIR / '.focal_inbox'
INBOX_FLUSH_GRACE = 90          # seconds with no Focal poll before the launcher writes to disk
_db_lock = threading.Lock()
_last_pending_poll = 0.0        # updated on every GET /pending-tasks (Focal liveness signal)


def _read_inbox():
    """Return [(Path, dict)] for each queued task file, oldest first. Partial/unreadable
    files are skipped (a concurrent .tmp is never matched by the *.json glob)."""
    if not INBOX_DIR.exists():
        return []
    items = []
    for f in sorted(INBOX_DIR.glob('*.json')):
        try:
            items.append((f, json.loads(f.read_text(encoding='utf-8'))))
        except Exception:
            pass
    return items

# ---------------------------------------------------------------------------
# Load .env from the Focal folder for AWS credentials (only fills gaps —
# existing env vars always take precedence). See .env.example.
# ---------------------------------------------------------------------------
def _load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v

_load_dotenv(FOCAL_DIR / '.env')

app = Flask(__name__, static_folder=None)

_wiki_proc = None
_wiki_lock = threading.Lock()


# ---------------------------------------------------------------------------
# CORS / Origin check
# Only the Focal app itself may call this server. A wildcard here would let
# any website you visit read or overwrite focal.db via fetch() to localhost.
# ---------------------------------------------------------------------------
ALLOWED_ORIGINS = {'http://localhost:8080', 'http://127.0.0.1:8080'}


@app.before_request
def check_origin():
    origin = request.headers.get('Origin')
    # Same-origin GETs and non-browser clients send no Origin header
    if origin and origin not in ALLOWED_ORIGINS:
        return Response('Forbidden origin', status=403)


@app.after_request
def add_cors(r):
    origin = request.headers.get('Origin')
    if origin in ALLOWED_ORIGINS:
        r.headers['Access-Control-Allow-Origin'] = origin
        r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        r.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return r


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return send_from_directory(FOCAL_DIR, 'index.html')


# Whitelist — the project folder also holds focal.db, .git and personal
# exports, none of which should be reachable over HTTP.
ALLOWED_STATIC = {'index.html', 'favicon.ico'}


@app.route('/<path:filename>')
def static_file(filename):
    if filename not in ALLOWED_STATIC:
        return Response('Not found', status=404)
    return send_from_directory(FOCAL_DIR, filename)


# ---------------------------------------------------------------------------
# Wiki server management API
# ---------------------------------------------------------------------------
DB_PATH = FOCAL_DIR / 'focal.db'


def _wiki_running():
    return _wiki_proc is not None and _wiki_proc.poll() is None


# ---------------------------------------------------------------------------
# Database read/write endpoints — lets the browser save focal.db without FSAA
# ---------------------------------------------------------------------------
@app.route('/db', methods=['GET'])
def get_db():
    if not DB_PATH.exists():
        return Response('', status=404)
    data = DB_PATH.read_bytes()
    return Response(data, mimetype='application/x-sqlite3',
                    headers={'Content-Disposition': 'inline; filename="focal.db"'})


@app.route('/db', methods=['POST', 'OPTIONS'])
def post_db():
    if request.method == 'OPTIONS':
        return Response('', status=200)
    data = request.get_data()
    if not data:
        return jsonify(ok=False, msg='empty body'), 400
    if not data.startswith(b'SQLite format 3\x00'):
        return jsonify(ok=False, msg='not a SQLite database'), 400
    # Atomic write — a crash mid-write must not corrupt the live DB. The lock keeps this
    # from racing the inbox flusher, which may also write focal.db.
    with _db_lock:
        tmp = DB_PATH.with_suffix('.db.tmp')
        tmp.write_bytes(data)
        os.replace(tmp, DB_PATH)
    print(f'[launcher] focal.db saved ({len(data):,} bytes)')
    return jsonify(ok=True, size=len(data))


# ---------------------------------------------------------------------------
# Task handoff endpoints — the open Focal app polls these to pull queued tasks.
# ---------------------------------------------------------------------------
@app.route('/pending-tasks', methods=['GET'])
def pending_tasks():
    """Return queued tasks for Focal to drain. Also the Focal-liveness heartbeat: while
    this is being polled, the launcher leaves focal.db to Focal and never flushes itself."""
    global _last_pending_poll
    _last_pending_poll = time.time()
    tasks = []
    for f, d in _read_inbox():
        item = dict(d)
        item['queue_id'] = f.name        # Focal echoes this back to ack (delete) the file
        tasks.append(item)
    return jsonify(tasks=tasks)


@app.route('/pending-tasks/ack', methods=['POST', 'OPTIONS'])
def ack_pending():
    """Focal calls this AFTER it has persisted the drained tasks, to delete their inbox
    files. Deleting only post-persist means a crash mid-drain just re-queues them."""
    if request.method == 'OPTIONS':
        return Response('', status=200)
    ids = (request.get_json(force=True, silent=True) or {}).get('ids', [])
    removed = 0
    for name in ids:
        # Never let a client-supplied name escape the inbox dir.
        if not isinstance(name, str) or '/' in name or '\\' in name or not name.endswith('.json'):
            continue
        p = INBOX_DIR / name
        try:
            if p.exists():
                p.unlink()
                removed += 1
        except Exception:
            pass
    return jsonify(ok=True, removed=removed)


@app.route('/api/wiki-status')
def wiki_status():
    return jsonify(running=_wiki_running())


@app.route('/api/start-wiki', methods=['POST', 'OPTIONS'])
def start_wiki():
    if request.method == 'OPTIONS':
        return Response('', status=200)

    global _wiki_proc
    with _wiki_lock:
        if _wiki_running():
            return jsonify(ok=True, msg='already running')
        if not WIKI_SCRIPT.exists():
            return jsonify(ok=False, msg=f'wiki_server.py not found at {WIKI_SCRIPT}'), 404

        try:
            _wiki_proc = subprocess.Popen(
                [sys.executable, str(WIKI_SCRIPT)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            # Give it a moment to bind to port 8765
            time.sleep(0.8)
            if _wiki_proc.poll() is not None:
                stderr = _wiki_proc.stderr.read().decode(errors='replace')
                return jsonify(ok=False, msg=f'wiki server exited immediately: {stderr}'), 500
            print(f'[launcher] Wiki server started (pid {_wiki_proc.pid})')
            return jsonify(ok=True, msg='started', pid=_wiki_proc.pid)
        except Exception as e:
            return jsonify(ok=False, msg=str(e)), 500


@app.route('/api/stop-wiki', methods=['POST', 'OPTIONS'])
def stop_wiki():
    if request.method == 'OPTIONS':
        return Response('', status=200)

    global _wiki_proc
    with _wiki_lock:
        if not _wiki_running():
            return jsonify(ok=True, msg='not running')
        try:
            _wiki_proc.terminate()
            _wiki_proc.wait(timeout=5)
        except Exception:
            _wiki_proc.kill()
        pid = _wiki_proc.pid
        _wiki_proc = None
        print(f'[launcher] Wiki server stopped (pid {pid})')
        return jsonify(ok=True, msg='stopped')


# ---------------------------------------------------------------------------
# Reap zombie wiki process if it dies on its own (e.g. idle timeout)
# ---------------------------------------------------------------------------
def _reaper():
    global _wiki_proc
    while True:
        time.sleep(10)
        with _wiki_lock:
            if _wiki_proc is not None and _wiki_proc.poll() is not None:
                print(f'[launcher] Wiki server exited on its own (pid {_wiki_proc.pid})')
                _wiki_proc = None


threading.Thread(target=_reaper, daemon=True).start()


# ---------------------------------------------------------------------------
# Inbox flusher — write queued tasks straight to focal.db when Focal is not running.
# Only fires after INBOX_FLUSH_GRACE seconds with no /pending-tasks poll, so an open
# Focal (which drains and acks) is never second-guessed. When Focal is closed there is no
# in-memory model to clobber the write, and Focal will load these rows next time it opens.
# ---------------------------------------------------------------------------
def _now_iso():
    now = datetime.now(timezone.utc)
    return now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'


def _flush_inbox_to_disk():
    while True:
        time.sleep(15)
        try:
            items = _read_inbox()
            if not items:
                continue
            if time.time() - _last_pending_poll < INBOX_FLUSH_GRACE:
                continue                     # Focal is alive and draining — leave it be
            if not DB_PATH.exists():
                continue
            with _db_lock:
                con = sqlite3.connect(str(DB_PATH))
                try:
                    # Case-insensitive map of existing category names, so an inbox
                    # task tagged "billing" lands in "Billing" rather than forking a
                    # phantom duplicate. Mirrors normalizeCategory() in index.html.
                    cats = {}
                    try:
                        for (nm,) in con.execute('SELECT name FROM categories'):
                            if nm:
                                cats[str(nm).strip().lower()] = str(nm)
                    except Exception:
                        pass
                    for f, d in items:
                        title = str(d.get('title', '')).strip()
                        if not title:
                            try: f.unlink()
                            except Exception: pass
                            continue
                        try:
                            pr = int(d.get('priority', 3))
                        except (TypeError, ValueError):
                            pr = 3
                        if pr < 1 or pr > 4:
                            pr = 3
                        proj = d.get('projectId')
                        proj = int(proj) if isinstance(proj, (int, str)) and str(proj).lstrip('-').isdigit() else None
                        cat = str(d.get('category', '')).strip()
                        cat = cats.get(cat.lower(), cat)
                        con.execute(
                            'INSERT INTO tasks '
                            '(title,priority,category,due,recur,description,done,subtasks,'
                            ' created,notes,projectId,teamFlag,wikiPage) '
                            "VALUES (?,?,?,?,?,?,0,'[]',?,?,?,?,'')",
                            [title, pr, cat, str(d.get('due', '')),
                             str(d.get('recur', '')), str(d.get('description', '')),
                             str(d.get('created') or _now_iso()), str(d.get('notes', '')),
                             proj, 1 if d.get('teamFlag') else 0],
                        )
                        con.commit()
                        try: f.unlink()
                        except Exception: pass
                    print(f'[launcher] flushed {len(items)} queued task(s) to focal.db (Focal not running)')
                finally:
                    con.close()
        except Exception as e:
            print(f'[launcher] inbox flush error: {e}')


try:
    INBOX_DIR.mkdir(exist_ok=True)
except Exception as e:
    print(f'[launcher] could not create inbox dir: {e}')
threading.Thread(target=_flush_inbox_to_disk, daemon=True).start()


# ---------------------------------------------------------------------------
# Graceful shutdown: kill wiki server when launcher exits
# ---------------------------------------------------------------------------
def _on_exit(signum=None, frame=None):
    global _wiki_proc
    with _wiki_lock:
        if _wiki_running():
            print(f'[launcher] Shutting down wiki server (pid {_wiki_proc.pid})…')
            _wiki_proc.terminate()
    sys.exit(0)


signal.signal(signal.SIGINT, _on_exit)
signal.signal(signal.SIGTERM, _on_exit)


# ---------------------------------------------------------------------------
# Bedrock AI proxy
# The browser can't sign AWS requests, so we proxy through the launcher.
# Handles the full converse + wiki tool-call loop server-side.
# ---------------------------------------------------------------------------
WIKI_BASE = 'http://localhost:8765'
BEDROCK_WIKI_TOOLS = [
    {
        'toolSpec': {
            'name': 'get_page',
            'description': (
                'Read a full wiki page by name. Use search_wiki first if you are '
                'unsure which pages exist.'
            ),
            'inputSchema': {
                'json': {
                    'type': 'object',
                    'properties': {'name': {'type': 'string', 'description': 'Page filename without the .md extension'}},
                    'required': ['name'],
                }
            },
        }
    },
    {
        'toolSpec': {
            'name': 'search_wiki',
            'description': 'Search all wiki pages for a keyword or phrase. Returns matching excerpts.',
            'inputSchema': {
                'json': {
                    'type': 'object',
                    'properties': {'query': {'type': 'string', 'description': 'Search term'}},
                    'required': ['query'],
                }
            },
        }
    },
]


def _wiki_fetch(tool_name: str, args: dict) -> str:
    """Call the local wiki server from the launcher process."""
    try:
        if tool_name == 'get_page':
            url = f"{WIKI_BASE}/page?name={urllib.parse.quote(args.get('name', ''))}"
        elif tool_name == 'search_wiki':
            url = f"{WIKI_BASE}/search?q={urllib.parse.quote(args.get('query', ''))}"
        else:
            return 'Unknown tool'
        with urllib.request.urlopen(url, timeout=8) as resp:
            return resp.read().decode('utf-8')
    except Exception as e:
        return f'Wiki lookup failed: {e}'


def _openai_to_bedrock(messages: list):
    """Convert OpenAI-style message list to Bedrock Converse (system, messages)."""
    system = []
    bedrock_msgs = []
    for m in messages:
        role = m.get('role', '')
        content = m.get('content') or ''
        if role == 'system':
            if content:
                system.append({'text': content})
        elif role == 'tool':
            bedrock_msgs.append({
                'role': 'user',
                'content': [{
                    'toolResult': {
                        'toolUseId': m.get('tool_call_id', 'unknown'),
                        'content': [{'text': str(content)}],
                    }
                }],
            })
        elif role == 'assistant' and m.get('tool_calls'):
            blocks = []
            if content:
                blocks.append({'text': content})
            for tc in m['tool_calls']:
                blocks.append({
                    'toolUse': {
                        'toolUseId': tc['id'],
                        'name': tc['function']['name'],
                        'input': json.loads(tc['function'].get('arguments') or '{}'),
                    }
                })
            bedrock_msgs.append({'role': 'assistant', 'content': blocks})
        else:
            if content:
                bedrock_msgs.append({'role': role, 'content': [{'text': content}]})
    return system, bedrock_msgs


@app.route('/api/ai/bedrock/status')
def bedrock_status():
    configured = bool(os.getenv('AWS_ACCESS_KEY_ID'))
    return jsonify({
        'configured': configured,
        'region': os.getenv('AWS_REGION', 'us-east-1'),
        'model': os.getenv('BEDROCK_MODEL_ID', ''),
    })


@app.route('/api/ai/bedrock', methods=['POST', 'OPTIONS'])
def bedrock_proxy():
    if request.method == 'OPTIONS':
        return Response('', status=200)
    try:
        import boto3
    except ImportError:
        return jsonify({'error': 'boto3 not installed — run: pip install boto3'}), 500

    try:
        data = request.get_json(force=True)
        messages     = list(data.get('messages', []))
        max_tokens   = int(data.get('max_tokens', 600))
        temperature  = float(data.get('temperature', 0.3))
        wiki_online  = bool(data.get('wiki_online', False))
        region       = os.getenv('AWS_REGION', 'us-east-1')
        model_id     = os.getenv('BEDROCK_MODEL_ID', '')
        if not model_id:
            return jsonify({'error': 'BEDROCK_MODEL_ID not set — add it to the .env file in the Focal folder'}), 500

        bedrock = boto3.client('bedrock-runtime', region_name=region)
        tools = BEDROCK_WIKI_TOOLS if wiki_online else None

        for _ in range(5):
            system, bedrock_msgs = _openai_to_bedrock(messages)
            kwargs = {
                'modelId':         model_id,
                'messages':        bedrock_msgs,
                'inferenceConfig': {'maxTokens': max_tokens, 'temperature': temperature},
            }
            if system:
                kwargs['system'] = system
            if tools:
                kwargs['toolConfig'] = {'tools': tools}

            resp = bedrock.converse(**kwargs)
            stop_reason = resp.get('stopReason', 'end_turn')
            out_msg     = resp['output']['message']

            if stop_reason == 'tool_use':
                # Collect tool-use blocks and append assistant turn
                tool_calls_oa = []
                text_so_far   = ''
                for block in out_msg.get('content', []):
                    if 'text' in block:
                        text_so_far = block['text']
                    elif 'toolUse' in block:
                        tu = block['toolUse']
                        tool_calls_oa.append({
                            'id':       tu['toolUseId'],
                            'type':     'function',
                            'function': {'name': tu['name'], 'arguments': json.dumps(tu['input'])},
                        })

                messages.append({'role': 'assistant', 'content': text_so_far, 'tool_calls': tool_calls_oa})

                # Execute each tool and append results
                for tc in tool_calls_oa:
                    args   = json.loads(tc['function'].get('arguments') or '{}')
                    result = _wiki_fetch(tc['function']['name'], args)
                    messages.append({'role': 'tool', 'tool_call_id': tc['id'], 'content': result})

            else:
                # Final answer — collect all text blocks
                text = ''.join(b.get('text', '') for b in out_msg.get('content', []))
                return jsonify({'text': text.strip()})

        return jsonify({'error': 'Tool call loop exceeded max rounds'}), 500

    except Exception as exc:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(exc)}), 500


if __name__ == '__main__':
    print('─' * 50)
    print('  Focal launcher')
    print('  App  →  http://localhost:8080')
    print('  Wiki →  http://localhost:8765  (start from app UI)')
    print('─' * 50)
    app.run(port=8080, debug=False, use_reloader=False)
