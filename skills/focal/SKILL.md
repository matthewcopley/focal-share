---
name: focal
description: Read and change the user's Focal task list (their personal to-do / productivity app) with the `focal` command. Use whenever the user asks about their tasks, to-dos, what's due, overdue or next, what they did, their projects or ideas, or wants to add, complete, reopen, snooze, reschedule, re-prioritize, annotate or edit a task, even when they don't say "Focal".
---

# Focal

Focal is the user's personal task app. The `focal` command reads and changes the live data through the app's own server, so answers are current and changes go through the app's own logic: repeating tasks spawn their next occurrence, and the changelog records the change.

Never read or write `focal.db` directly, and never edit the app's code to change data. Use `focal` for everything.

## Reading

Read commands never change anything. Run them freely, and add `--json` when you need to process the output rather than show it.

| Question | Command |
|---|---|
| What should I work on? | `focal` (top 5 by Focus ranking: priority plus how close or overdue the due date is) |
| Due today / overdue / coming up | `focal ls today` (includes overdue), `focal ls overdue`, `focal ls upcoming [--days N]` |
| Everything open | `focal ls all` |
| What did I get done? | `focal ls done [--since yesterday\|mon\|2026-10-01]` |
| Recently added | `focal ls recent [--days N]` |
| Stuck / pushed back | `focal ls sticky` (pushed back twice or more, 3+ days overdue, or open 21+ days with no date) |
| Snoozed / team agenda | `focal ls snoozed`, `focal ls team` |
| One task in full | `focal show ID` (description, notes, numbered subtasks, links, history) |
| Find a task | `focal search TEXT [--all]` (`--all` includes done tasks) |
| Projects / ideas / categories | `focal projects`, `focal project ID`, `focal ideas`, `focal cats` |

`ls` and `search` take `--cat NAME`, `--project ID`, `-p 1-4` and `-n N`. Priorities: 1 Critical, 2 High, 3 Medium, 4 Low.

When answering, lead with the answer and keep task ids and due dates visible (`#589, due Sep 20, 13 days overdue`). The user may want to act on them next.

## Changing

| Change | Command |
|---|---|
| Add | `focal add "Title" [-d DATE] [-p high] [-c CATEGORY] [--desc TEXT] [--recur weekly] [--project ID] [--team]` |
| Complete / undo | `focal done ID [--close-subs]`, `focal reopen ID` |
| Snooze / unsnooze | `focal snooze ID DATE`, `focal unsnooze ID` (restores the original due date) |
| Edit fields | `focal edit ID [--title T] [-d DATE \| --no-due] [-p P] [-c CAT] [--desc T] [--recur R \| --no-recur] [--project ID \| --no-project] [--team \| --no-team]` |
| Note | `focal note ID "text"` (appends a timestamped line to the notes) |
| Subtasks | `focal sub add ID "text"`, `focal sub done ID N` (N as `focal show` numbers them) |

Dates accept `2026-10-15`, `today`, `tomorrow`, `+3d`, `+2w`, or a weekday (`fri` = the next Friday).

Rules for changes:

- **Always pass `--source claude`** so the change is attributed in the app and its changelog.
- **Act directly when the user named the change and the task is unambiguous**, e.g. "mark 589 done" or "snooze the Verizon task to Friday" when one open task matches. If you found the task by searching and more than one could fit, show the candidates and ask first.
- **Confirm before changing more than three tasks at once.** List them first.
- **`done` refuses a task with open subtasks** (exit 2). Tell the user which subtasks are open and ask before retrying with `--close-subs`.
- **Adding:** `focal add` refuses an exact duplicate of an open task's title (exit 3). Tell the user which task already exists rather than forcing it. A category must already exist (see `focal cats`, case doesn't matter); only use `--new-category` if the user asked for a new one. Use `--review` only if the user wants to approve it in Focal's Review view first.
- There is no delete. If the user wants a task gone, say so and suggest completing it or deleting it in the app.

## When a change doesn't apply right away

Adds land within seconds. Other changes are applied by an open Focal browser tab, and `focal` waits for it:

- **Exit 0**: done. Report the printed result, including any "next one" for repeating tasks.
- **Exit 4**: queued but not applied yet. Either no Focal tab is open, so it applies when one opens, or a background tab hasn't checked in (up to about a minute). Say so plainly, and offer to check with `focal op <op_id>`. Don't re-run the command, which would queue a second copy.
- **Exit 2**: refused (bad input, or the app rejected it). Relay the reason.
- **Exit 1**: can't reach Focal's server. The machine running Focal may be off or asleep, or, away from that machine, `FOCAL_URL` may not be set. Tell the user rather than retrying in a loop.
