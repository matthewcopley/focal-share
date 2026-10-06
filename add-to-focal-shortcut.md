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
   	-- Small models can't count weekdays, so hand them a lookup table for "by Friday".
   	set now to current date
   	set coming to {}
   	repeat with i from 1 to 14
   		set end of coming to my isoDay(now + i * days)
   	end repeat
   	set AppleScript's text item delimiters to ", "
   	set today to "Today: " & my isoDay(now) & linefeed & "Coming days: " & (coming as text)
   	set AppleScript's text item delimiters to ""
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

   on isoDay(d)
   	set m to text -2 thru -1 of ("0" & ((month of d) as integer))
   	set dd to text -2 thru -1 of ("0" & (day of d))
   	return ((weekday of d) as text) & " " & (year of d) & "-" & m & "-" & dd
   end isoDay
   ```

   Selected text wins. Apps that hand over rich text (Outlook does) pass it as a file,
   which `textutil` converts to plain text. With nothing selected it reads the message
   selected in Apple Mail. Text is capped at 6000 characters because the on-device
   model's context is small.
   It also lists the next 14 dates with weekdays: small models can't work out
   which date "Friday" is, but they can copy it from a list.
2. **Use Model**: Private Cloud Compute. The on-device model often takes the sender's own plans ("I'm going to call her") for yours. Expand the action (⧁) and set the output to
   **Dictionary**. Prompt, ending with the *AppleScript Result* variable:

   ```
   You write to-do items for <your name>, who <your role> at <your company>. Below is an email or text <your name> selected. Write the ONE task it creates for <your name>.

   Read it carefully first:
   - Who wrote it and who it's addressed to. Ignore signatures, phone numbers and footers.
   - What <your name> himself is asked or needs to do. When the writer says "I will" or "I'm going to" do something, that is the writer's job, not <your name>'s. If the email only keeps <your name> informed, <your name>'s task is to review it or follow up with the writer about it.

   Return a dictionary with exactly these keys, in this order:
   situation: one sentence on who wrote it and what <your name>'s part is.
   title: <your name>'s action, starting with a verb, under 60 characters, naming the person or thing involved. Examples: "Follow up with Kyle on ERP planning", "Send WTSC final invoice to Jenny", "Pay Acme invoice #2210 ($1,200)".
   description: 1-3 sentences of context <your name> will need later: who, what, amounts, links.
   due: a date in YYYY-MM-DD form, only if the text gives a deadline. Copy it from the Today / Coming days lines (for "Friday", use the date listed for Friday). Leave it empty when the text gives no deadline. Never guess one.
   priority: 1 urgent today, 2 has a deadline this week or money is overdue, 3 normal (the default), 4 low or just informational.
   category, exactly one of these names:
     <Category>: <what belongs in it>   (one line per Focal category)

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
(marked *(new)* if it isn't one of yours) and description. When the text gives no
deadline the model leaves `due` empty and the launcher sets it to the day you captured
it, so a captured task never lands undated. From there:

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
