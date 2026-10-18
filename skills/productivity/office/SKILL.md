---
name: office
description: "Use whenever a Word document (.docx), spreadsheet (.xlsx), slide deck (.pptx) or PDF in the workspace is to be written, filled in, edited or commented on and the office_edit tool is available: the user watches your edits appear live in the file pane, under your name, and may edit beside you. Covers drafting a document section by section, filling and formatting sheets, building slides in the Claude Slides format, and filling, redacting or annotating a PDF."
requires_tools: [office_edit]
---

# Working on documents live with the user

Make a new document with `office_edit` too, not only change one: a missing `.docx`, `.xlsx`, `.pptx` or `.pdf` is made
blank and opens in the user's file pane. Never make or change one with a script, a python library (python-docx,
openpyxl, python-pptx, pypdf), `write_file`, `patch` or the terminal: the user can neither watch nor edit a document
made that way. What `office_edit` cannot do to a PDF (a new PDF with content; merging, splitting or encrypting PDFs)
is the pdf skill's work.

The document is open in the user's file pane, or opens there once your first edit to it has run (once per document:
a user who closes it is not pulled back). Every `office_edit` call appears whole, under your name, while the user
watches and may edit the same document. Work like a colleague in a shared document, not like a program writing a file.

## Where the document is

- Name a document by its path relative to the workspace, the folder the file pane shows. The same on a folder of the
  user's computer and in a project: `office_edit` always reaches the real document, never a thread's copy. Never `~`,
  `$HOME` or an absolute path outside that folder.
- Nothing to look up to find the place: `office_edit` makes a missing document, and `office_read` says when there is
  none. No `list_files`, terminal or other tool to find or check the place.
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
5. **Never touch an open document any other way.** No `write_file`, `patch`, terminal, script or python library on
   it: the editor is its only writer. The pdf skill's work (merging, splitting, encrypting) writes a new file, never
   over an open one.
6. **Check before you answer** (below), then finish with one line in the chat saying what you did and what the user
   can do next. The work belongs in the document, not in the chat.

## Before you answer

1. Read the outline again with `office_read`, and a block, range, slide or page in full where a detail matters.
2. Every fact the user gave is in the document: each name, number, date and item they asked for. No specific is made
   up: no name, date, time, figure, place, source or contact they did not give, and nothing said to have happened that
   their material does not say. The one exception: a spreadsheet template's sample rows, for a user who gave no rows,
   said in the chat to be samples. Each part they asked for is there: one they did not detail is drafted as a
   proposal, never left out or asked about.
3. The length is what was asked: about 450 words of body text fill a page, and each Word edit's result gives the
   document's `length` in words and pages.
4. Then say in one line what was done, what you drafted for the user to check, and what is missing. Something short:
   add it first, from what the user gave or drafted from common practice; never pad, and never make up a specific.

## Each kind of document

Your first office call on each type of document brings these rules, that type's reference and its templates, as
`guide`: `references/docx.md`, `references/xlsx.md`, `references/pptx.md` or `references/pdf.md`, and
`templates/docx.md`, `templates/xlsx.md` or `templates/pptx.md` (a PDF has none). They stay in this skill to read again.

Before you create a new document, open its type's template and start from it: `skill_view` with
`{"name": "office", "file_path": "templates/docx.md"}`, or `templates/xlsx.md` or `templates/pptx.md`. The call that
makes a new document is the first on it, so its `guide` comes after the outline is written.

- **Word documents:** start a new document from its template, in `templates/docx.md`: plant its outline first, every
  section a pending block saying what it will hold (a line `<?pending name: what it will hold?>`), then fill one
  section per call; re-word with `replace` on quoted words, which keeps their formatting. About 450 words of
  body text fill a page: count them for a length the user asks for.
- **Spreadsheets:** start a new workbook from its template, in `templates/xlsx.md`. In a sheet with content, read the
  range before writing it, and pass what it showed as `expect`. Values go in as typed into a cell: a formula starts
  with `=` and stays live.
- **Slide decks:** build a new deck from the slide templates, in `templates/pptx.md`. A small change (a title, a word, a
  number) is `text` on that one shape, which keeps its place and style. A new or redesigned slide is one `<section>` in
  the Claude Slides format, built as native shapes the user can edit. A slide too full for its boxes is refused: split
  it; nothing shrinks.
- **PDFs:** fill fields, add, remove or rotate pages, redact, annotate. Existing text cannot be changed.

## When a comment calls you

A message that starts `[Comment on <file>]` is a person who mentioned you in a comment in that document: the words in
« » are theirs, quoted from it. "On it, I'll reply here when done." is already posted in the thread, and the note with
the message says which thread it is, or, in a PDF, how to find it.

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
- Specifics over adjectives: the numbers, names and dates the user gave.
- **Specifics** (names, dates, times, figures, places, sources, contacts, and what happened: a meeting's decisions, a
  study's results) come from the user or their files: one they did not give is left out, never made up. The one
  exception: a spreadsheet template's sample rows, for a user who gave no rows, said in the chat to be samples.
- **Content** the user asked for but did not detail (a first-week plan, the points of a section, the steps of a
  process) is drafted from common practice as a proposal, with no specific they did not give; say in the chat what you
  drafted, for them to check.
- **Ask the user** only when the work cannot be done without the answer (which of two documents to change); otherwise
  do the work, and say in the chat what is missing or proposed.
- Short paragraphs; lists for parallel items; a table when the reader compares across rows.
- Use the document's own styles (headings, lists, table style): never fake a heading with bold text.
