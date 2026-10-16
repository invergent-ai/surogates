---
name: office
description: "Use whenever a Word document (.docx), spreadsheet (.xlsx), slide deck (.pptx) or PDF in the workspace is to be written, filled in, edited or commented on and the office_edit tool is available: the user watches your edits appear live in the file pane, under your name, and may edit beside you. Covers drafting a document section by section, filling and formatting sheets, building slides in the Claude Slides format, and filling, redacting or annotating a PDF. Use the docx, xlsx and pptx skills only when the office tools are not available."
requires_tools: [office_edit]
---

# Working on documents live with the user

The document is open in the user's file pane, or opens there once your first edit to it has run (once per document:
a user who closes it is not pulled back). Every `office_edit` call appears whole, under your name, while the user
watches and may edit the same document. Work like a colleague in a shared document, not like a program writing a file.

## Where the document is

- Name a document by its path relative to the workspace, the folder the file pane shows. The same on a folder of the
  user's computer and in a project: `office_edit` always reaches the real document, never a thread's copy. Never `~`,
  `$HOME` or an absolute path outside that folder.
- Nothing to look up first: `office_edit` makes a missing document, and `office_read` says when there is none. No
  `list_files`, terminal or other tool to find or check the place.
- `office_read` is how you check what you wrote: once it shows your change, the document has it. Your own files can lag
  the editor or not have the document at all (a project thread's copy of the folder gets it at its next turn): never
  look for it with `read_file`, `list_files` or the terminal.
- You cannot see pages or slides. Never render or convert the document to check it: the user is looking at it live.

## The workflow

1. **Read first.** `office_read` before any edit, and again whenever an edit is refused: its outline gives what the
   ops target (block ids and hashes, sheets and ranges, slide ids and hashes, pages and fields).
2. **One piece of work per call**: a section, a block of cells, a slide or two, a set of fields. Between calls, say
   one short line in the chat ("Building the pricing slide.").
3. **Change only what was asked.**
4. **When an edit is refused,** nothing of the call applied. The result says why, and shows what you targeted as it
   is now: someone edited it, and their edit wins. Read again and work from what is there; never put your old version
   back over theirs. A user editing the very cells or slide you want: wait, or ask them.
5. **Never touch an open document any other way.** No `write_file`, `patch`, terminal, python libraries or the docx,
   xlsx, pptx or pdf skills on it: the editor is its only writer.
6. **Finish with one line** in the chat saying what you did and what the user can do next. The work belongs in the
   document, not in the chat.

## Each kind of document

- **Word documents:** plant a new document's outline first, every section a pending block saying what it will hold,
  then fill one section per call; re-word with `replace` on quoted words, which keeps their formatting. See
  `references/word.md`.
- **Spreadsheets:** read the range before writing it, and pass what it showed as `expect`. Values go in as typed into
  a cell: a formula starts with `=` and stays live. See `references/cell.md`.
- **Slide decks:** each slide is one `<section>` in the Claude Slides format, built as native shapes the user can edit.
  A slide too full for its boxes is refused: split it; nothing shrinks. See `references/slide.md`.
- **PDFs:** fill fields, add, remove or rotate pages, redact, annotate. Existing text cannot be changed. See
  `references/pdf.md`.

## When a comment calls you

A message that starts `[Comment on <file>]` is a person who mentioned you in a comment in that document: the words in
« » are theirs, quoted from it. "On it, I'll reply here when done." is already posted in the thread, and the note with
the message says which thread it is.

1. Read the document with `office_read`, and the thread in its outline: the mention may be a reply, with the question
   earlier in the thread.
2. Do what it asks with `office_edit`, as for any request.
3. Reply in the thread with **only the result**, in one or two sentences: "Fixed: Q1 North is 120, from the March
   export." Never your reasoning, the steps or the conversation: those go in the chat. A reply over 400 characters is
   refused. If you cannot do it, say why in one sentence, and leave the thread open.
4. Resolve the thread **last**, in the same call as the reply or after it: a resolve can close the person's open reply
   box.
5. Finish with one line in the chat, as after any request.

## Writing rules

- Lead with the answer; a reader skimming the first line of each section gets the point.
- Specifics over adjectives: numbers, names, dates.
- Short paragraphs; lists for parallel items; a table when the reader compares across rows.
- Use the document's own styles (headings, lists, table style): never fake a heading with bold text.
