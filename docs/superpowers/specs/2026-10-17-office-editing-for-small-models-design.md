# Office Editing for Small Models — Design

**Status:** recommended after a benchmark of the bar model, approved in direction ("do it"); not yet planned
**Date:** 2026-10-17
**Builds on:** [Office Editors — Design](2026-10-13-office-editors-design.md) (the live editors, `office_read` /
`office_edit`, runtime-office, the office skill)

## Intent

An agent edits Word documents, spreadsheets, slide decks and PDFs live, through `office_read` and `office_edit`. That
works well with a frontier model. It has to work just as well with a much smaller one: the bar is `surogate3.7-flash`,
a 35B mixture-of-experts model with about 3B parameters active per token and instruction following above 90%. It must
handle everything on its own, end to end, including creating new material from scratch, with the advisor model off.

The order of work is fixed: first everything that guides the model (the protocol, the tool descriptions, the office
skill, templates), measured on the benchmark; fine-tuning only for what is still failing after that.

## What the benchmark found

`office_bench` drives the real agent harness: a Work chat (`channel=studio`) on a real agent, with every production
tool and system prompt, one session at a time; each task's checks read the saved document. 33 tasks (Word 11, slides
10, spreadsheets 9, PDF 3), each run three times on the bar model: 99 sessions, 0 lost to infrastructure.

- **Score:** 566/594 checks (95%); 74/99 sessions passed every check. Rendering the decks showed the benchmark's text
  overflow check was wrong every time it fired: 9 sessions failed on it alone. Corrected, **82/99** sessions are
  right: spreadsheets 26/27, PDF 8/9, slides 25/30, Word 23/33.
- **It claims success it did not achieve.** 23 of the 25 failing sessions ended telling the user the work was done.

The failures, by cause:

| What went wrong | Sessions | Why |
| --- | --- | --- |
| Adding a third item to a numbered list gives "1. 2. 1." | docx-continue-list 3/3 | An inserted list always starts its own numbering, and neither the edit result nor the outline shows list numbers, so the model "verified" and reported "3." |
| Adding speaker notes moves and resizes the slide's title | pptx-notes 3/3 | Notes can only be set by rebuilding the whole slide (`slides` with an `<aside>`) |
| A document far shorter than asked | docx-report 2/3 (332 and 505 words for "two pages"), docx-faq, docx-guide, docx-proposal 1/3 each | The model does not count; word.md's "about 450 words fill a page" was read every time and still not applied; no result says how long the document is |
| Something asked for left out | docx-proposal (the total), docx-report (the chart) | No check against the request before answering |
| The document built by a script, not in the editor | 6 sessions never called `office_read` or `office_edit` (pptx-kpi-chart r2 and r3, pptx-pitch r2, pptx-workshop r1, xlsx-sales-table r2, xlsx-dates r3); 3 more mixed scripts in (docx-report r2, xlsx-sales-table r3, pdf-annotate r3) | The model wrote python-pptx or openpyxl scripts, reasoning that the office skill is for editing and a new file is different; nothing tells it otherwise. The user cannot watch or co-edit such a file, and two of them failed: a chart off a python-pptx default 4:3 slide (pptx-kpi-chart r2), and values typed where formulas were asked (xlsx-dates r3) |
| Card headings invisible: white text on white cards | pptx-onboarding 1/3 | Contrast is a guideline in slide.md, never checked (the benchmark does not check it either) |
| A PDF's form fields destroyed | pdf-annotate 1/3 | The model read the sandbox `pdf` skill instead of the office skill, then rewrote the PDF with pypdf from the terminal |
| A PDF note placed far from its line | pdf-annotate (earlier run) | `annotate` takes only a rectangle in points, and the page view has no positions: the model computes coordinates itself, with pdfplumber in the sandbox |

Waste, which costs model calls rather than checks:

