# Focal

A personal productivity app — tasks, projects, ideas, wins, streaks, and a momentum dashboard — built as a single self-contained HTML file with a small local Python server. Your data lives in a SQLite file on your own machine; nothing is sent anywhere.

## Features

- **Tasks** with priorities, categories, due dates, subtasks, notes, and recurring schedules (daily/weekly/monthly/custom intervals — daily skips weekends)
- **Momentum dashboard** — a 0–100 score from on-time rate, overdue penalty, completion velocity, and win momentum, plus a trend chart, GitHub-style activity heatmap, and workday streaks
- **Focus view** — top 5 tasks with a daily goal progress bar
- **Projects, ideas, and a wins tracker**, all cross-linkable
- **Changelog** — an audit trail of everything you create, edit, and complete
- **Optional AI features** — task prioritization, daily digests, "what's next?" suggestions — powered by a local model (LM Studio etc.) or AWS Bedrock
- **Optional wiki integration** — link tasks/projects to pages in a personal markdown wiki

## Requirements

- Python 3.9+
- Flask: `pip install flask`
- A modern browser (Chrome/Edge/Firefox/Safari)

## Getting started

```bash
git clone <this-repo>
cd focal
pip install flask
python3 focal_launcher.py
```

Then open **http://localhost:8080**. That's it — a fresh database (`focal.db`) is created in the project folder the first time you save anything.

On macOS you can instead double-click `focal.command`, which starts the server and opens the browser for you (run `chmod +x focal.command` once if Finder complains).

> Don't serve the folder with `python3 -m http.server` — the app needs the launcher's `/db` endpoint to save your database.

## Your data

- Everything is stored in `focal.db` (SQLite) next to `focal_launcher.py`. Back it up by copying the file.
- The app auto-saves 2 seconds after any change and keeps its own rotating backups (last 7 snapshots, downloadable from Settings).
- `focal.db` is gitignored — never commit it if you fork this repo.

## Optional: AI features

Focal's AI features (Prioritize, digests, What's next?) work with either backend, selected in **Settings → AI backend**:

**Local model (default)** — run any OpenAI-compatible server, e.g. [LM Studio](https://lmstudio.ai):
1. Load a model in LM Studio and start the local server (default `http://127.0.0.1:1234`).
2. In Focal Settings, set the endpoint if yours differs. The model name is auto-detected; you can override it in Settings.
3. Prefer a non-reasoning model — Focal caps responses at ~600 tokens, which a reasoning model will burn on thinking.

**AWS Bedrock** — for a hosted model via your AWS account:
1. `pip install boto3`
2. `cp .env.example .env` and fill in `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, and `BEDROCK_MODEL_ID` (a model you've enabled in the Bedrock console).
3. Restart the launcher and pick **Bedrock** in Settings.

If you skip both, everything else in the app works normally.

## Optional: wiki integration

If you keep a folder of markdown notes, Focal can link tasks/projects to pages and let the AI read them for context:

1. Set `FOCAL_WIKI_ROOT` in `.env` (default `~/Documents/focal-wiki`). Pages go in `<root>/wiki/*.md`.
2. Start the wiki server by clicking the purple dot in Focal's top bar.
3. AI daily summaries (Settings) are written to `<root>/raw/focal/`.

The wiki server auto-shuts down after 30 idle minutes (`WIKI_IDLE_MINUTES` in `.env`).

## Optional: task handoff from scripts/agents

External tools should **not** write `focal.db` directly (the app's auto-save would clobber their rows). Instead, drop a JSON file into `.focal_inbox/` next to the database:

```json
{ "title": "Follow up with vendor", "priority": 2, "category": "Admin", "due": "2026-09-01" }
```

The running app drains the inbox every few seconds; if the app is closed, the launcher flushes queued tasks into the database after 90 seconds. Recognized fields: `title` (required), `priority` (1–4), `category`, `due`, `recur`, `description`, `notes`, `projectId`, `teamFlag`.

## Security notes

- Both local servers only accept browser requests originating from `http://localhost:8080`, so arbitrary websites can't reach your data via localhost fetches.
- The launcher serves only `index.html` — not the rest of the folder.
- Everything binds to localhost; nothing is exposed to your network.

## Architecture

| File | Purpose |
|---|---|
| `index.html` | The entire app — HTML, CSS, and JS in one file (sql.js runs SQLite in the browser via WebAssembly) |
| `focal_launcher.py` | Flask server on :8080 — serves the app, loads/saves `focal.db`, task-handoff inbox, Bedrock proxy, wiki server management |
| `wiki_server.py` | Flask wiki bridge on :8765 — reads markdown wiki pages (started from the app UI) |
| `focal.command` | macOS double-click launcher |

Frontend dependencies (sql.js, marked.js, Tabler Icons) load from CDN at runtime — no build step, no `node_modules`.
