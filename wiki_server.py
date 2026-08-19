from flask import Flask, request, jsonify, Response
from pathlib import Path
import re
import json
import threading
import time
import os
import signal

# Root folder of your markdown wiki. Override with the FOCAL_WIKI_ROOT env var.
# Pages live in <root>/wiki/*.md; daily summaries written by Focal land in
# <root>/raw/focal/.
WIKI_ROOT = Path(os.environ.get('FOCAL_WIKI_ROOT', '~/Documents/focal-wiki')).expanduser()
WIKI_DIR = WIKI_ROOT / 'wiki'
RAW_FOCAL_DIR = WIKI_ROOT / 'raw' / 'focal'
app = Flask(__name__)

# ---------------------------------------------------------------------------
# Idle timeout — shut down after this many minutes of no requests
# Set WIKI_IDLE_MINUTES=0 to disable
# ---------------------------------------------------------------------------
IDLE_MINUTES = int(os.environ.get('WIKI_IDLE_MINUTES', '30'))
_last_request_time = time.time()


@app.before_request
def _touch():
    global _last_request_time
    _last_request_time = time.time()


def _idle_watcher():
    if IDLE_MINUTES <= 0:
        return
    print(f'[wiki] Idle timeout active: will shut down after {IDLE_MINUTES} min of inactivity')
    while True:
        time.sleep(60)
        idle_for = (time.time() - _last_request_time) / 60
        if idle_for >= IDLE_MINUTES:
            print(f'[wiki] Idle for {idle_for:.0f} min — shutting down')
            os.kill(os.getpid(), signal.SIGTERM)


threading.Thread(target=_idle_watcher, daemon=True).start()


# Only the Focal app may call this server — a wildcard would let any website
# you visit read the work wiki via fetch() to localhost.
ALLOWED_ORIGINS = {'http://localhost:8080', 'http://127.0.0.1:8080'}


@app.before_request
def check_origin():
    origin = request.headers.get('Origin')
    if origin and origin not in ALLOWED_ORIGINS:
        return Response('Forbidden origin', status=403)


@app.after_request
def add_cors(r):
    origin = request.headers.get('Origin')
    if origin in ALLOWED_ORIGINS:
        r.headers['Access-Control-Allow-Origin'] = origin
        r.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        r.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return r


@app.route('/health')
def health():
    return jsonify(ok=True)


@app.route('/pages')
def pages():
    if not WIKI_DIR.exists():
        return jsonify([])
    return jsonify([f.stem for f in sorted(WIKI_DIR.glob('*.md'))])


@app.route('/page')
def page():
    name = request.args.get('name', '')
    if '/' in name or '\\' in name or '..' in name:
        return Response('Not found', status=404, mimetype='text/plain')
    path = WIKI_DIR / f"{name}.md"
    if not path.exists():
        return Response('Not found', status=404, mimetype='text/plain')
    return Response(path.read_text('utf-8'), mimetype='text/plain')


@app.route('/search')
def search():
    q = request.args.get('q', '')
    if not q or not WIKI_DIR.exists():
        return jsonify([])
    pat = re.compile(re.escape(q), re.IGNORECASE)
    results = []
    for path in sorted(WIKI_DIR.glob('*.md')):
        text = path.read_text('utf-8')
        m = pat.search(text)
        if m:
            s = max(0, m.start() - 100)
            e = min(len(text), m.end() + 100)
            excerpt = ('…' if s else '') + text[s:e].strip() + ('…' if e < len(text) else '')
            results.append({'page': path.stem, 'excerpt': excerpt})
    return jsonify(results)


@app.route('/write-focal', methods=['GET', 'POST', 'OPTIONS'])
def write_focal():
    """
    Accepts the markdown content as a plain-text POST body.
    Filename is passed as a query parameter: /write-focal?filename=2025-05-20-focal-summary.md
    Using text/plain avoids the CORS preflight OPTIONS round-trip entirely.
    """
    if request.method == 'OPTIONS':
        return Response('', status=200)

    filename = request.args.get('filename', '').strip()
    if not filename:
        return Response(json.dumps({'error': 'filename query param required'}),
                        status=400, mimetype='application/json')

    # Accept both plain-text body (preferred, no preflight) and JSON body (fallback)
    if request.content_type and 'application/json' in request.content_type:
        data = request.get_json(force=True, silent=True) or {}
        content = data.get('content', '')
        # Allow filename to come from JSON body too
        if not filename:
            filename = data.get('filename', '').strip()
    else:
        content = request.get_data(as_text=True)

    if not filename:
        return Response(json.dumps({'error': 'filename required'}),
                        status=400, mimetype='application/json')

    # Sanitize filename
    safe = re.sub(r'[^\w\-.]', '-', filename)
    if not safe.endswith('.md'):
        safe += '.md'

    RAW_FOCAL_DIR.mkdir(parents=True, exist_ok=True)
    dest = RAW_FOCAL_DIR / safe
    dest.write_text(content, encoding='utf-8')

    return Response(json.dumps({'ok': True, 'filename': safe, 'path': str(dest)}),
                    status=200, mimetype='application/json')


@app.route('/focal-files')
def focal_files():
    """List previously uploaded focal summaries."""
    if not RAW_FOCAL_DIR.exists():
        return jsonify([])
    files = sorted(RAW_FOCAL_DIR.glob('*.md'), reverse=True)
    return jsonify([{'name': f.name, 'size': f.stat().st_size,
                     'modified': f.stat().st_mtime} for f in files])


if __name__ == '__main__':
    print('Wiki server running at http://localhost:8765')
    app.run(port=8765, debug=False)