- **309 `office_edit` calls; 31 rejected because `ops` was not a list**, in 9 sessions. 30 of them were JSON with a
  closing brace missing, four or five levels deep (`ops → op → content → blocks → key → {…}`), which the model
  server's tool parser passes on as a string; one was a single op sent without the `ops` list. The error,
  `"ops" is a non-empty list of ops.`, says neither, so the model sends the same thing again: 16 times in one
  session, which took 51 model calls and 12 minutes.
- **11 rejected for an op shape we do not take**, in 10 sessions: the op's name as a key (`{"text": {…}}`,
  `{"fill": {…}}`, `{"sheet": {"action": …}}`), or `"type"` where `"op"` belongs.
- **Slide CSS refused that changes nothing in the native shape:** `border-collapse` on a table, `list-style-type` on a
  list.
- **The skill is often not read.** 64 of 99 sessions read both the office skill and their document type's reference;
  21 never opened the skill's main file, and 15 opened nothing of it. The model asked for `references/docx.md` (it is
  `word.md`) and got a 404.

## Goals

1. Every failure class in the table above is fixed (passes 3/3) or shown to be the model's own behaviour and handed
   to fine-tuning.
2. The model can see the outcome of every edit in the result, in a reader's terms, so it cannot report what is not
   there.
3. No rejected call for something the model clearly meant: the rejected-call classes above go to near zero.
4. The model gets the rules and the reference for a document type whether or not it thinks to open the skill.
5. New material from scratch starts from templates good enough to hand to a user.
6. Every step is measured: the full benchmark (33 tasks × 3) after each one.

## Non-goals

- Fine-tuning. It comes after this, for what is left.
- A judge that checks the agent's final message against the document.
- Refusing terminal commands that touch a document open in the editor: a script's effects cannot be known ahead.
- Spreadsheets beyond what the evidence asks for: they already pass 26/27.
- Speed. Fewer wasted calls make sessions shorter, but time is not a target.

## Anti-goals

- **A silent repair.** Whatever the tool reads differently from what the model sent, it says so in the result.
- **A protocol that grows without evidence.** Every new op, field or check answers a failure measured above.
- **Help for the small model that hurts a frontier one**: no forced re-reads after every edit, no stripped features.
- **Two ways to do one thing.** When a form changes, the old form goes (a call using it gets an error naming the new
  one); no legacy fallback.
- **Private details in the public repository:** no hosts, buckets, keys or cluster names in docs, the benchmark or
  its README.

## The rule behind every change

The move a small model naturally makes produces the right result, and every result says what a reader of the
document will now see.

## Step 1 — read what the model meant, and show it the outcome

### Input the tool reads as meant

In `office_edit`, before any op runs, in one place:

- `ops` given as a JSON **string** is parsed. If it fails to parse only because closing braces or brackets are
  missing, they are added where the parse stopped, and the call goes ahead if the result is valid ops. Nothing else
  is guessed: not quotes, commas or keys. The result says what was added (`"read_as": "added a missing '}' at the end
  of op 0"`).
- A single op sent without the `ops` list is read as a list of one.
- An op with no `"op"` but one key that is an op name of the document's type holding an object (`{"text": {…}}`)
  is read as that op; so is `"type"` holding an op name when `"op"` is missing (but not `add_field`'s `type`, a field
  type).
- **Slide CSS** that changes nothing in the native shape (`border-collapse` on a table; `list-style-type` that a
  native bullet or number already gives) is taken, with a note in the result, not refused. Anything that would change
  the slide stays refused.
- Anything still wrong is refused with an error that says exactly what is wrong and where. Broken JSON names the
  character position and the likely missing closer. An unknown op lists the document type's ops and shows the shape
  of the nearest one.

### Results that show the outcome

- **Word:** every edit result gives, for each op, the blocks it made or changed as a reader sees them: kind, and text
  beginning with the list label (`"3. Renew the support contract"`, `"• …"`). Every Word result also gives the
  document's length: body words and pages, the editor's page count if it can give one, otherwise words ÷ 450.
- **Word outline:** a list item's `text` begins with its label as a reader sees it, so a read after an edit shows the
  numbering.
