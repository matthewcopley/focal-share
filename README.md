# Focal

A personal productivity app — tasks, projects, ideas, wins, streaks, and a momentum dashboard — built as a single self-contained HTML file with a small local Python server. Your data lives in a SQLite file on your own machine; nothing is sent anywhere.

## Features

- **Tasks** with priorities, categories, due dates, subtasks, notes, and recurring schedules (daily/weekly/monthly/custom intervals — daily skips weekends)
- **Momentum dashboard** — a 0–100 score from on-time rate, overdue penalty, completion velocity, and win momentum, plus a trend chart, GitHub-style activity heatmap, and workday streaks
- **Focus view** — top 5 tasks with a daily goal progress bar
- **Projects, ideas, and a wins tracker**, all cross-linkable
- **Changelog** — an audit trail of everything you create, edit, and complete

## Requirements

- Python 3.9+
- Flask: `pip install flask`
- A modern browser (Chrome/Edge/Firefox/Safari)

## Getting started

```bash
git clone https://github.com/matthewcopley/focal-share.git
cd focal-share
pip install flask
python3 focal_launcher.py
```

Then open **http://localhost:8080**. That's it — a fresh database (`focal.db`) is created in the project folder the first time you save anything.

On macOS you can instead double-click `focal.command`, which starts the server and opens the browser for you (run `chmod +x focal.command` once if Finder complains).

> Don't serve the folder with `python3 -m http.server` — the app needs the launcher's `/db` endpoint to save your database.

## Your data

- Everything is stored in `focal.db` (SQLite) next to `focal_launcher.py`. Back it up by copying the file.
- The app auto-saves 2 seconds after any change and keeps its own rotating backups (last 7 snapshots, downloadable from Settings).
- Focal can be open in several tabs or on several devices at once. Each save names the version of the database it started from, and if anything else has saved since, the app merges the two field by field instead of overwriting, so changes on both sides survive. Open tabs also pick up changes made elsewhere within a few seconds. After updating Focal, reload any tabs that were already open: a tab running the old code can't save until it's reloaded.
- `focal.db` is gitignored — never commit it if you fork this repo.

## Optional: task handoff from scripts/agents

External tools should **not** add tasks by writing `focal.db` directly: a new task needs the app's own bookkeeping (its id, the changelog entry, category matching). Instead, drop a JSON file into `.focal_inbox/` next to the database:

```json
{ "title": "Follow up with vendor", "priority": 2, "category": "Admin", "due": "2026-09-01" }
```

The running app drains the inbox every few seconds; if the app is closed, the launcher flushes queued tasks into the database after 90 seconds. Recognized fields: `title` (required), `priority` (1–4), `category`, `due`, `recur`, `description`, `notes`, `projectId`, `teamFlag`.

### Review queue: `POST /pending-tasks`

Tools that can't write files (a macOS Shortcut, a script on another machine) can POST the same JSON to `http://localhost:8080/pending-tasks` instead. These tasks are **held for review** rather than added: a **Review** entry appears in Focal's sidebar, where each one can be approved, edited (it opens the normal task form) or discarded, with Undo. Fields are coerced rather than rejected, since they often come from a language model: priority words (`high`, `low`, …) map to 1–4, and a malformed `due` is dropped. Only `title` is required, and an optional `source` (e.g. `"Mail"`) is shown on the card. Send `"review": false` to skip the review and add the task directly, as if it had been dropped in `.focal_inbox/`.

[`add-to-focal-shortcut.md`](add-to-focal-shortcut.md) builds an "Add to Focal" macOS Shortcut that turns the selected email into a task with Apple Intelligence and sends it here. `message://` links in a task's description render as **Open email**.

## Command line: `focal`

`focal_cli.py` reads and changes Focal from a terminal, a script or an AI agent. It needs only Python 3.9+, with no Flask, and talks to the launcher over HTTP, so it always sees the live data:

```bash
ln -s "$PWD/focal_cli.py" ~/.local/bin/focal   # or anywhere on your PATH
focal                        # top 5 by the Focus ranking
focal ls today               # also overdue, upcoming, all, done, recent, snoozed, sticky, team
focal ls upcoming --cat Admin -n 10
focal show 42                # one task: notes, subtasks, links, history
focal search invoice --all   # include done tasks
focal projects; focal project 3; focal ideas; focal cats
focal add "Send W-9" --due fri --cat Admin -p high
focal add "Maybe this" --review   # into the Review queue instead
focal done 42                # complete it (the next one is created if it repeats)
focal done 42 --close-subs   # ...and its open subtasks
focal reopen 42
focal snooze 42 fri; focal unsnooze 42
focal edit 42 -d +2d -p high --cat Admin    # also --title, --desc, --recur, --project, --team…
focal note 42 "Called them, waiting on a reply"
focal sub add 42 "Draft the email"; focal sub done 42 1
```

Every read takes `--json`. `--db FILE` reads a copy of the database (`.db` or `.db.gz`) instead of the running app. Set `FOCAL_URL` if the launcher isn't at `http://localhost:8080`. `focal add` refuses (exit code 3) a title that matches an open task, and refuses categories that don't exist yet unless you pass `--new-category`. It never writes `focal.db` itself: the add goes through `POST /pending-tasks`, and the app does the insert.

Changes to existing tasks (`done`, `edit`, `snooze`…) are applied by an open Focal tab, using the same code as the buttons, so a repeating task still spawns its next occurrence and the changelog records the change (tagged `via CLI`, or `--source NAME`). The command waits for the tab to apply the change and prints the result. If no Focal tab is open, the change waits for the next one, the command exits with code 4, and `focal op <id>` checks on it later. A change more than a day old is refused rather than applied stale.

## Security notes

- Both local servers only accept browser requests originating from `http://localhost:8080`, so arbitrary websites can't reach your data via localhost fetches.
- The launcher serves only `index.html` — not the rest of the folder.
- Everything binds to localhost; nothing is exposed to your network.

## Architecture

| File | Purpose |
|---|---|
| `index.html` | The entire app — HTML, CSS, and JS in one file (sql.js runs SQLite in the browser via WebAssembly) |
| `focal_launcher.py` | Flask server on :8080 — serves the app, loads/saves `focal.db`, task-handoff inbox, pasted images |
| `focal_cli.py` | The `focal` command line: read views and add tasks over HTTP |
| `focal.command` | macOS double-click launcher |

Frontend dependencies (sql.js, Tabler Icons) load from CDN at runtime — no build step, no `node_modules`.
