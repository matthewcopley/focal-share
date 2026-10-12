#!/usr/bin/env python3
"""focal — read and change Focal from the command line (for people, scripts and agents).

Everything goes through the launcher over HTTP, so it works the same on the machine
running Focal and on any other that can reach it:

    FOCAL_URL   launcher base URL (default http://localhost:8080)

Reads fetch GET /db into a temp file and query that copy. The open app autosaves within
~2s of a change, so a read is never more than a moment behind. Nothing here ever writes
focal.db: the open app's autosave rewrites the whole database from its own memory and
would overwrite any direct write. Adds go through POST /pending-tasks, the same inbox the
Add to Focal shortcut uses, so the app (or the launcher, if no tab is open) does the
insert.

    focal                      top 5 by Focus ranking
    focal ls today|overdue|upcoming|all|done|recent|snoozed|sticky|team
    focal show 589             one task in full (notes, subtasks, links, history)
    focal search invoice       title / description / notes / next step
    focal projects | project 3 | ideas | cats
    focal add "Send W-9" --due fri --cat Admin -p high
    focal done 589             also reopen, snooze 589 fri, unsnooze, note 589 "text",
    focal edit 589 -d +2d -p 1      sub add 589 "text", sub done|reopen 589 2
    focal undo 589             reopen, and take back the repeat it spawned if untouched
    focal launch 589 --started ISO --ended ISO --planned 5 --outcome done [--step "..."]
    focal cat add Billing | cat rename Billing "Media Billing"
    focal proj add "New app" --type internal | proj done 3 | proj reopen 3
    focal pref get [KEY] | pref set KEY VALUE (- = stdin) | pref unset KEY

Adds go through the inbox. Every other change (to tasks, categories, projects, prefs,
Launch sessions) is queued with the launcher and applied by an open Focal tab, in the
order queued, using the app's own logic, and the CLI waits for the result. Exit 4 means
queued but not applied yet (no tab open, or a background tab that hasn't checked in).

Every read takes --json, and so does every change (op_id, applied, and the app's result).
`--db FILE` reads a local copy instead, including a backup snapshot (.db.gz). Exit codes:
0 ok (or already so), 1 can't reach Focal, 2 bad input or refused, 3 duplicate (an open
task or project with that title, a Launch session already logged), 4 queued but not
applied yet.

Deliberately not called: GET /pending-tasks. The launcher treats every call as the open
app's heartbeat, and a CLI polling it would stop the closed-app flusher from running.
"""
import argparse
import atexit
import gzip
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

URL = os.environ.get('FOCAL_URL', 'http://localhost:8080').rstrip('/')

# Mirrors index.html: STICKY_* constants, upcoming = next 14 days, Focus shows 5.
STICKY_PUSHES, STICKY_OVERDUE, STICKY_AGE = 2, 3, 21
UPCOMING_DAYS, FOCUS_TOP = 14, 5
PRIORITY_WORDS = {'critical': 1, 'urgent': 1, 'high': 2, 'medium': 3, 'normal': 3, 'low': 4}
WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']


def die(msg, code=2):
    print('focal: ' + msg, file=sys.stderr)
    sys.exit(code)


# ── Loading ──────────────────────────────────────────────────────────────────