- **PDF:** an `annotate` result says which page and which words the note sits beside (with Step 2's anchor).

### Documents made in the editor, never by a script

- `office_edit`'s description and the office skill open with it: **a new document is created with `office_edit` too**
  (a missing file is made blank and opens in the user's pane); never with a script, python-docx, openpyxl,
  python-pptx, pypdf or a sandbox skill: the user can neither watch nor edit such a file.
- **A fallback skill is hidden everywhere** where its tool is offered: today `fallback_for_tools` only drops it from
  the system prompt's skill index, while `skills_list` still lists it and `skill_view` still serves it (pptx-kpi-chart
  r3 listed the skills, read `pptx` and scripted the deck). `skills_list` and `skill_view` hide it too.
- **The `pdf` skill is split.** What `office_edit` does for a PDF (filling forms, annotating, redacting, adding,
  removing and rotating pages) moves to a fallback skill with `fallback_for_tools: [office_edit]`; what it does not
  (merging, splitting, OCR, watermarks, encryption, making a PDF with content) stays in `pdf`, visible everywhere.
- **A terminal or code call that creates or changes a `.docx`, `.xlsx`, `.pptx` or `.pdf` in the workspace**, in a
  session with the office tools, gets a note in its result: the user cannot watch or co-edit a file made that way;
  make it with `office_edit`. The scripts name a `.py` file, not the document, so this is seen in the workspace after
  the call, not in the command. A command naming a document open in the editor also gets the note `read_file` already
  gets: the file can lag the editor, which is its only writer.

### Rules, references and templates that reach the model

- The **first office call for each document type in a session**, `office_read` or `office_edit`, carries the office
  skill's core rules and that type's reference, templates included (Step 3), once. 31 of the 99 sessions began with
  `office_edit`, every Word creation among them, so the first read alone would miss them. The skill stays available
  for re-reading; nothing depends on the model choosing to open it.
- The references are renamed **`docx.md`, `xlsx.md`, `pptx.md`, `pdf.md`**: the names the model guesses.
- The **tool descriptions** carry, in this order: documents are made and changed only with these tools; one complete
  and valid example call per document type (nesting shown once, right); words per page; "check with `office_read`
  before you answer". This is the part every session has before its first call.

### Skill text

- Words per page beside every length the user may ask for, and the length in each template section's intent.
- A short **"before you answer"** check in the skill and the tool description: read the outline again; every fact the
  user gave is in the document; the length is what was asked; then say what was done.

## Step 2 — close the protocol gaps

1. **A Word list item inserted next to a list continues it.** An `insert` before or after a list item, whose content
   is list items of the same kind (numbered or bulleted), joins that list and takes its numbering: the number written
   in the markdown is ignored. `"list": "new"` on the op starts a list of its own. The result shows the labels.
2. **Placeholder sections inline in the markdown.** `<?pending key: intent?>` makes a pending block where it stands.
   A new document's outline then needs no `blocks` map: two levels of braces instead of five. `blocks` stays for charts
   and pictures only; a pending block in `blocks` is refused with an error naming the inline form.
3. **A `notes` op for slides.** `{"op": "notes", "slide": id, "hash": h, "text": "…"}` sets a slide's speaker notes
   and touches nothing else; empty text clears them. A `slides` rebuild still carries notes in `<aside>`.
4. **Unreadable text is refused,** as overflow and shapes past the slide's edge already are: text whose contrast with
   what lies under it is below 3:1. The refusal names the element, both colours and one that passes. 4.5:1 stays the
   guideline in the reference.
5. **A PDF note placed by its words.** `annotate` takes `"anchor": {"find": "thirty days", "nth": 1}` instead of
   `page` and `rect`, and places the note beside the words, in the margin where one is free, without covering text,
   as the fork already does for a PDF comment's note. Stamps take an anchor too. `rect` stays for a place with no
   words to anchor to.

## Step 3 — templates for creating from scratch

Complete starting points in the office skill's bundle (`skills/productivity/office/templates/`), each ready to send
as is and to adapt. They reach the model with its document type's reference on the first office call (Step 1), so
using them does not depend on one more read: the Word, spreadsheet and deck templates whole, and the slide types for
the deck's own canvas only (16:9 or 4:3), which keeps that first result to a few thousand tokens.

- **Word:** report, letter, meeting minutes, proposal, FAQ, how-to guide. Each is a first call that plants the
  title, the headings and a pending block per section, with each intent stating what the section holds and how many
  words; then the order to fill them.
- **Spreadsheets:** budget, invoice, tracker: `set` and `format` calls with live formulas and number formats.
- **Slides:** the slide types a deck is built from (title, statement, big number, cards, table, chart, timeline,
  closing), each a valid `<section>` on 16:9 and on 4:3, following the slide reference's design rules; and three decks (pitch,
  onboarding, review) as orders of those types.

