"""
focal_launcher.py — Focal app launcher

Serves index.html on port 8080, plus the /db, /images and /pending-tasks endpoints.

Usage:
  python3 focal_launcher.py
  then open http://localhost:8080
"""

from flask import Flask, send_from_directory, jsonify, request, Response
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import sqlite3
import threading
import time
import os
import json
import re
import secrets

FOCAL_DIR = Path(__file__).parent

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
LEASE_SECONDS = 60
_leases = {}                    # inbox file name -> (tab id, expiry): who is adding it


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

app = Flask(__name__, static_folder=None)


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


DB_PATH = FOCAL_DIR / 'focal.db'


# ---------------------------------------------------------------------------
# Database read/write endpoints — lets the browser save focal.db without FSAA
# ---------------------------------------------------------------------------
# Versioned saves. Every save is a whole-database export from one tab, so without a check
# the last writer wins: a stale tab, a second device or anything else writing focal.db
# (the inbox flusher) silently overwrites newer data. The version is a hash of the file's
# contents, so any writer changes it. GET /db reports it, POST /db must name the
# version it started from (X-Focal-Base), and a mismatch is a 409. The tab then fetches
# the current file, merges it with its own changes row by row (mergeDB() in index.html)
# and saves again. "force" skips the check; the app sends it only right after the user
# explicitly replaced their data (Open database, Start fresh, CSV import).
#
# A tab loaded before versioning sends no X-Focal-Base and is refused: its saves would
# overwrite whatever changed since it loaded. It shows "save failed" and downloads the
# file instead, which is the cue to reload it.
def _version(data):
    return hashlib.sha256(data).hexdigest()[:16] if data else ''


def _current_version():
    return _version(DB_PATH.read_bytes()) if DB_PATH.exists() else ''


@app.route('/db', methods=['GET'])
def get_db():
    if not DB_PATH.exists():
        return Response('', status=404)
    data = DB_PATH.read_bytes()
    return Response(data, mimetype='application/x-sqlite3',
                    headers={'Content-Disposition': 'inline; filename="focal.db"',
                             'X-Focal-Version': _version(data), 'Cache-Control': 'no-store'})


@app.route('/db', methods=['POST', 'OPTIONS'])
def post_db():
    if request.method == 'OPTIONS':
        return Response('', status=200)
    data = request.get_data()
    if not data:
        return jsonify(ok=False, msg='empty body'), 400
    if not data.startswith(b'SQLite format 3\x00'):
        return jsonify(ok=False, msg='not a SQLite database'), 400
    base = request.headers.get('X-Focal-Base')
    if base is None:
        print('[launcher] refused a save from a tab without versioning (reload it)')
        return jsonify(ok=False, conflict=True, msg='reload this tab'), 409
    # Atomic write — a crash mid-write must not corrupt the live DB. The lock makes the
    # version check and the write one step, and keeps this from racing the inbox flusher.
    with _db_lock:
        current = _current_version()
        if base != 'force' and base != current:
            return jsonify(ok=False, conflict=True, version=current), 409
        tmp = DB_PATH.with_suffix('.db.tmp')
        tmp.write_bytes(data)
        os.replace(tmp, DB_PATH)
    print(f'[launcher] focal.db saved ({len(data):,} bytes)' + (' [forced]' if base == 'force' else ''))
    return jsonify(ok=True, size=len(data), version=_version(data))


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
    this is being polled, the launcher leaves focal.db to Focal and never flushes itself.
    db_version lets an open tab notice that focal.db changed under it and pull it in.

    A tab from before versioned saves sends no X-Focal-Client. It gets an empty list and
    doesn't count as alive: its saves are refused (see POST /db), so a task it drained
    would be acked without ever reaching focal.db. The flusher or a current tab takes it."""
    if request.headers.get('X-Focal-Client') is None:
        return jsonify(tasks=[])
    global _last_pending_poll
    _last_pending_poll = now = time.time()
    # Each task to add goes to one tab at a time. Two tabs draining the same file would
    # both add it, and since saves now merge instead of overwrite, both copies would
    # stay. The lease lapses if that tab never acks (closed mid-drain, save failed).
    tab = request.headers.get('X-Focal-Tab', '')
    tasks = []
    for f, d in _read_inbox():
        if not d.get('review'):
            holder, until = _leases.get(f.name, ('', 0))
            if holder and holder != tab and until > now:
                continue
            _leases[f.name] = (tab, now + LEASE_SECONDS)
        item = dict(d)
        item['queue_id'] = f.name        # Focal echoes this back to ack (delete) the file
        tasks.append(item)
    return jsonify(tasks=tasks, db_version=_current_version())


_DUE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_PRIORITY_WORDS = {'critical': 1, 'urgent': 1, 'high': 2, 'medium': 3, 'normal': 3, 'low': 4}
_RECUR_RE = re.compile(r'^(daily|weekly|monthly|every:[1-9]\d{0,2})$')


@app.route('/pending-tasks', methods=['POST'])
def queue_task_for_review():
    """Queue a task captured outside Focal (e.g. the "Add to Focal" macOS Shortcut) for
    review. It lands in the same inbox as agent-dropped tasks, but flagged
    review=true: Focal lists it under Review instead of adding it, and the closed-app
    flusher leaves it alone, so nothing reaches the task list until it's approved.
    Fields are coerced rather than rejected — they usually come from a language model.

    "review": false (the focal CLI's default) skips the review and queues an ordinary
    inbox task instead, exactly like a file dropped in .focal_inbox: the open app adds it
    on its next drain, or the flusher writes it to focal.db if the app is closed."""
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
                 if ln.strip() and not ln.startswith(('Today:', 'Coming days:', 'Source:'))]
        if lines:
            description = (description + '\n\n' + '\n'.join(lines)).strip()
        source = source or ('Mail' if 'message://' in head else 'Selection')
    recur = s('recur', 12)
    proj = d.get('projectId')
    proj = int(proj) if isinstance(proj, (int, str)) and str(proj).isdigit() else None
    review = d.get('review') is not False
    item = {
        'title': title, 'description': description, 'notes': s('notes', 20000),
        'due': due, 'priority': pr, 'category': s('category', 80), 'source': source,
        'recur': recur if _RECUR_RE.match(recur) else '', 'projectId': proj,
        'teamFlag': bool(d.get('teamFlag')), 'review': review, 'created': _now_iso(),
    }
    INBOX_DIR.mkdir(exist_ok=True)
    name = f"{'review' if review else 'task'}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.json"
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
        _leases.pop(name, None)
        try:
            if p.exists():
                p.unlink()
                removed += 1
        except Exception:
            pass
    return jsonify(ok=True, removed=removed)


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
                            ' created,notes,projectId,teamFlag) '
                            "VALUES (?,?,?,?,?,?,0,'[]',?,?,?,?)",
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


if __name__ == '__main__':
    print('─' * 50)
    print('  Focal launcher')
    print('  App  →  http://localhost:8080')
    print('─' * 50)
    app.run(port=8080, debug=False, use_reloader=False)
