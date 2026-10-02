# "Add to Focal" — macOS Shortcut

Turns the email you're looking at (or any selected text) into a Focal task, using
Apple Intelligence (on-device or Private Cloud Compute). The task lands in Focal's
**Review** list. Nothing is added to your tasks until you Approve, Edit or Discard it.

Needs macOS 26+ with Apple Intelligence turned on, running on the same Mac as the Focal launcher.

## How it flows

```
Mail (⌃⌥F) or selected text → Run AppleScript → Use Model (Apple AI) → POST /pending-tasks → Focal ▸ Review
```

## 1. Build it

In the Shortcuts app, create **Add to Focal**. In **ⓘ Details**, tick **Use as Quick Action** and **Services Menu**, then **Add Keyboard Shortcut** (e.g. ⌃⌥F). It's four actions, no variables. Receive **Text** from **Quick Actions**; if there's no
input, **Continue**.

1. **Run AppleScript** with *Shortcut Input*:

   ```applescript
   on run {input, parameters}
   	set today to "Today: " & (date string of (current date))
   	-- Selected text. Apps that share rich text (Outlook does) hand it over as a file,
   	-- so convert files to plain text rather than reading their path.
   	set picked to ""
   	try
   		if class of input is list then
   			set x to item 1 of input
   		else
   			set x to input
   		end if
   		if class of x is in {alias, «class furl», file} then
   			set picked to do shell script "textutil -convert txt -stdout " & quoted form of POSIX path of x
   		else
   			set picked to input as text
   		end if
   	end try
   	if picked is not "" then
   		if (length of picked) > 6000 then set picked to text 1 thru 6000 of picked
   		return today & linefeed & "Source: Selection" & linefeed & "@@BODY@@" & linefeed & picked
   	end if
   	-- No selection: read the email open in Apple Mail. Outlook can't be read this way.
   	if application "Mail" is not running then error "Nothing selected. Select the email's text (click in the message, then Cmd-A) and try again."
   	tell application "Mail"
   		set sel to selection
   		if sel is {} then error "Nothing selected. Select the email's text, or pick a message in Mail, and try again."
   		set m to item 1 of sel
   		set body to content of m
   		set hdr to "From: " & (sender of m) & linefeed & "Subject: " & (subject of m) & linefeed & "message://%3C" & (message id of m) & "%3E"
   	end tell
   	-- trim outside the tell block: inside it, Mail turns "text" into "rich text"
   	if (length of body) > 6000 then set body to text 1 thru 6000 of body
   	return today & linefeed & hdr & linefeed & "@@BODY@@" & linefeed & body
   end run
   ```

   Selected text wins. Apps that hand over rich text (Outlook does) pass it as a file,
   which `textutil` converts to plain text. With nothing selected it reads the message
   selected in Apple Mail. Text is capped at 6000 characters because the on-device
   model's context is small.
2. **Use Model**: Private Cloud Compute. Expand the action (⧁) and set the output to
   **Dictionary**. Prompt, ending with the *AppleScript Result* variable:

   ```
   Turn the email or text below into one to-do task for me. Return a dictionary with exactly these keys:
   title: a short imperative action, under 70 characters.
   description: 1-3 sentences with what's needed and the key details (amounts, names, dates).
   due: the deadline as YYYY-MM-DD, only if the text states or clearly implies one, otherwise empty. Work out relative dates like "Friday" or "end of month" from the Today line.
   priority: 1 critical, 2 high, 3 medium (the default), 4 low.
   category: exactly one of <your Focal categories, comma-separated>, or empty if none fits.

   [AppleScript Result]
   ```
3. **Get Contents of URL** `http://localhost:8080/pending-tasks`,
   Method **POST**, Request Body **JSON**, with Text fields `title`, `description`, `due`,
   `priority` and `category`. Each one is the *Response* variable → **Get Value for Key** → that name.
   Add one more field, `email` = *AppleScript Result*. The launcher puts the From /
   Subject / `message://` lines under the description. That's what becomes the
   **Open email** link.
4. **Show Notification** "Sent to Focal for review", body *Response → title*.

## 2. First run

Open an email in Mail and press **⌃⌥F**. macOS will ask to let Shortcuts control Mail
and to run AppleScript. Allow both.

To send text instead: select it anywhere, right-click → **Services → Add to Focal**.

**Outlook:** the new Outlook for Mac can't be read by scripts, so the shortcut can't
pick up the open email the way it does in Mail. Click in the message body, press
**⌘A** to select the email, then press **⌃⌥F**. If you right-click a message in the
list, Services won't appear: it only shows for selected text. Select the From and
Subject lines too if you want them in the task. Outlook has no link back to a
message, so these tasks won't get an **Open email** link.

## 3. In Focal

A **Review** entry appears in the sidebar under Focus, along with a toast ("New task
to review"), within about 4 seconds. Each item shows its priority, due date, category
(marked *(new)* if it isn't one of yours) and description. From there:

- **Approve**: adds the task as is.
- **Edit**: opens the normal task form. Saving adds the task.
- **Discard**: removes it, with Undo for 6 seconds.
- **Approve all**: shown when more than one task is waiting.

Queued items live in the launcher's `.focal_inbox/`, not in `focal.db`. They wait
there even if Focal is closed, and every device sees the same list.

## Troubleshooting

- **The notification shows but nothing appears in Focal.** Reload Focal. A tab opened
  before the Review feature existed adds these as plain tasks instead.
- **"Could not connect".** Check that the Focal launcher is running. Test with
  `curl http://localhost:8080/pending-tasks`.
- **The task arrives with an empty or odd title.** Run the shortcut from the editor
  with an email selected and look at the Use Model *Response*: its keys must be
  exactly `title`, `description`, `due`, `priority`, `category`. Private Cloud Compute
  follows the key list more reliably than On-Device.
- **Test the endpoint without the shortcut:**

  ```bash
  curl -X POST http://localhost:8080/pending-tasks -H 'Content-Type: application/json' -d '{"title":"Test from curl","priority":2}'
  ```
