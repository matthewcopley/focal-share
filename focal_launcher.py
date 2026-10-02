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
import re
import secrets

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
#
# POST /pending-tasks adds a second kind of item, flagged review=true (the "Add to Focal"
# macOS Shortcut). Those wait in the inbox until approved in Focal's Review view: Focal
# doesn't drain them and the flusher below skips them.
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
# Load .env from the Focal folder for optional settings like FOCAL_WIKI_ROOT
# (only fills gaps — existing env vars always take precedence). See .env.example.
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
# Pasted images
#
# Screenshots live as files in focal_images/, never in focal.db: a save is a full
# export of the whole database over POST /db every couple of seconds, so a few MB of
# base64 in a task's notes would be rewritten on every keystroke-debounce. The notes
# text holds only an [img:NAME] token; the browser resolves it to GET /images/NAME.
#
# Nothing here trusts the client's filename — the name is generated on this side, and
# reads are constrained to a single flat directory by IMG_NAME_RE.
# ---------------------------------------------------------------------------
IMG_DIR = FOCAL_DIR / 'focal_images'
IMG_MAX_BYTES = 12 * 1024 * 1024
IMG_NAME_RE = re.compile(r'^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}\.(png|jpg|gif|webp)$')

# Content-Type → (extension, magic bytes). The extension decides what mimetype the file
# is later served as, so an image that lies about its type is still served as an image.
IMG_TYPES = {
    'image/png':  ('.png', b'\x89PNG'),
    'image/jpeg': ('.jpg', b'\xff\xd8\xff'),
    'image/gif':  ('.gif', b'GIF8'),
    'image/webp': ('.webp', b'RIFF'),
}


@app.route('/images', methods=['POST', 'OPTIONS'])
def post_image():
    if request.method == 'OPTIONS':
        return Response('', status=200)
    ctype = (request.content_type or '').split(';')[0].strip().lower()
    if ctype not in IMG_TYPES:
        return jsonify(ok=False, msg=f'unsupported image type: {ctype}'), 400
    ext, magic = IMG_TYPES[ctype]
    data = request.get_data()
    if not data:
        return jsonify(ok=False, msg='empty body'), 400
    if len(data) > IMG_MAX_BYTES:
        return jsonify(ok=False, msg='image too large'), 413
    if not data.startswith(magic):
        return jsonify(ok=False, msg='body does not look like ' + ctype), 400
    IMG_DIR.mkdir(exist_ok=True)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}{ext}"
    tmp = IMG_DIR / (name + '.tmp')
    tmp.write_bytes(data)
    os.replace(tmp, IMG_DIR / name)
    print(f'[launcher] image saved: {name} ({len(data):,} bytes)')
    return jsonify(ok=True, name=name, size=len(data))


@app.route('/images/<name>')
def get_image(name):
    if not IMG_NAME_RE.match(name) or not (IMG_DIR / name).is_file():
        return Response('Not found', status=404)
    # Names are unique per upload and files are never rewritten, so they cache forever.
    return send_from_directory(IMG_DIR, name, max_age=31536000)


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


_DUE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_PRIORITY_WORDS = {'critical': 1, 'urgent': 1, 'high': 2, 'medium': 3, 'normal': 3, 'low': 4}


@app.route('/pending-tasks', methods=['POST'])
def queue_task_for_review():
    """Queue a task captured outside Focal (e.g. the "Add to Focal" macOS Shortcut) for
    review. It lands in the same inbox as agent-dropped tasks, but flagged
    review=true: Focal lists it under Review instead of adding it, and the closed-app
    flusher leaves it alone, so nothing reaches the task list until it's approved.
    Fields are coerced rather than rejected — they usually come from a language model."""
    d = request.get_json(force=True, silent=True)
    if not isinstance(d, dict):
        return jsonify(ok=False, error='expected a JSON object'), 400

    def s(key, cap):
        v = d.get(key)
        return '' if v is None else str(v).strip()[:cap]

    title = s('title', 300)
    if not title:
        return jsonify(ok=False, error='title is required'), 400
    due = s('due', 10)
    if not _DUE_RE.match(due):
        due = ''
    pr = d.get('priority')
    try:
        pr = int(pr)
    except (TypeError, ValueError):
        pr = _PRIORITY_WORDS.get(str(pr or '').strip().lower(), 3)
    if pr < 1 or pr > 4:
        pr = 3
    # `email` is the shortcut's raw capture: header lines (From / Subject / message:// link),
    # then '@@BODY@@', then the text the model read. The header goes under the description
    # so the task links back to the email; the shortcut stays four actions with no text
    # plumbing of its own.
    description, source = s('description', 20000), s('source', 40)
    head, sep, _ = s('email', 60000).partition('@@BODY@@')
    if sep:
        lines = [ln.strip() for ln in head.splitlines()
                 if ln.strip() and not ln.startswith(('Today:', 'Source:'))]
        if lines:
            description = (description + '\n\n' + '\n'.join(lines)).strip()
        source = source or ('Mail' if 'message://' in head else 'Selection')
    item = {
        'title': title, 'description': description, 'notes': s('notes', 20000),
        'due': due, 'priority': pr, 'category': s('category', 80), 'source': source,
        'review': True, 'created': _now_iso(),
    }
    INBOX_DIR.mkdir(exist_ok=True)
    name = f"review-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.json"
    tmp = INBOX_DIR / ('.' + name + '.tmp')     # never matched by the *.json glob
    tmp.write_text(json.dumps(item), encoding='utf-8')
    os.replace(tmp, INBOX_DIR / name)
    return jsonify(ok=True, queue_id=name), 201


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
            # Review items wait for a person to approve them in Focal, open or not.
            items = [(f, d) for f, d in _read_inbox() if not d.get('review')]
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


if __name__ == '__main__':
    print('─' * 50)
    print('  Focal launcher')
    print('  App  →  http://localhost:8080')
    print('  Wiki →  http://localhost:8765  (start from app UI)')
    print('─' * 50)
    app.run(port=8080, debug=False, use_reloader=False)