Every template is checked by the test that already checks the references' examples against their formats.

## The benchmark

`benchmarks/office_bench` was built for this investigation and is not yet committed. It lands first, so each step is
measured against the same baseline:

- committed with nothing private in it: endpoints, tokens and agent ids come from the environment;
- **overflow measured from a render** by the document server's own converter, not estimated from geometry;
- new checks: **contrast**, **every shape within the slide**, and **made in the editor**: a document the session built
  with a script fails even when it reads right (pptx-workshop r1 and xlsx-sales-table r2 passed every check today);
- the minimum lengths reviewed against what a reader of each document needs (meeting minutes at 96 words against a
  100-word floor is a threshold question, not a failure);
- harder editing tasks added after Step 3: long documents, tables in Word, many comment threads.

Each step's pull request ends with the full run (33 tasks × 3) and its report next to the previous one.

## Constraints

- runtime-office drives Euro-Office 9.3.5 editor pages; the scripts that run in the page are
  `runtime-office/editor/{word,cell,slide,pdf}.js`, the ops' formats `runtime-office/src/formats/*.ts`, the slide
  checks `runtime-office/src/slides/`. Editor fixes, if one is needed, go to our forks' `surogate/9.3.5` branch.
- `office_edit`'s schema and descriptions are in `surogates/tools/builtin/office.py`; the open-document guard in
  `surogates/office/guard.py`; the skill in `skills/productivity/office/`.
- Integration tests run against a live document server, one file at a time.
- Measured with the advisor off and the bar model as the agent's base model.

## Decided

By Flavius: the bar model and the order of work (guidance first, fine-tuning after); the advisor off for every
measurement; measuring with the real agent harness; templates for creating from scratch; the three steps above.

My calls, open to change:

- A list item next to a list joins it by default, with `"list": "new"` to opt out.
- The op is named `notes`; the inline placeholder is `<?pending key: intent?>`.
- The contrast floor is 3:1 for a refusal.
- Repairs are limited to missing closers; aliases are limited to the shapes measured.
- The first office call of each document type, read or edit, carries the rules, the reference and the templates,
  rather than a pointer to them. Rejected: refusing a document's first creating call once to hand them over first
  (it would reach even a one-call letter in time, but costs every model a call per new document); and putting every
  type's reference in the tool descriptions (every session would pay for all four).
- Fallback skills are hidden from `skills_list` and `skill_view` too, and the `pdf` skill is split so its other
  abilities stay.
- A script that makes an office document gets a note after the fact, not a refusal.
- The references are renamed after the file extensions.
- The benchmark lands as its own pull request before Step 1.

## Left to the builder

- The wording of every error, note and result field.
- The JSON repair algorithm, within the limits above.
- How contrast is measured: the background under the text, a gradient's worst point, text over a picture.
- How a terminal or code call's new or changed office documents are seen in the workspace.
- How the editor reports pages, and the fallback when it cannot.
- Each template's content, within the slide reference's design rules and the skill's writing rules.
- How the benchmark renders a document.
