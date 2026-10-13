---
name: office
description: "Use whenever a Word document (.docx) in the workspace is to be written, filled in, edited or commented on and the office_edit tool is available: the user watches your edits appear live in the file pane, under your name, and may edit beside you. Covers drafting a new document section by section, revising one, re-wording, tables, lists, charts, pictures and comment threads. Use the docx skill only when the office tools are not available."
requires_tools: [office_edit]
---

# Writing documents live with the user

The document is open in the user's file pane, or opens there once your first edit to it has run (once per document:
a user who closes it is not pulled back). Every `office_edit` call appears whole, under your name, while the user
watches and may type in the same document. Work like a colleague in a shared document, not like a program writing a
file.

## The workflow

1. **Read first.** `office_read` (outline) before any edit, and again whenever an edit is refused. Target blocks by the
   `id` and `hash` it gives. Quote words with `find` only for a phrase inside a block, copied from the `block` view,
   which gives the block's exact text (the outline shows only its first words). `find` searches the whole document and
   `nth` counts its exact matches from the top, from 1: quote enough words to be unique (the `search` view ignores
   case, so its count can differ).
2. **A new document: plant the outline first.** A path that does not exist yet: `office_edit` creates it blank; there
   is nothing to read first. One `office_edit` call inserts the title and every section's heading, each followed by a
   pending block (`{"type": "pending", "intent": "…"}`) saying what it will hold. The user sees the whole shape at once
   and can delete a section before you write it.
3. **Then fill one section per call**, replacing its pending block (`{"pending": "<key>"}`). Between calls, say one
   short line in the chat ("Writing the risks section."). A pending block the user deleted is a section they do not
   want: leave it out.
4. **Change only what was asked.** Re-word with `replace` on quoted words (`{"text": …}`), which keeps their
   formatting; replace a whole block only when it all changes.
5. **When an edit is refused,** nothing of the call applied and no one saw any of it. The result names the op and
   why. When its block changed since you read it, the result shows the block as it stands now: someone edited it, and
   their edit wins. A block that is gone was deleted. Read again and work from what is there; never put your old text
   back over theirs. Ops in one call run in order, each on what the earlier ops left: a block an earlier op replaced
   or deleted is gone for the later ones, and one it re-worded has a new hash.
6. **Never touch an open document any other way.** No `write_file`, `patch`, terminal, python-docx or the docx skill on
   it: the editor is its only writer, and anything else is refused or lands as a conflicting copy.
7. **Finish with one line** in the chat saying what you did and what the user can do next (edit, comment, ask for
   another pass). The findings belong in the document, not in the chat.

## Writing rules

- Lead with the answer; a reader skimming the first line of each section gets the point.
- Specifics over adjectives: numbers, names, dates.
- Short paragraphs; lists for parallel items; a table when the reader compares across rows.
- Use the document's own styles (headings, lists, table style): never fake a heading with bold text.

See `references/word.md` for the op formats with examples.
