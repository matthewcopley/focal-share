# "Add to Focal" — macOS Shortcut

Turns the email you're looking at (or any selected text) into a Focal task, using
Apple Intelligence (on-device or Private Cloud Compute). The task lands in Focal's
**Review** list. Nothing is added to your tasks until you Approve, Edit or Discard it.

Needs macOS 26+ with Apple Intelligence turned on, running on the same Mac as the Focal launcher.
Build time: about 5 minutes.

## How it flows

```
Mail (⌃⌥F) or selected text → Shortcut → Use Model (Apple AI) → POST /pending-tasks → Focal ▸ Review
```

## 1. Create the shortcut

Shortcuts app → **+** → name it **Add to Focal**.

In the right sidebar, open the **ⓘ Details** tab:

- ✅ **Use as Quick Action**, then tick **Services Menu**.
- Click **Add Keyboard Shortcut** and press **⌃⌥F** (or any combo you like).

At the top of the editor, the "Receive" line should read: Receive **Text** from **Quick Actions**.
If there's no input: **Continue**.

## 2. Add the actions in order

### A. Get the text: either the selection or the open email

1. **If** · *Shortcut Input* · **has any value**
   - **Set Variable** `Email` to *Shortcut Input*
   - **Text** → leave it empty → **Set Variable** `Header`
   - **Text** `Selection` → **Set Variable** `Source`
2. **Otherwise**
   - **Run AppleScript**, replacing the template with:

     ```applescript
     on run {input, parameters}
         tell application "Mail"
             set sel to selection
             if sel is {} then return ""
             set m to item 1 of sel
             set body to content of m
             if (length of body) > 6000 then set body to text 1 thru 6000 of body
             set hdr to "From: " & (sender of m) & linefeed & ¬
                 "Subject: " & (subject of m) & linefeed & ¬
                 "message://%3C" & (message id of m) & "%3E"
             return hdr & linefeed & "@@BODY@@" & linefeed & body
         end tell
     end run
     ```
   - **Split Text** *AppleScript Result* by **Custom** `@@BODY@@`
   - **Get Item from List**: **First Item** of *Split Text* → **Set Variable** `Header`
   - **Set Variable** `Email` to *AppleScript Result*
   - **Text** `Mail` → **Set Variable** `Source`
3. **End If**

The `message://` line becomes an **Open email** link on the task, which opens the
original message in Mail. The body is capped at 6000 characters because the
on-device model has a small context window.

### B. Ask Apple Intelligence for the task

4. **Get Current Date** → **Format Date**: Custom, `yyyy-MM-dd EEEE`
5. **Use Model**: pick **Private Cloud Compute**. It reads long emails better.
   **On-Device** also works and keeps everything on the Mac. Prompt:

   ```
   Turn this into one to-do task for me. Today is [Formatted Date].
   Reply with ONLY a JSON object, no other text, no code fences:
   {"title": "", "description": "", "due": "", "priority": 3, "category": ""}

   - title: a short imperative action, under 70 characters ("Send Q3 invoice to Acme").
   - description: 1-3 sentences with what's needed and key details (amounts, names, dates).
   - due: YYYY-MM-DD, only if the text states or clearly implies a deadline; otherwise "".
   - priority: 1 critical, 2 high, 3 medium (default), 4 low.
   - category: exactly one of <your Focal categories, comma-separated>,
     or "" if none fits.

   Text:
   [Email]
   ```

   (Insert *Formatted Date* and the `Email` variable as variables, not typed text.)
6. **Match Text**: pattern `\{[\s\S]*\}` in *Response*. This strips any stray prose or
   ``` fences the model adds.
7. **Get Dictionary from Input** · *Matches*

### C. Send it to Focal

8. **Text**, then **Set Variable** `Description`:

   ```
   [Dictionary.description]

   [Header]
   ```

   To insert `Dictionary.description`, add the *Dictionary* variable, click it, and choose
   **Get Value for Key** → `description`.
9. **Get Contents of URL**
   - URL: `http://localhost:8080/pending-tasks`
   - Method: **POST**
   - Request Body: **JSON**, with these Text fields:

     | Key | Value |
     |---|---|
     | `title` | Dictionary → `title` |
     | `description` | `Description` |
     | `due` | Dictionary → `due` |
     | `priority` | Dictionary → `priority` |
     | `category` | Dictionary → `category` |
     | `source` | `Source` |
10. **Show Notification**: `Sent to Focal for review: [Dictionary.title]`

## 3. First run

Open an email in Mail and press **⌃⌥F**. macOS will ask to let Shortcuts control Mail
and to run AppleScript. Allow both. Pressing the shortcut with no email selected sends
an empty prompt, so select a message first.

To send text instead: select it anywhere, right-click → **Services → Add to Focal**.

## 4. In Focal

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
- **"Get Dictionary" fails.** The model answered with something other than JSON. Run
  the shortcut from the editor to see the *Response*. Switching to Private Cloud
  Compute usually fixes it.
- **Test the endpoint without the shortcut:**

  ```bash
  curl -X POST http://localhost:8080/pending-tasks -H 'Content-Type: application/json' -d '{"title":"Test from curl","priority":2}'
  ```