def http(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(URL + path, data=data, method=method,
                                 headers={'Content-Type': 'application/json'} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors='replace')[:300]
        try:
            detail = json.loads(detail).get('error') or detail
        except ValueError:
            pass
        die(f'{method} {path} → {e.code}: {detail}', 1)
    except (urllib.error.URLError, OSError) as e:
        die(f"can't reach Focal at {URL} ({getattr(e, 'reason', e)}). Set FOCAL_URL?", 1)


def open_db(path):
    if path:
        raw = open(os.path.expanduser(path), 'rb').read()
        if path.endswith('.gz'):
            raw = gzip.decompress(raw)
    else:
        raw = http('GET', '/db')
    if not raw.startswith(b'SQLite format 3\x00'):
        die('that is not a Focal database', 1)
    fd, tmp = tempfile.mkstemp(suffix='.db')
    os.write(fd, raw)
    os.close(fd)
    atexit.register(os.unlink, tmp)
    con = sqlite3.connect(tmp)
    con.row_factory = sqlite3.Row
    return con


class Focal:
    def __init__(self, con):
        self.con = con
        self.today = date.today().isoformat()
        self.tasks = [self._task(dict(r)) for r in con.execute('SELECT * FROM tasks')]
        self.by_id = {t['id']: t for t in self.tasks}
        self.projects = {r['id']: dict(r) for r in con.execute('SELECT * FROM projects')}
        self.categories = [r['name'] for r in con.execute('SELECT name FROM categories ORDER BY name')]
        self._done_days = None

    @staticmethod
    def _task(r):
        for k in ('completedAt', 'snoozedUntil', 'snoozedFrom', 'nextStep'):
            r.setdefault(k, '')
        try:
            subs = json.loads(r.get('subtasks') or '[]')
        except ValueError:
            subs = []
        return {
            'id': r['id'], 'title': r['title'] or '', 'priority': r['priority'] or 3,
            'category': r['category'] or '', 'due': r['due'] or '', 'recur': r['recur'] or '',
            'done': bool(r['done']), 'description': r['description'] or '',
            'notes': r['notes'] or '', 'subtasks': subs, 'projectId': r['projectId'],
            'teamFlag': bool(r.get('teamFlag')), 'created': r['created'] or '',
            'completedAt': r['completedAt'] or '', 'snoozedUntil': r['snoozedUntil'] or '',
            'deferrals': r.get('deferrals') or 0, 'nextStep': r['nextStep'] or '',
        }

    # Same as completionDayMap(): completedAt first, newest 'complete' changelog entry next.
    def done_day(self, t):
        if self._done_days is None:
            self._done_days = {}
            for r in self.con.execute("SELECT entityId, ts FROM changelog WHERE entityType='task' "
                                      "AND action='complete' ORDER BY ts"):
                self._done_days[r['entityId']] = local_day(r['ts'])
        if not t['done']:
            return None
        return local_day(t['completedAt']) if t['completedAt'] else self._done_days.get(t['id'])

    def project_title(self, pid):
        p = self.projects.get(pid)
        return p['title'] if p else None


def local_day(ts):
    """UTC ISO timestamp → local YYYY-MM-DD (never slice the UTC string; see CLAUDE.md)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone().date().isoformat()
    except ValueError:
        return ts[:10]


def days_between(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


# ── Ranking, mirrored from index.html (score(), stickyInfo(), isSnoozed()) ───

def score(t, today):
    pts = max(0, (4 - t['priority']) * 20)
    urgency = 0
    if t['due']:
        diff = days_between(today, t['due'])
        urgency = 20 + min(50, (-diff) ** 0.5 * 7) if diff <= 0 else max(0, 18 - diff * 0.9)
    return pts + urgency


def sticky(f, t):
    day = f.done_day(t) if t['done'] else f.today
    if not day:
        return None
    if t['deferrals'] >= STICKY_PUSHES:
        return f"pushed back {t['deferrals']} times"
    if t['due']:
        n = days_between(t['due'], day)
        if n >= STICKY_OVERDUE:
            return f'{n} days overdue'
    elif t['created']:
        n = days_between(local_day(t['created']), day)
        if n >= STICKY_AGE:
            return f'open {n} days'
    return None


def snoozed(t, today):
    return (not t['done'] and t['snoozedUntil'] and t['due'] == t['snoozedUntil']
            and t['snoozedUntil'] > today)


def by_due(tasks):
    return sorted(tasks, key=lambda t: (not t['due'], t['due'], t['category'].lower(), t['title'].lower()))


# ── Views ────────────────────────────────────────────────────────────────────

VIEWS = ['focus', 'today', 'overdue', 'upcoming', 'all', 'done', 'recent', 'snoozed', 'sticky', 'team']


def view(f, name, a):
    td = f.today
    pending = [t for t in f.tasks if not t['done']]
    if name == 'focus':
        return sorted(pending, key=lambda t: -score(t, td))
    if name == 'today':
        out = [t for t in pending if t['due'] and t['due'] <= td]
    elif name == 'overdue':
        out = [t for t in pending if t['due'] and t['due'] < td]
    elif name == 'upcoming':
        end = (date.today() + timedelta(days=a.days or UPCOMING_DAYS)).isoformat()
        out = [t for t in pending if t['due'] and td < t['due'] <= end]
    elif name == 'all':
        out = pending
    elif name == 'done':
        since = parse_day(a.since, past=True) if a.since else td
        out = [t for t in f.tasks if t['done'] and (f.done_day(t) or '') >= since]
        return sorted(out, key=lambda t: f.done_day(t) or '', reverse=True)
    elif name == 'recent':
        since = (date.today() - timedelta(days=a.days or 7)).isoformat()
        out = [t for t in f.tasks if (local_day(t['created']) or '') >= since]
        return sorted(out, key=lambda t: t['created'], reverse=True)
    elif name == 'snoozed':
        return sorted([t for t in f.tasks if snoozed(t, td)], key=lambda t: t['snoozedUntil'])
    elif name == 'sticky':
        out = [t for t in pending if sticky(f, t)]
    elif name == 'team':
        out = [t for t in pending if t['teamFlag']]
    return by_due(out)


def apply_filters(f, tasks, a):
    if a.cat:
        tasks = [t for t in tasks if t['category'].lower() == a.cat.lower()]
    if a.project is not None:
        tasks = [t for t in tasks if t['projectId'] == a.project]
    if a.priority:
        tasks = [t for t in tasks if t['priority'] == parse_priority(a.priority)]
    return tasks


# ── Output ───────────────────────────────────────────────────────────────────

def task_json(f, t, full=False):
    d = {k: t[k] for k in ('id', 'title', 'priority', 'category', 'due', 'recur', 'done',
                           'projectId', 'teamFlag', 'created', 'nextStep', 'deferrals')}
    d['project'] = f.project_title(t['projectId'])
    d['completedDay'] = f.done_day(t)
    d['snoozedUntil'] = t['snoozedUntil'] if snoozed(t, f.today) else ''
    d['sticky'] = sticky(f, t)
    d['subtasksOpen'] = sum(1 for s in t['subtasks'] if not s.get('done'))
    if full:
        d.update(description=t['description'], notes=t['notes'], subtasks=t['subtasks'])
    return d


def due_label(f, t):
    if t['done']:
        return 'done ' + (f.done_day(t) or '?')
    if not t['due']:
        return 'no date'
    n = days_between(f.today, t['due'])
    nice = datetime.strptime(t['due'], '%Y-%m-%d').strftime('%a %b %-d')
    if n < 0:
        return f'{nice} · {-n}d overdue'
    return {0: 'today', 1: 'tomorrow'}.get(n, nice)


def task_line(f, t):
    extras = []
    if t['category']:
        extras.append(t['category'])
    if t['recur']:
        extras.append('↻' + t['recur'])
    if snoozed(t, f.today):
        extras.append('snoozed')
    s = None if t['done'] else sticky(f, t)
    if s:
        extras.append('sticky: ' + s)
    open_subs = sum(1 for x in t['subtasks'] if not x.get('done'))
    if open_subs:
        extras.append(f'{open_subs} subtask' + ('s' if open_subs > 1 else ''))
    tail = ('  · ' + ' · '.join(extras)) if extras else ''
    return f"{t['id']:>5}  P{t['priority']}  {due_label(f, t):<25}  {t['title']}{tail}"


def emit(a, rows, line, empty='Nothing here.'):
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    elif not rows:
        print(empty)
    else:
        for r in rows:
            print(line(r))


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_ls(f, a):
    name = a.view or 'focus'
    matched = apply_filters(f, view(f, name, a), a)
    tasks = matched[:a.limit or (FOCUS_TOP if name == 'focus' else None)]
    if a.json:
        print(json.dumps([task_json(f, t) for t in tasks], indent=2, ensure_ascii=False))
        return
    shown = f'{len(tasks)} of {len(matched)}' if len(tasks) < len(matched) else str(len(tasks))
    print(f'{name} · {shown} task' + ('' if len(matched) == 1 else 's'))
    for t in tasks:
        print(task_line(f, t))


def cmd_show(f, a):
    t = f.by_id.get(a.id)
    if not t:
        die(f'no task {a.id}')
    links = [dict(r) for r in f.con.execute(
        'SELECT * FROM links WHERE (fromType="task" AND fromId=?) OR (toType="task" AND toId=?)',
        (t['id'], t['id']))]
    history = [dict(r) for r in f.con.execute(
        "SELECT ts, action, description FROM changelog WHERE entityType='task' AND entityId=? "
        'ORDER BY ts DESC LIMIT 10', (t['id'],))]
    if a.json:
        d = task_json(f, t, full=True)
        d['links'], d['history'] = links, history
        print(json.dumps(d, indent=2, ensure_ascii=False))
        return
    print(f"#{t['id']}  {t['title']}")
    print(f"  P{t['priority']} · {due_label(f, t)}" + (f" · {t['category']}" if t['category'] else '')
          + (f" · ↻{t['recur']}" if t['recur'] else '') + (' · team' if t['teamFlag'] else ''))
    if t['projectId']:
        print(f"  project: {f.project_title(t['projectId'])} (#{t['projectId']})")
    s = sticky(f, t)
    if s:
        print(f'  sticky: {s}')
    if snoozed(t, f.today):
        print(f"  snoozed until {t['snoozedUntil']}")
    if t['nextStep']:
        print(f"  next step: {t['nextStep']}")
    for label, text in (('description', t['description']), ('notes', t['notes'])):
        if text.strip():
            print(f'\n{label}:')
            print('  ' + text.strip().replace('\n', '\n  '))
    if t['subtasks']:
        print('\nsubtasks:')
        for i, st in enumerate(t['subtasks'], 1):
            print(f"  {i}. [{'x' if st.get('done') else ' '}] {st.get('title', '')}")
    if links:
        print('\nlinks:')
        for l in links:
            other = (l['toType'], l['toId']) if l['fromType'] == 'task' and l['fromId'] == t['id'] \
                else (l['fromType'], l['fromId'])
            print(f"  {other[0]} #{other[1]} {entity_title(f, *other)}" + (f" ({l['label']})" if l['label'] else ''))
    if history:
        print('\nhistory:')
        for h in history:
            print(f"  {local_day(h['ts'])}  {h['description']}")


def entity_title(f, kind, eid):
    table = {'task': 'tasks', 'project': 'projects', 'idea': 'ideas'}.get(kind)
    if not table:
        return ''
    r = f.con.execute(f'SELECT title FROM {table} WHERE id=?', (eid,)).fetchone()
    return repr(r['title']) if r else '(deleted)'


def cmd_search(f, a):
    q = a.text.lower()
    hits = [t for t in f.tasks if (a.all or not t['done']) and any(
        q in t[k].lower() for k in ('title', 'description', 'notes', 'nextStep'))]
    hits = apply_filters(f, by_due(hits), a)[:a.limit or None]
    if a.json:
        print(json.dumps([task_json(f, t) for t in hits], indent=2, ensure_ascii=False))
        return
    emit(a, hits, lambda t: task_line(f, t), f'No {"" if a.all else "open "}tasks match "{a.text}".')


def project_progress(f, p):
    if p['progressManual']:
        return p['progress']
    linked = [t for t in f.tasks if t['projectId'] == p['id']]
    return round(100 * sum(t['done'] for t in linked) / len(linked)) if linked else 0


def cmd_projects(f, a):
    rows = []
    for p in f.projects.values():
        if not a.all and p['status'] == 'complete':
            continue
        open_tasks = sum(1 for t in f.tasks if t['projectId'] == p['id'] and not t['done'])
        rows.append({'id': p['id'], 'title': p['title'], 'status': p['status'], 'type': p['type'],
                     'progress': project_progress(f, p), 'dueDate': p['dueDate'], 'openTasks': open_tasks})
    rows.sort(key=lambda r: (not r['dueDate'], r['dueDate'], r['title'].lower()))
    emit(a, rows, lambda r: f"{r['id']:>4}  {r['status']:<12} {r['progress']:>3}%  "
                            f"{r['dueDate'] or '—':<10}  {r['title']}  · {r['openTasks']} open")


def cmd_project(f, a):
    p = f.projects.get(a.id)
    if not p:
        die(f'no project {a.id}')
    try:
        notes = sorted(json.loads(p.get('noteList') or '[]'), key=lambda n: n.get('created', ''), reverse=True)
    except ValueError:
        notes = []
    tasks = by_due([t for t in f.tasks if t['projectId'] == p['id']])
    if a.json:
        d = {k: p[k] for k in ('id', 'title', 'status', 'type', 'startDate', 'dueDate', 'jobNumber', 'notes', 'created')}
        d.update(progress=project_progress(f, p), noteList=notes, tasks=[task_json(f, t) for t in tasks])
        print(json.dumps(d, indent=2, ensure_ascii=False))
        return
    print(f"#{p['id']}  {p['title']}")
    print(f"  {p['status']} · {project_progress(f, p)}% · {p['type']}"
          + (f" · due {p['dueDate']}" if p['dueDate'] else '') + (f" · job {p['jobNumber']}" if p['jobNumber'] else ''))
    if (p['notes'] or '').strip():
        print('\nnotes:\n  ' + p['notes'].strip().replace('\n', '\n  '))
    for n in notes:
        print(f"\nnote {local_day(n.get('created'))}:\n  " + n.get('text', '').strip().replace('\n', '\n  '))
    print(f'\ntasks ({sum(not t["done"] for t in tasks)} open):')
    for t in tasks:
        print(task_line(f, t))


def cmd_ideas(f, a):
    rows = [dict(r) for r in f.con.execute('SELECT id, title, status, description, created, updated FROM ideas')]
    rows.sort(key=lambda r: (r['status'], r['title'].lower()))
    emit(a, rows, lambda r: f"{r['id']:>4}  {r['status']:<12} {r['title']}")


def cmd_cats(f, a):
    rows = [{'name': c, 'open': sum(1 for t in f.tasks if not t['done'] and t['category'] == c)}
            for c in f.categories]
    emit(a, rows, lambda r: f"{r['open']:>4}  {r['name']}")


def parse_priority(v):
    v = str(v).strip().lower().lstrip('p')
    if v.isdigit() and 1 <= int(v) <= 4:
        return int(v)
    if v in PRIORITY_WORDS:
        return PRIORITY_WORDS[v]
    die(f'priority must be 1–4 or one of {", ".join(PRIORITY_WORDS)}')


def parse_day(v, past=False):
    """YYYY-MM-DD, today, tomorrow, yesterday, +3 / +3d / +2w, or a weekday: the next one,
    or with past=True (for --since) the most recent one, today included."""
    s, d0 = v.strip().lower(), date.today()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', s):
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError:
            pass
    elif s in ('today', 'tomorrow', 'yesterday'):
        return (d0 + timedelta(days={'today': 0, 'tomorrow': 1, 'yesterday': -1}[s])).isoformat()
    elif re.fullmatch(r'[+-]\d+[dw]?', s):
        n = int(s.rstrip('dw')) * (7 if s.endswith('w') else 1)
        return (d0 + timedelta(days=n)).isoformat()
    elif s[:3] in WEEKDAYS:
        if past:
            return (d0 - timedelta(days=(d0.weekday() - WEEKDAYS.index(s[:3])) % 7)).isoformat()
        ahead = (WEEKDAYS.index(s[:3]) - d0.weekday() - 1) % 7 + 1
        return (d0 + timedelta(days=ahead)).isoformat()
    die(f'bad date "{v}" (try 2026-10-15, tomorrow, +3d, fri)')


def check_category(f, name, allow_new):
    """The existing category matching `name` case-insensitively. Tasks store the category
    as a name, not an id, so "bilings" would fork a phantom category with no colour that
    filters miss. Insist on an existing one unless asked."""
    if not name.strip():
        return ''
    known = {c.lower(): c for c in f.categories}
    cat = known.get(name.strip().lower())
    if not cat:
        if not allow_new:
            die(f'no category "{name}". Existing: {", ".join(f.categories)} (--new-category to create)')
        cat = name.strip()
    return cat


def cmd_add(f, a):
    title = a.title.strip()
    if not title:
        die('title is required')
    dupes = [t for t in f.tasks if not t['done'] and t['title'].strip().lower() == title.lower()]
    if dupes and not a.force:
        die(f'already open: #{dupes[0]["id"]} "{dupes[0]["title"]}" (use --force to add anyway)', 3)
    cat = check_category(f, a.cat, a.new_category) if a.cat else ''
    if a.project is not None and a.project not in f.projects:
        die(f'no project {a.project}')
    if a.recur and not re.fullmatch(r'daily|weekly|monthly|every:[1-9]\d{0,2}', a.recur):
        die('recur must be daily, weekly, monthly or every:N')
    if a.recur and not a.due:
        die('a recurring task needs --due (its first occurrence)')
    body = {'title': title, 'due': parse_day(a.due) if a.due else f.today,
            'priority': parse_priority(a.priority) if a.priority else 3, 'category': cat,
            'description': a.desc or '', 'notes': a.notes or '', 'recur': a.recur or '',
            'projectId': a.project, 'teamFlag': a.team, 'source': a.source or 'CLI',
            'review': a.review}
    res = json.loads(http('POST', '/pending-tasks', body))
    if a.json:
        print(json.dumps({**res, 'task': body}, indent=2, ensure_ascii=False))
    elif a.review:
        print(f'Queued for review: "{title}". Approve it in Focal → Review.')
    else:
        print(f'Queued: "{title}"' + (f" (due {body['due']})" if body['due'] else '')
              + '. Focal adds it within a few seconds while open, or within ~2 min if closed.')


# ── Changes to existing tasks ────────────────────────────────────────────────
# Completing, snoozing and editing run app logic (recurring spawns, deferral counts,
# changelog wording), so they're queued with the launcher (POST /ops) and applied by an
# open Focal tab with the app's own functions. We wait for its result. With no tab open
# the change stays queued for the next one, and the app refuses one more than a day old.

def run_op(f, a, op, **kw):
    if a.id not in f.by_id:
        die(f'no task {a.id}')
    send_op(a, op, id=a.id, **kw)


def send_op(a, op, **kw):
    """Queue an op, wait up to --wait for an open tab to apply it, report, and exit:
    0 applied (or already so), 2 refused, 3 refused as a duplicate, 4 not applied yet."""
    body = {'op': op, 'source': a.source or 'CLI', **kw}
    q = json.loads(http('POST', '/ops', body))
    op_id, result = q['op_id'], None
    if q.get('tab_alive'):
        deadline = time.time() + a.wait
        while time.time() < deadline:
            st = json.loads(http('GET', '/ops/' + op_id))
            if st.get('status') == 'done':
                result = st['result']
                break
            time.sleep(0.5)
    if a.json:
        print(json.dumps({'op_id': op_id, 'applied': result is not None, 'result': result}, indent=2, ensure_ascii=False))
    elif result is None:
        print(f'Queued ({op_id}), not applied yet: '
              + ('Focal is open but hasn\'t picked it up (a background tab checks about once a minute).'
                 if q.get('tab_alive') else 'no Focal tab is open; the next one to open applies it.')
              + f'\nCheck on it with: focal op {op_id}')
    elif result.get('ok'):
        print(result.get('message', 'done'))
    else:
        print('focal: ' + result.get('error', 'refused'), file=sys.stderr)
    sys.exit(4 if result is None else 0 if result.get('ok') else 3 if result.get('duplicate') else 2)


def cmd_done(f, a):
    run_op(f, a, 'done', closeSubs=a.close_subs)


def cmd_reopen(f, a):
    run_op(f, a, 'reopen')


def cmd_undo(f, a):
    run_op(f, a, 'undo')


def cmd_snooze(f, a):
    until = parse_day(a.until)
    if until <= f.today:
        die('snooze needs a date after today')
    run_op(f, a, 'snooze', until=until)


def cmd_unsnooze(f, a):
    run_op(f, a, 'unsnooze')


def cmd_note(f, a):
    run_op(f, a, 'note', text=a.text)


def cmd_sub(f, a):
    if a.action == 'add':
        run_op(f, a, 'subtask', text=a.arg)
    elif a.arg.isdigit():
        run_op(f, a, 'subdone' if a.action == 'done' else 'subreopen', sub=int(a.arg))
    else:
        die(f'sub {a.action} takes the subtask\'s number, as `focal show` lists it')


def cmd_edit(f, a):
    fields = {}
    if a.title is not None:
        fields['title'] = a.title
    if a.due is not None or a.no_due:
        fields['due'] = '' if a.no_due else parse_day(a.due)
    if a.priority:
        fields['priority'] = parse_priority(a.priority)
    if a.cat is not None:
        fields['category'] = check_category(f, a.cat, a.new_category)
    if a.desc is not None:
        fields['description'] = a.desc
    if a.recur is not None or a.no_recur:
        if a.recur and not re.fullmatch(r'daily|weekly|monthly|every:[1-9]\d{0,2}', a.recur):
            die('recur must be daily, weekly, monthly or every:N')
        fields['recur'] = '' if a.no_recur else a.recur
    if a.project is not None or a.no_project:
        if a.project is not None and a.project not in f.projects:
            die(f'no project {a.project}')
        fields['projectId'] = None if a.no_project else a.project
    if a.team is not None:
        fields['teamFlag'] = a.team
    if not fields:
        die('nothing to change (see focal edit -h)')
    run_op(f, a, 'edit', fields=fields)


def parse_iso(v, what):
    """An ISO timestamp → UTC in JavaScript's toISOString() shape. One with no offset is
    taken as local time."""
    try:
        d = datetime.fromisoformat(v.strip().replace('Z', '+00:00'))
    except ValueError:
        die(f'bad {what} "{v}" (try 2026-10-12T21:02:10Z)')
    d = d.astimezone(timezone.utc)
    return d.strftime('%Y-%m-%dT%H:%M:%S.') + f'{d.microsecond // 1000:03d}Z'


def cmd_launch(f, a):
    t = f.by_id.get(a.id)
    if not t:
        die(f'no task {a.id}')
    started, ended = parse_iso(a.started, '--started'), parse_iso(a.ended, '--ended')
    if ended <= started:
        die('--ended must be after --started')
    if not 1 <= a.planned <= 600:
        die('--planned is minutes, 1–600')
    # Checked again by the app, which also catches one queued but not yet applied
    for r in f.con.execute('SELECT id, started FROM sessions WHERE taskId=?', (a.id,)):
        if r['started'] and parse_iso(r['started'], 'started') == started:
            die(f"already logged: session #{r['id']} on #{a.id} started {started}", 3)
    session = {'started': started, 'ended': ended, 'plannedMin': a.planned, 'outcome': a.outcome,
               'step': a.step or '', 'plan': a.plan or '', 'next': a.next or '', 'where': a.where or ''}
    if a.sticky is not None:
        session['sticky'] = a.sticky
    run_op(f, a, 'launch', session=session, closeSubs=a.close_subs)


def cmd_cat(f, a):
    if a.action == 'add':
        if len(a.names) != 1:
            die('usage: focal cat add NAME [--color #RRGGBB]')
        if a.color and not re.fullmatch(r'#[0-9a-fA-F]{6}', a.color):
            die('--color must be #RRGGBB')
        send_op(a, 'catadd', name=a.names[0], **({'color': a.color} if a.color else {}))
    else:
        if len(a.names) != 2:
            die('usage: focal cat rename OLD NEW')
        old, new = a.names
        if old.strip().lower() not in {c.lower() for c in f.categories}:
            die(f'no category "{old}". Existing: {", ".join(f.categories)}')
        send_op(a, 'catrename', name=old, to=new)


PROJECT_STATUSES = ['not-started', 'in-progress', 'active', 'on-hold', 'complete']


def cmd_proj(f, a):
    if a.action == 'add':
        title = a.arg.strip()
        if not title:
            die('a project needs a title')
        dupes = [p for p in f.projects.values() if p['status'] != 'complete' and p['title'].strip().lower() == title.lower()]
        if dupes and not a.force:
            die(f'already open: project #{dupes[0]["id"]} "{dupes[0]["title"]}" (use --force to add anyway)', 3)
        fields = {'title': title, 'status': a.status or 'not-started', 'type': a.type or 'client',
                  'jobNumber': a.job or '', 'startDate': parse_day(a.start) if a.start else '',
                  'dueDate': parse_day(a.due) if a.due else '', 'notes': a.notes or ''}
        return send_op(a, 'projadd', fields=fields)
    if not a.arg.isdigit():
        die(f'proj {a.action} takes a project id (see focal projects)')
    pid = int(a.arg)
    if pid not in f.projects:
        die(f'no project {pid}')
    if a.action == 'done':
        return send_op(a, 'projdone', id=pid)
    if a.status == 'complete':
        die('--status for reopen can\'t be complete')
    send_op(a, 'projreopen', id=pid, **({'status': a.status} if a.status else {}))


def cmd_pref(f, a):
    if a.action == 'get':
        prefs = {r['key']: r['value'] for r in f.con.execute('SELECT key, value FROM prefs')} \
            if f.con.execute("SELECT 1 FROM sqlite_master WHERE name='prefs'").fetchone() else {}
        if a.key is None:
            return emit(a, [{'key': k, 'value': v} for k, v in sorted(prefs.items())],
                        lambda r: f"{r['key']} = {r['value'][:80]}", 'No prefs set.')
        if a.key not in prefs:
            die(f'no pref "{a.key}"')
        print(json.dumps({'key': a.key, 'value': prefs[a.key]}, indent=2, ensure_ascii=False) if a.json else prefs[a.key])
        return
    if not a.key or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', a.key):
        die('a pref key is 1–64 of A-Z a-z 0-9 _ . : -')
    if a.action == 'unset':
        if a.value is not None:
            die('usage: focal pref unset KEY')
        return send_op(a, 'pref', key=a.key, value=None)
    if a.value is None:
        die('usage: focal pref set KEY VALUE  (VALUE - reads stdin)')
    send_op(a, 'pref', key=a.key, value=sys.stdin.read().rstrip('\n') if a.value == '-' else a.value)


def cmd_op(f, a):
    st = json.loads(http('GET', '/ops/' + a.op_id))
    if a.json:
        print(json.dumps(st, indent=2, ensure_ascii=False))
    elif st.get('status') == 'done':
        r = st['result']
        print(r.get('message') if r.get('ok') else 'refused: ' + r.get('error', ''))
    else:
        print('still queued' + ('' if st.get('tab_alive') else ' (no Focal tab is open)'))


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    p = argparse.ArgumentParser(prog='focal', description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog='Run `focal <command> -h` for options. FOCAL_URL=' + URL)
    p.add_argument('--db', metavar='FILE', help='read a local .db / .db.gz instead of the launcher')
    sub = p.add_subparsers(dest='cmd')

    def cmd(name, fn, help_, filters=False):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        sp.add_argument('--json', action='store_true', help='machine-readable output')
        if filters:
            sp.add_argument('--cat', help='only this category')
            sp.add_argument('--project', type=int, help='only tasks in this project id')
            sp.add_argument('-p', '--priority', help='only this priority (1–4 / high…)')
            sp.add_argument('-n', '--limit', type=int, help='at most N tasks')
        return sp

    def op_opts(sp):
        sp.add_argument('--wait', type=float, default=20, help='seconds to wait for Focal to apply it (20)')
        sp.add_argument('--source', help='who made the change, shown in Focal and the changelog (default CLI)')
        return sp

    sp = cmd('ls', cmd_ls, 'list tasks in a view (default: focus)', filters=True)
    sp.add_argument('view', nargs='?', choices=VIEWS, metavar='VIEW', help=' | '.join(VIEWS))
    sp.add_argument('--days', type=int, help='upcoming: look N days ahead (14); recent: N days back (7)')
    sp.add_argument('--since', help='done: completed on or after this day (default today; mon = this week\'s Monday)')

    cmd('show', cmd_show, 'one task in full').add_argument('id', type=int)

    sp = cmd('search', cmd_search, 'find tasks by text', filters=True)
    sp.add_argument('text')
    sp.add_argument('--all', action='store_true', help='include done tasks')

    cmd('projects', cmd_projects, 'list projects').add_argument('--all', action='store_true',
                                                               help='include complete ones')
    cmd('project', cmd_project, 'one project with its notes and tasks').add_argument('id', type=int)
    # Not `project add …`: settings.json allows `focal project *` without a prompt, as a read
    sp = cmd('proj', cmd_proj, 'add a project, or mark one done / reopen it')
    sp.add_argument('action', choices=['add', 'done', 'reopen'])
    sp.add_argument('arg', metavar='TITLE|ID', help='add: the title; done, reopen: the project id')
    sp.add_argument('--status', choices=PROJECT_STATUSES, help='add: initial status (not-started); '
                    'reopen: the status to reopen as (default: the one it had before it closed)')
    sp.add_argument('--type', choices=['client', 'internal'], help='add: default client, like the app')
    sp.add_argument('--job', help='add: job number (client projects)')
    sp.add_argument('--start', help='add: start date')
    sp.add_argument('--due', help='add: due date')
    sp.add_argument('--notes', help='add: notes')
    sp.add_argument('--force', action='store_true', help='add even if an open project has this title')
    op_opts(sp)
    cmd('ideas', cmd_ideas, 'list ideas')
    cmd('cats', cmd_cats, 'list categories with open-task counts')

    sp = cmd('add', cmd_add, 'add a task (queued; Focal does the insert)')
    sp.add_argument('title')
    sp.add_argument('-d', '--due', help='2026-10-15, today, tomorrow, +3d, +2w, fri… (default today)')
    sp.add_argument('-p', '--priority', help='1–4 or critical/high/medium/low (default 3)')
    sp.add_argument('-c', '--cat', help='an existing category (case-insensitive)')
    sp.add_argument('--new-category', action='store_true', help='allow a category that doesn\'t exist yet')
    sp.add_argument('--desc', help='description')
    sp.add_argument('--notes', help='notes')
    sp.add_argument('--recur', help='daily | weekly | monthly | every:N (needs --due)')
    sp.add_argument('--project', type=int, help='project id')
    sp.add_argument('--team', action='store_true', help='flag as a team task')
    sp.add_argument('--source', help='who added it, shown in Review (default CLI)')
    sp.add_argument('--review', action='store_true', help='send to the Review queue instead of adding')
    sp.add_argument('--force', action='store_true', help='add even if an open task has this title')

    def change(name, fn, help_, action=None):
        sp = cmd(name, fn, help_)
        if action:
            sp.add_argument('action', choices=action)
        sp.add_argument('id', type=int, help='task id')
        return op_opts(sp)

    change('done', cmd_done, 'complete a task (spawns the next one if it repeats)').add_argument(
        '--close-subs', action='store_true', help='also complete its open subtasks (otherwise refused)')
    change('reopen', cmd_reopen, 'un-complete a task (removes the next occurrence it spawned)')
    change('undo', cmd_undo, 'undo a completion: reopen it, and remove the next occurrence it '
           'spawned only if nobody has touched it')
    change('snooze', cmd_snooze, 'snooze a task until a date').add_argument('until', help='fri, +3d, 2026-10-20…')
    change('unsnooze', cmd_unsnooze, 'cancel a snooze, restoring the original due date')
    change('note', cmd_note, 'append a stamped line to a task\'s notes').add_argument('text')
    sp = change('sub', cmd_sub, 'add a subtask, or complete / reopen one by number', action=['add', 'done', 'reopen'])
    sp.add_argument('arg', metavar='TEXT|N', help='subtask text (add) or its number from `focal show` (done, reopen)')
    sp = change('edit', cmd_edit, 'change a task\'s fields')
    sp.add_argument('--title')
    sp.add_argument('-d', '--due', help='2026-10-15, tomorrow, +3d, fri…')
    sp.add_argument('--no-due', action='store_true', help='clear the due date')
    sp.add_argument('-p', '--priority', help='1–4 or critical/high/medium/low')
    sp.add_argument('-c', '--cat', help='an existing category; "" clears it')
    sp.add_argument('--new-category', action='store_true')
    sp.add_argument('--desc', help='replace the description')
    sp.add_argument('--recur', help='daily | weekly | monthly | every:N')
    sp.add_argument('--no-recur', action='store_true', help='stop repeating')
    sp.add_argument('--project', type=int, help='project id')
    sp.add_argument('--no-project', action='store_true')
    sp.add_argument('--team', action=argparse.BooleanOptionalAction, help='flag (or --no-team unflag) for the team agenda')
    sp = change('launch', cmd_launch, 'record a Launch session run elsewhere (feeds the streak and Launch ring)')
    sp.add_argument('--started', required=True, help='ISO timestamp (no offset = local time)')
    sp.add_argument('--ended', required=True, help='ISO timestamp')
    sp.add_argument('--planned', type=int, required=True, help='planned minutes (the timebox)')
    sp.add_argument('--outcome', required=True, choices=['done', 'parked'], help='done completes the task, like the app')
    sp.add_argument('--step', help='the tiny step being worked on')
    sp.add_argument('--plan', help='the if-stuck plan')
    sp.add_argument('--next', help='parked: the next step (the task\'s resume cue)')
    sp.add_argument('--where', help='parked: where you got to (stamped into the notes with --next)')
    sp.add_argument('--sticky', action=argparse.BooleanOptionalAction,
                    help='was it a sticky task (default: whether it is now)')
    sp.add_argument('--close-subs', action='store_true', help='done: also complete its open subtasks')

    sp = op_opts(cmd('cat', cmd_cat, 'add or rename a category'))
    sp.add_argument('action', choices=['add', 'rename'])
    sp.add_argument('names', nargs='+', metavar='NAME', help='add NAME | rename OLD NEW')
    sp.add_argument('--color', help='add: #RRGGBB (default random, like the app)')

    sp = op_opts(cmd('pref', cmd_pref, 'read or write a synced pref (prefs table)'))
    sp.add_argument('action', choices=['get', 'set', 'unset'])
    sp.add_argument('key', nargs='?', help='get with no key lists them all')
    sp.add_argument('value', nargs='?', help='set: the value (a string; - reads stdin)')

    cmd('op', cmd_op, 'check on a queued change').add_argument('op_id')

    a = p.parse_args(argv)
    if not a.cmd:  # bare `focal` = the Focus list
        a = p.parse_args(['ls'] if not a.db else ['--db', a.db, 'ls'])
    f = Focal(open_db(a.db))
    a.fn(f, a)


if __name__ == '__main__':
    try:
        main()
    except BrokenPipeError:  # `focal ls all | head`
        sys.stderr.close()
