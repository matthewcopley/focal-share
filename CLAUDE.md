# CLAUDE.md

Guidance for Claude Code when working in this repository. See `README.md` for what
the app does and how to run it.

## What this repo is

Focal is a single self-contained `index.html` (HTML, CSS and JS, CDN dependencies,
no build step) served by `focal_launcher.py` (Flask, port 8080). Data lives in a local
`focal.db` (SQLite via sql.js); pasted screenshots live as files in `focal_images/`.
Both are gitignored. There are no tests, lint or CI. Run `python3 focal_launcher.py`
and open http://localhost:8080; `python3 -m http.server` won't work, because it has
no `/db` endpoint.

This is the **public, sanitized release** of a private working copy where the app is
developed day to day. Changes normally arrive here as ports from that copy, so keep
the files easy to port into.

## The shared regions are byte-identical

Apart from a short list of deliberate differences, `index.html` here is
byte-for-byte the private copy's `index.html`. That is what lets a change port
cleanly. **The deliberate differences are:**

- the top-bar `#app-name` reads "Focal";
- three comments say "external agents/scripts" and "remote/LAN link" where the
  private copy names its own setup.

The AI features and the wiki integration, which used to account for most of the
differences, were removed from both copies in Oct 2026.

`focal_launcher.py` likewise matches the private copy apart from two inbox
comments and `ALLOWED_ORIGINS` (localhost only here). Its endpoints are identical:
`/db`, `/images` and `/pending-tasks*`.
`focal.command` is this repo's own path-independent version.

**Keep it that way.** Make app changes in a form that applies to both copies:
no reformatting, renaming or reordering of shared code here alone. A change that
only lands here becomes a merge conflict on every later port.

**Never commit personal data or setup:** a real `focal.db`, `focal_images/`,
`.env`, real names or clients, personal paths, hostnames or tailnet addresses.

## Porting from the private copy

Port with `git cherry-pick` from the private repo added as a remote, not with
`git apply` on an exported diff. Cherry-pick does a 3-way merge, so only real
overlaps conflict. Resolve them by keeping this repo's side of the deliberate
differences above. Drop the private `CLAUDE.md` from each pick, and end the
commit message with "Ported from the private working copy." without naming
private repos or machines.

**Drift, Sep–Oct 2026.** Twelve consecutive private-side changes went unported
(default due date and team checkbox, the zoomed trend axis, the goal bar on
every list, due-date sort, pasted screenshots, the subtask confirm dialog,
project notes, the snooze rework, three-level sort, and the Upcoming
Tomorrow / This Week filters), while one later change was ported on its own.
The next port, Launch, then failed `git apply` on 15 of 16 hunks. All of them
were ported in order on Oct 1 2026. The shared regions are byte-identical again,
and a diff against the private `index.html` shows only the deliberate set
(4 hunks since Oct 2026). Port each change in the same session it's made.
