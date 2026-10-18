# Office-Bench Results

All 33 tasks, three rollouts each, unless the size column says otherwise. "Model served" is the model named in every
`llm.response` of the run (`served_models` in each `checks.json`), not the tier the agent was configured with. Checks
is passed/judged: a check that comes back unknown is reported but never counted. Clean rollouts is the number of
rollouts with every judged check passed.

A row is comparable only to a row scored by the same checks. After a check changes, re-score the stored run being
compared with `obench check <run_id>` before reading a delta, and say so in the row's notes. Every counted run records
its row **and its failing classes**: the failing classes are the backlog, and a row without them is just a number.

| Date | Run | Where | Model served | Size | Checks | Clean rollouts |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-10-17 | full-001 | DEV | surogate3.7-flash | 33 × 3 | 692/724 (96%) | 76/99 |
| 2026-10-17 | full-002 | DEV | surogate3.7-flash | 33 × 3 | 711/727 (98%) | 83/99 |
| 2026-10-18 | full-003 | DEV | surogate3.7-flash | 33 × 3 | 709/722 (98%) | 89/99 |
| 2026-10-18 | full-005 | DEV | surogate3.7-flash | 36 × 3 | 787/792 (99%) | 104/108 |

**full-001** — the bar model's baseline, before any change to the protocol, the tool descriptions or the office
skill. 99 sessions, every one completed on the `studio` channel; no harness or runner error, no rollout set aside, no
`sandbox_unavailable` result. 163 minutes of sessions, 915 model calls, 933 tool calls.

| Slice | Checks | Clean rollouts |
| --- | --- | --- |
| create | 385/409 (94%) | 35/51 |
| edit | 307/315 (97%) | 41/48 |
| Word | 229/240 (95%) | 25/33 |
| spreadsheet | 181/186 (97%) | 24/27 |
| deck | 227/241 (94%) | 19/30 |
| PDF | 55/57 (96%) | 8/9 |

23 rollouts failed at least one check. By cause (a rollout can fail for more than one):

| What went wrong | Rollouts | Why |
| --- | --- | --- |
| The document built by a script, not in the editor | 9: six never called `office_read` or `office_edit` (pptx-kpi-chart r2 and r3, pptx-pitch r2, pptx-workshop r1, xlsx-sales-table r2, xlsx-dates r3); three mixed a script in (docx-report r2, xlsx-sales-table r3, pdf-annotate r3) | The model wrote python-pptx, openpyxl or pypdf scripts in the sandbox. Four of the nine fail only this check: they read right, but the user could not watch or co-edit them. docx-report r2 drew its chart with matplotlib and deleted the document from the terminal mid-edit, so the editor's save landed as a conflict copy |
| A document far shorter than asked | docx-report r2 and r3 (332 and 505 words for "two pages", 600 at least); docx-faq r3 (138 of 150), docx-guide r1 (110 of 120), docx-proposal r2 (149 of 150) | The model does not count words, and no result tells it how long the document is |
| Text unreadable against what is under it (below 3:1) | pptx-onboarding r1 and r2, pptx-pitch r1, r2 and r3 | onboarding r1 and pitch r3 have near-white text on white cards (1.0:1); all five put orange, yellow or teal accent text on white or cream (1.6 to 2.8:1). Contrast is a guideline in the slide reference that nothing checks while editing |
| Adding a third item to a numbered list gives "1. 2. 1." | docx-continue-list 3/3 | An inserted list starts its own numbering, and neither the edit result nor the outline shows list numbers, so the model reports "3." |
| Adding speaker notes moves, resizes or adds to slide 1's shapes | pptx-notes 3/3 | Notes can only be set by rebuilding the whole slide (`slides` with an `<aside>`) |
| The total cost not given as one sum | docx-proposal r2 (€55,000 one-off and €4,800 a year; the check wants 59,800) | It kept the yearly licence out of the total on purpose and told the user why |
| A chart asked for left out | docx-report r2 | Its chart calls were refused for their form (a chart block with no markdown placing it, then `ops` sent as a string); it took that to mean the editor takes a chart only when the document is made, put the chart in the chat instead and said so |
| Text running out of its box | pptx-pitch r2: the bullet lists on slide 1 and slide 6 end 17 and 18 pt below their boxes in the document server's render | A python-pptx deck whose boxes the script sized. Its only render could not be read (its image tool returned 401 on every slide), so it checked geometry with a script and then changed the layout without re-rendering |
| A shape off the slide | pptx-kpi-chart r2: the title and the chart run off the slide | A python-pptx deck on the library's default 4:3 slide (10 in wide) with boxes 12.8 in wide |
| Values typed where formulas were asked | xlsx-dates r3 | Written with openpyxl: no formula at all |
| A PDF's form fields destroyed | pdf-annotate r3 | The model read the sandbox `pdf` skill, not the office one, and rewrote the PDF with pypdf from the terminal when the editor's note did not show in the file mid-turn (the editor saves as the turn ends) |

How to read the run:

- **The bar model alone.** `surogate3.7-flash` served every model call as the agent's base tier, with the advisor off
  and one session at a time: no load, and no stronger model correcting it.
- **Scored by the checks of this row.** Text overflow is measured on the document server's own render (the geometry estimate
  is gone), contrast is checked on every deck, every deck task checks that each shape stays on its slide, every task
  checks the document was made in the editor, and meeting minutes need 80 words, not 100. The same 99 sessions scored
  by the checks they ran with read 566/594 and 74/99; that score is not comparable to this one. Of the 9 rollouts the
  old overflow estimate failed, 8 measure clean on the render; docx-minutes r2 and r3 now pass their length.
- **Unknown is not counted.** 14 checks came back unknown: the contrast of the 9 edits of the sample deck and of 2
  scripted kpi decks, whose text or background takes the theme's colours (not resolved), and the values of the 3 kpi
  charts (a picture in r1; a native chart whose values are not read in r2 and r3).
- **What the model said.** 21 of the 23 failing rollouts ended telling the user the work was done, 7 of them in the
  words of a check that failed. The other 2 said they had worked around the
  editor; pdf-annotate r3 still said its form fields were untouched.
- **Waste is in the trace, not the score.** 309 `office_edit` calls: 7 refused and 85 errors, 31 of the errors
  `"ops" is a non-empty list of ops.` in 9 sessions (mostly a JSON brace missing four or five levels deep, which the
  model server passes on as a string).
- **The image tool failed once.** `vision_analyze` answered 401 on all 6 calls of pptx-pitch r2, the only session
  in which it failed (it has since been fixed); the 5 calls of pptx-kpi-chart r3 and pdf-annotate r3, later in the
  run, worked. pitch r2's model never saw its slides.
- **One run is three samples.** A class at 3/3 (the numbered list, the speaker notes) is systematic; a class at 1/3
  needs another run before a fix is believed.

**full-002** — the same 33 tasks × 3 on the read-as-meant build, scored by the same checks as full-001's row.
`office_edit` reads what the model meant (an `ops` that is JSON text missing only its closers, a single op not in a
list, an op name used as the key) and says so in `read_as`; Word results carry `reads` (each block as written, a list
item with its `label`) and the document's `length`; the first office call on each document type carries the office
skill's rules and that type's reference (`guide`); the docx, xlsx, pptx and pdf-forms skills are hidden where the office
tools are; a terminal call that leaves office documents behind gets a note; the tool descriptions lead with making
documents in the editor; slide CSS that changes nothing is taken with a note instead of refused. 99 scored sessions,
every one completed on the `studio` channel; no harness or runner error, no `sandbox_unavailable` result. Four rollouts
were set aside as infrastructure-invalid: the document server, loaded by other work's tests at the same time, did not
save the editor's work. They were re-run on the same build, and the report lists them. 139 minutes of sessions, 724
model calls, 714 tool calls.

| Slice | Checks | Clean rollouts |
| --- | --- | --- |
| create | 398/411 (97%) | 38/51 |
| edit | 313/316 (99%) | 45/48 |
| Word | 232/240 (97%) | 25/33 |
| spreadsheet | 185/186 (99%) | 26/27 |
| deck | 237/244 (97%) | 23/30 |
| PDF | 57/57 (100%) | 9/9 |

16 rollouts failed at least one check, each for one cause:

| What went wrong | Rollouts | Why |
| --- | --- | --- |
| A document under the check's word floor | 5: docx-report r1, r2 and r3 (431, 362 and 334 words for "two pages", 600 at least); docx-guide r1 and r3 (99 and 98 of 120 for "one page") | Each result now gives the document's length, and all five stop at or past the page count asked: the reports reach two pages (r1 three) at that length, the table and the chart taking the rest |
| The yearly licence kept out of the total cost | docx-proposal 3/3: r1 and r2 give €55,000 one-off plus €4,800 a year (the check wants 59,800); r3 gives €59,800 for the first year but leaves the licence out of its cost table | As full-001's r2: the model keeps a recurring cost apart from the one-off ones on purpose. At 3/3 it is systematic, and the prompt allows the reading |
| Text unreadable against what is under it (below 3:1) | pptx-onboarding r3, pptx-pitch r2 and r3, pptx-workshop r3 | The first three the same yellow accent (F2C14E) on white, 1.7:1; workshop r3 puts its agenda times in a sky-blue accent (0EA5E9) on off-white (FBFBF8), 2.7:1. Nothing checks contrast while editing |
| Adding speaker notes moves, resizes or adds to slide 1's shapes | pptx-notes 3/3 | Unchanged: notes can only be set by rebuilding the whole slide |
| A task's name cut short | xlsx-dates r2: "Copy final" written "Copy" | The durations are formulas and right |

How to read the run:

- **Still the model alone.** `surogate3.7-flash` served every model call, one session at a time. The runtime's advisor
  expert was still offered to the model: it is switched apart from the agent's advisor, is on by default, and was
  listed in full-001 too, where it was never called. Here the model called it once, to check its draft of docx-report
  r3, and the call failed, so no advice reached the model and no stronger model corrected anything in this run.
- **Like for like.** Both rows are scored by the same checks (full-001 re-scored), so the deltas below are not a
  change of measure.
- **Unknown is not counted.** 11 checks came back unknown: the contrast of 8 edits of the sample deck (theme colours)
  and the values of the 3 kpi charts, each drawn as an SVG picture (inline, as the slide format has it, in r1 and r3;
  from a file in r2).
- **What the model said.** All 16 failing rollouts ended telling the user the work was done, 6 of them in the words of
  a check that failed; none said it had worked around the editor.
- **Four rollouts set aside and re-run.** docx-continue-list r3, xlsx-rename-sheet r3, pptx-kpi-chart r3 and
  pptx-workshop r3 ran while other work's tests loaded the same document server, and it did not save the editor's
  work: a save went unanswered, or did not come within 30 s of the turn's end. Such a rollout says nothing about the
  model, so they were set aside as infrastructure-invalid (kept, not scored; the report lists them) and re-run on the
  same build, one at a time, with nothing else on the document server. The re-runs pass every judged check but
  workshop r3's contrast.

What moved from full-001:

| | full-001 | full-002 |
| --- | --- | --- |
| Checks | 692/724 | 711/727 |
| Clean rollouts | 76/99 | 83/99 |
| Clean Word / spreadsheet / deck / PDF | 25/33, 24/27, 19/30, 8/9 | 25/33, 26/27, 23/30, 9/9 |
| `office_edit` calls | 309 | 266 |
| `office_edit` errors | 85 | 37 |
| of them `"ops" is a non-empty list of ops.` | 31 | 1 |
| "Made in the editor" failures | 9, all scripts | 0 |
| Terminal calls | 84 | 6 |
| Sessions given a `guide` | 0 | 99 |
| Sessions that opened an office reference with `skill_view` | 72 | 20 |
| Model calls / tool calls / session minutes | 915 / 933 / 163 | 724 / 714 / 139 |

- **No document made by a script.** The 6 terminal calls left only listed folders, 4 of them looking for the office
  skill's reference files, and the docx, xlsx, pptx and pdf skills went unread (8 reads in full-001). With the scripts
  went the classes only they produced: the shape off the slide, the text out of its box, values typed where formulas
  were asked, and the PDF's destroyed form fields. The chart is in every docx-report (full-001's r2 left it out).
- **Broken `ops` read as meant.** 6 calls in 5 sessions sent `ops` as JSON text short of a closing brace; the brace was
  added at the end, with a `read_as` saying so, and 4 of the calls were applied. In the other 2 the brace missing was
  the one closing the `content`, so the `"op"` after it was read inside the `content` and the call failed. Of the 37
  errors left, the most are forms not read yet: an op whose `"op"` is missing or sits inside its `content` (6), a
  spreadsheet range without its sheet (5), slide CSS the format does not take (5), JSON text broken elsewhere than its
  closers (4), a chart block with no `"type"` (3), a `<?block?>` with no entry (3) and a block target with no hash (3).
  The `border-collapse` that refused 8 slide calls in full-001 is taken with a note (7 calls in 7 sessions).
- **The numbered list is seen.** Every docx-continue-list rollout saw an inserted item come back as "1." in `reads`
  and rebuilt the list by replacing one of its items with all three, which numbers them 1. 2. 3.; all three pass. An
  inserted item still starts its own numbering, so the task takes 5 to 8 `office_edit` calls where one should do.
- **Contrast 5 → 4.** No near-white text on white this time; three of the four are one yellow accent, the fourth a
  sky-blue one.
- **Not moved, or worse.** Speaker notes stay 3/3. Short documents stay 5, though every Word result now gives the
  length: the model writes to the page count, under the checks' word floors (docx-report 3/3, was 2/3). The licence
  kept out of the total is 3/3 (was 1/3). 72 sessions still open the office skill themselves before their first office
  call, so there the `guide` repeats rules the model already has.

**full-003** — the same 33 tasks × 3 on the protocol-gaps build, scored by the same checks as full-002's row. A Word
list item inserted next to a list continues it, numbered or bulleted, at its level, unless the op says
`"list": "new"`. A pending section is written in the markdown, a line of its own `<?pending key: what it will hold?>`;
a pending entry in `blocks` is refused, and so is a line that is nearly a placeholder, which markdown used to drop
unsaid. A `notes` op sets a slide's speaker notes without rebuilding the slide. Slide text below 3:1 against what is
under it is refused while editing, with a colour to use, and for faded text the fade to drop. A PDF note or stamp can be
placed beside the words it is about (`"anchor": {"find", "nth"}`), in the first free place by them. The runtime's
advisor was switched off (`llm.advisor_enabled: false`). 99 sessions, every one completed on the `studio` channel; no
harness or runner error, no rollout set aside, no `sandbox_unavailable` result. 147 minutes of sessions, 727 model
calls, 707 tool calls.

| Slice | Checks | Clean rollouts |
| --- | --- | --- |
| create | 396/409 (97%) | 41/51 |
| edit | 313/313 (100%) | 48/48 |
| Word | 231/240 (96%) | 27/33 |
| spreadsheet | 181/184 (98%) | 24/27 |
| deck | 240/241 (100%) | 29/30 |
| PDF | 57/57 (100%) | 9/9 |

10 rollouts failed at least one check, each for one cause:

| What went wrong | Rollouts | Why |
| --- | --- | --- |
| A document under the check's word floor | 4: docx-report r1, r2 and r3 (455, 438 and 360 words for "two pages", 600 at least); docx-guide r2 (97 of 120 for "one page") | As in full-002: the model writes to the page count, and the reports reach two pages at that length, the table and the chart taking the rest. docx-guide r2 is the rollout of the pending-line refusals below |
| Dates written as DATE formulas, which the check does not count | xlsx-dates r1 and r3 | Start and end are `=DATE(2026,11,3)` and the like. Their stored values are the right November 2026 dates, in a date format, but the check counts typed dates only, so it finds none. The sheets are right |
| A turn that ended on a call written as text | docx-guide r1: the document holds its title alone (5 words), so the steps, the table, 31 March and the length all fail | Its outline came back twice as `ops is not valid JSON: Extra data`: the JSON text had one `}` too many at its end, which is not read as meant. The model blamed its newlines, wrote the title alone, then put its next call in its answer as text, which ran nothing and ended the turn |
| Amounts not all formatted as euros | xlsx-budget r1: 12 of 19 amount cells | The budget is in euros. For the pie chart it copied the seven expenses into a second table beside it and left those plain |
| A question heading written as a statement | docx-faq r1: "Why we are raising the price" | The other five headings are questions; the check wants six |
| A key fact split from its sign | pptx-pitch r2: the fee reads "+", "2" and "percent per sale" in three boxes, so "2%" is not found | After its `€` in a span with a size of its own was refused ("no sizes or fonts on a span (use two blocks)"), it put each sign and number in a block of its own and wrote the `%` out |

How to read the run:

- **The model alone, with no advisor.** `surogate3.7-flash` served every model call, one session at a time. The
  runtime's advisor was switched off for this run, so no advisor tool was offered at all, and no session's events name
  one. In full-001 and full-002 it was offered and called at most once (full-002's docx-report r3), without success.
- **Like for like.** Both rows are scored by the same checks, so the deltas below are not a change of measure.
- **Measured before the pending-line fixes.** In docx-guide r2 the model wrote a pending line with a two-word key,
  `<?pending leave types: …?>`. A key is one word, but the refusal said "A pending block is a line of its own", which
  names the wrong cause, and the model sent the line again and again: 17 refusals, 16 on that key and one on an
  `expiry` line ending in `>` without its `?`, told the same. The rollout took 41 model calls, 30 `office_edit` calls
  (20 of the run's 51 errors) and about 7 minutes, and the guide ended at 97 words, under its floor. The last three
  commits on this branch make every refusal of a pending line name its real fault and give the fixed line; they are
  not in the build this run measured. Their reach here: 2 of 99 rollouts hit a pending-line refusal, 18 refusals in
  all. The other is docx-faq r2, which sent its outline's newlines as the two characters `\n`, so the outline was one
  line; that refusal named the right cause, and its next call was applied.
- **Unknown is not counted.** 16 checks came back unknown: the contrast of 11 edits of the sample deck (theme colours;
  the speaker-notes deck is among them now, as its slide keeps its theme colours), the values of the 3 kpi charts,
  each drawn as an inline SVG picture, and the durations of xlsx-dates r1 and r3, worked out from DATE formulas the
  check cannot evaluate.
- **What the model said.** 9 of the 10 failing rollouts ended telling the user the work was done, 7 of them in the
  words of a check that failed (xlsx-dates r1 and r3 among them, whose dates are right); none said it had worked
  around the editor. docx-guide r1 ended on the call it meant to make next.
- **One run is three samples.** The list, the notes and the PDF anchor, each used the same way in 3/3, are
  systematic. Contrast at 0 is not all the refusal's work: it fired once, and the other decks wrote nothing below 3:1
  this time. The licence is in the total in 3/3 (it was out in 3/3): the model's reading of an open prompt, which
  nothing in this build touches.

What moved from full-002:

| | full-002 | full-003 |
| --- | --- | --- |
| Checks | 711/727 | 709/722 |
| Clean rollouts | 83/99 | 89/99 |
| Clean Word / spreadsheet / deck / PDF | 25/33, 26/27, 23/30, 9/9 | 27/33, 24/27, 29/30, 9/9 |
| docx-continue-list `office_edit` calls (r1, r2, r3) | 5, 8, 5 | 1, 1, 1 |
| pptx-notes passed | 0/3 | 3/3 |
| pdf-annotate notes placed by their words | 0/3 (page and rect) | 3/3 |
| Contrast failures / refused while editing | 4 / not checked | 0 / 1 |
| Under the word floor | 5 | 4, and docx-guide r1's title alone |
| Licence kept out of the total | 3/3 | 0/3 |
| Advisor offered / called | yes / once, failed | no / 0 |
| `office_edit` calls | 266 | 273 |
| `office_edit` errors | 37 | 51 |
| of them a pending line refused | — | 18, 17 in docx-guide r2 |
| `office_edit` refused: target changed or gone / no such slide or sheet / pending block gone | 3 / 1 / 5 | 2 / 5 / 0 |
| Terminal calls | 6 | 7 |
| Sessions given a `guide` (guides delivered) | 99 (102) | 99 (102) |
| Sessions that opened an office reference with `skill_view` | 20 | 26 |
| Model calls / tool calls / session minutes | 724 / 714 / 139 | 727 / 707 / 147 |

- **The numbered list in one call.** Every docx-continue-list rollout inserted "3. Renew the support contract" after
  item 2 with one `office_edit` call, and `reads` gave it back as "3."; all three pass. 4 or 5 model calls and under
  a minute each, against 10 to 16 model calls and 101 to 228 s.
- **Speaker notes 0/3 → 3/3.** Every pptx-notes rollout set the notes with one `notes` op, and slide 1's shapes are
  kept. With the slide no longer rebuilt in colours of its own, its contrast comes back unknown: the task judges 7
  checks, not 8.
- **The PDF note by its words.** Every pdf-annotate rollout gave `"anchor": {"find": "thirty days"}` and no page or
  rect; the note landed beside the line (0 pt from it), and the result said where. No "no free place" refusal. 3/3 as
  in full-002, now with 9 `office_read` calls against 14.
- **Contrast 4 → 0.** One refusal fired while editing: pptx-pitch r2 put "10 min" in E07A5F on white (2.9:1), was
  given 14213D, and its next call no longer used E07A5F; its deck's lowest is 4.9:1. No faded text was refused. No
  other deck had text below 3:1 (the lowest judged is 3.3:1), so most of the drop is this run's model, not the refusal.
- **Pending lines.** 13 rollouts wrote outlines with pending lines (16 used pending blocks in full-002); 11 went
  through with no refusal. No pending block was reported gone (5 refusals in full-002), no `<?block?>` named no entry
  (3 errors), and no call sent a pending entry in `blocks` or a line that was nearly a placeholder.
- **Errors 37 → 51, 33 without the pending lines.** The most: JSON text broken other than by closers missing at its
  end (7: five with a `}` too many or where the closing `]` belongs, one with a `]` where a `}` belongs, one with its
  quotes escaped twice), an op whose `"op"` sits inside its `content` (5, all docx-proposal r2), an `expect` not
  shaped as the range (5), a spreadsheet range without its sheet (4), slide CSS the format does not take (3) and a
  block target with no hash (3, all docx-guide r2). Of the 7 refusals, 3 add a slide `"after": "last"`, which is not
  a slide.
- **Still no scripts.** The 7 terminal calls listed folders, 4 of them looking for the office references; every
  document was made in the editor.
- **Not moved, or worse.** Short documents stay: docx-report 3/3 again, at 360 to 455 words. New this run: a question
  heading written as a statement (docx-faq r1), a turn ended on a call written as text (docx-guide r1), a chart's copy
  of the data left without euro formats (xlsx-budget r1) and the fee split from its `%` (pptx-pitch r2). The dates
  check counts typed dates only, and a DATE formula's dates are real: the check is the thing to fix, and full-003 to be
  re-scored with it before the next row is read against it.

**full-005** — the 33 tasks and three new ones, × 3, on the templates build, scored by this branch's checks. The office
skill carries templates for new documents. Word: a report, letter, minutes, proposal, FAQ and how-to, each an outline
whose pending lines end with the section's length in words. Spreadsheets: a budget, invoice and tracker, their totals
formulas. Slides: eight kinds on one palette and type scale, fitting 16:9 and 4:3 decks, their limits in characters and
lines. SKILL.md says to open the type's template before making a document, and the first office call's guide carries
the templates too. A rule on what may not be made up: specifics (names, dates, times, figures, places, sources,
contacts, what happened) come only from the user or their files; content asked for but not detailed is drafted as a
proposal, and the chat says so; the user is asked only when the work cannot be done without the answer. `ops` sent as
JSON text with closing brackets too many after it is read as meant, and `read_as` says how many were dropped. The tool
descriptions and the references agree with the templates. The build also has the pending-line refusals that name their
fault, which full-003 did not measure. The benchmark adds three harder Word edits: docx-long-edit (a figure wrong in
four places of a twelve-page report), docx-table-edit (a row added to a table and its Total row fixed) and
docx-many-threads (three of five comment threads answered and resolved; the checks read Word comments now). Checks that
failed right answers were fixed (below). The runner answers a model's question as a busy user would, at most twice a
session, and counts the asks; the report says whether each guide carried templates and whether the template was
opened before the first edit. 108 sessions, every one completed on the `studio` channel; no harness or runner error, no
rollout set aside, no `sandbox_unavailable` result. 144 minutes of sessions, 773 model calls, 764 tool calls.

| Slice | Checks | Clean rollouts |
| --- | --- | --- |
| create | 411/414 (99%) | 48/51 |
| edit | 376/378 (99%) | 56/57 |
| Word | 302/306 (99%) | 39/42 |
| spreadsheet | 185/186 (99%) | 26/27 |
| deck | 243/243 (100%) | 30/30 |
| PDF | 57/57 (100%) | 9/9 |

4 rollouts failed at least one check, each for one cause:

| What went wrong | Rollouts | Why |
| --- | --- | --- |
| A document under the check's word floor | docx-report r2 (550 words for "two pages", 600 at least) | It reached 702 words by the editor's count, then cut 144 to bring the count's three pages down to the two asked. The count stayed at three, with the chart and the table in it; r3, whose chart was dropped, fit 669 words on two |
| A chart asked for left out | docx-report r3 | Its chart went in `blocks` with no `<?block chart?>` line to place it. The editor applied the call and dropped the entry without a word. Neither the result's `reads` nor the outline showed a chart; the model saw that, reasoned that a failed chart would have been refused, and told the user the chart was there |
| A figure fixed in 2 of its 4 places | docx-long-edit r1: €45,800 left in the summary and in section 17's first paragraph | It searched for "48,500 45,800 budget", which no block holds, then worked from the outline, whose texts are cut short. It never searched for the figure alone, as r2 and r3 did, and said it had fixed "the other place" |
| A task's name cut short | xlsx-dates r1: "Copy final" written "Final" | It read "Copy" as asking it to copy the list (full-002's r2 wrote "Copy"). The dates and durations are right |

How to read the run:

- **The model alone, with no advisor.** As in full-003: `surogate3.7-flash` served every model call, one session at a
  time, and no session's events name an advisor.
- **Like for like: against full-003 re-scored, on the tasks both ran.** This branch's checks count a date a formula
  makes as a real date, find a percentage key fact written "70 percent" or in words, let the correction sentence say
  the figure was fixed or quote the old one, and let a Summary total reach the month sheets through its row's links.
  full-003's row was scored before them, so its 99 stored rollouts were copied and re-scored with this branch's checks
  (`runs/full-003-rescored/`): 714/724 and 92/99, against 709/722 and 89/99. Three verdicts changed: xlsx-dates r1 and
  r3, whose `=DATE(...)` starts and ends now count as real dates and whose durations, unknown before, are judged and
  right; and pptx-pitch r2, whose fee, written "2" and "percent per sale", now gives the 2%. The comparison below is
  on the 33 tasks the two runs share; the three new tasks are reported apart.
- **The Summary check was fixed after the first scoring, and both runs re-scored with it.** xlsx-multi-sheet asks for
  totals by formulas that point at the month sheets, and the check read only the total's own formula. full-005's r1
  and r3 give each month a Summary column of links (`=Jan!B2`, `=Feb!B2`, `=Mar!B2`) and total the row
  (`=SUM(B2:D2)`): 380, 313 and 428, by formula, from the month sheets, failed as "does not point at the month
  sheets". The check now follows the total's references through cells of its own sheet, up to three deep, and still
  fails a typed total, numbers written in the formula, or typed copies of the month values. Scored before the fix,
  full-005 read 781/792 and 102/108; the re-score changed those two rollouts' 6 checks and nothing else, in either
  run (full-003's totals point at the month sheets directly).
- **full-004 was stopped, and is not a row.** It ran the build before the never-invent rule was split, with a runner
  that did not answer questions: rollout 1 of all 36 tasks, then rollout 2 from docx-report to pptx-report-summary, 62
  of 108 rollouts, 60 of them clean. The next, pptx-onboarding r2, asked the user what the "first week" slide should
  show: the rule then said nothing is made up, and the model read it as drafting nothing the user had not spelled out.
  Nobody answers a benchmark session, so it waited out the runner's 30-minute cap, and the run was stopped. The rule
  now separates specifics, which only the user gives, from content asked for but not detailed, which is drafted as a
  proposal and said so, and the runner answers a question as a busy user would. full-005 is the run of the final
  build; full-004's 62 rollouts are kept in `runs/full-004/`.
- **Nobody asked.** 0 of 108 sessions asked the user, so the runner never had to answer. All three pptx-onboarding
  rollouts drafted the first week and said so.
- **Unknown is not counted.** 12 checks came back unknown: the contrast of the 12 edits of the sample deck (theme
  colours). The kpi charts' values are judged now (below).
- **What the model said.** All 4 failing rollouts ended telling the user the work was done, 3 of them in the words of
  a check that failed: "a two-page report" at 550 words (docx-report r2), "a column chart" (docx-report r3; the claim
  analysis missed it, reading its "didn't detail" as owning up to a problem) and €48,500 in "the other place"
  (docx-long-edit r1). docx-report r1 also said "two-page" with three pages in its `length`; no check reads pages.
- **One run is three samples.** Every failing class is at 1/3, so none is systematic on this run alone; saying the
  work is done is in every failing rollout.

What moved from full-003 re-scored, on the 33 shared tasks:

| | full-003 re-scored | full-005 |
| --- | --- | --- |
| Checks | 714/724 | 723/726 |
| Clean rollouts | 92/99 | 96/99 |
| Clean Word / spreadsheet / deck / PDF | 27/33, 26/27, 30/30, 9/9 | 31/33, 26/27, 30/30, 9/9 |
| docx-report words (r1, r2, r3) | 455, 438, 360 | 684, 550, 663 |
| docx-guide words (r1, r2, r3) | 5, 97, 150 | 170, 206, 261 |
| Under the word floor | 4, and docx-guide r1's title alone | 1 |
| Guides delivered / on a Word document, spreadsheet or deck, with templates | 102 / 0 of 93 | 101 / 92 of 92 |
| Documents made with their template opened before the first edit | 0/51 | 51/51 |
| Sessions that asked the user | 0 | 0 |
| Made documents whose answer says what was drafted or left out | 1/51 | 22/51 |
| `skill_view` calls / sessions that opened an office reference | 100 / 26 | 157 / 28 |
| `office_edit` calls | 273 | 264 |
| `office_edit` errors | 51 | 22 |
| of them a pending line refused | 18 | 1 |
| `ops` with closers too many after it: read as meant (applied) | 0 (2 refused, docx-guide r1) | 10 (9) |
| `office_edit` refused: target changed / no such slide or sheet / pending block gone | 2 / 5 / 0 | 3 / 1 / 2 |
| Terminal calls / `read_file` calls | 7 / 34 | 1 / 3 |
| Lowest judged slide contrast / refused while editing | 3.3:1 / 1 | 4.8:1 / 2 |
| Session minutes making Word / spreadsheet / deck documents | 40 / 25 / 41 | 32 / 29 / 29 |
| Model calls / tool calls / session minutes | 727 / 707 / 147 | 710 / 692 / 131 |

- **Short documents, mostly gone.** Every docx-report outline gave each section its words, 770 to 840 in all, and
  every docx-guide outline 260 to 340. The reports reached 698, 702 and 669 words by the editor's count, and two kept
  them. In full-003 all three passed 500 too (740, 501 and 559) and were then cut back to two pages, to 360 to 455; now
  only r2 cut. The guides end at 170 to 261 words, over the floor of 120 though under the template's 380. Under the
  floor: 4 and a title alone, now 1.
- **Templates carried and opened.** Every guide on a Word document, spreadsheet or deck carried its templates (101/101
  with the new tasks), and every one of the 51 documents made had its template opened with `skill_view` before its
  first `office_edit`, though the guide carries them too. The skill's files are opened with `skill_view` now:
  `read_file` on the office references fell from 26 calls to 1, and terminal calls from 7 to 1 (a `pwd; ls -la`).
- **Drafts are said.** 22 of the 51 made documents ended saying what was drafted for the user to check or what was left
  out for want of it: the onboarding deck's first week and the pitch's drafted parts in all three rollouts, the
  invoice's seller and terms in all three, the next meeting's time and place, a letter's sender address. 1 did in
  full-003.
- **Decks.** All 30 deck rollouts pass. The lowest judged contrast is 4.8:1 (3.3:1). The refusal fired twice, both on
  the template's amber (F2C14E) put on its paper (FBFBF8), 1.6:1, where the template keeps amber for navy; both decks
  end at 8.4:1 or more. The kpi charts are drawn from shapes, as the template's Chart is, so their six values are read
  and pass, where full-003's SVG pictures came back unknown. The 15 decks made took 29 minutes and 103 model calls
  (41 and 116).
- **Errors 51 → 22.** `ops` with closers too many after it was read as meant in 10 calls in 5 sessions, 9 of them
  applied; in full-003 docx-guide r1's 2 such calls were refused and its turn ended on a call written as text. One
  pending line was refused (18 in full-003): docx-report r2's outline, its newlines sent as the two characters `\n`;
  the next call was applied. Of the 22: the closers at the end of JSON text in the wrong order, `]}]}` where `]]}]`
  belongs (6, xlsx-multi-sheet r1 and r2); a sheet added after one the same call renames (3, every xlsx-multi-sheet
  rollout); an op whose `"op"` sits inside its `content` (2, docx-proposal r1); a `<?block chart?>` with no entry (2,
  docx-report r2); slide text below 3:1 (2); slide markup the format does not take (2).
- **A right answer after 52 model calls.** xlsx-multi-sheet r2 wrote its Summary formulas with the sheet names in
  capitals (`=JAN!B2+FEB!B2+MAR!B2`). Each call with them was applied and stored nothing, without a word; the model
  spent 36 calls trying formulas before it found the case. The rollout passes, in 338 s.
- **Not moved, or worse.** Spreadsheets stay 26/27: the budget's euro formats are right in all three, and a task's
  name is copied short again (xlsx-dates r1), as in full-002. And every failing rollout still ends saying the work is
  done.

The three new tasks: 64/66 checks, 8 of 9 rollouts clean, 63 model calls, 13 minutes. docx-table-edit 3/3 and
docx-many-threads 3/3, each in one `office_edit` call (many-threads r3 in three) and under 70 s. docx-long-edit 2/3:
r2 and r3 searched for "45,800", fixed all four places and searched again (13 and 15 model calls, 204 and 146 s); r1 is
above. The 3 refusals and the 1 error of these 9 rollouts are all docx-long-edit's: a `find` across a table row's cells
(2), a block an earlier op of the same call had changed (1), and the same phrase replaced four times in one call (1).

What is left, for deciding on fine-tuning. Every failure class still in full-005, and those full-003 re-scored had:

- **Keeping a length when the page count disagrees.** docx-report r2; 1/3, from 3/3 and docx-guide r2 in full-003
  re-scored. The outline set each section's words and every result gave words and pages; r2 wrote 702 words, then cut
  to the page count, as all three reports did in full-003. The template's 450 words to a page does not hold once a
  chart and a table are in, and the page count says so; keeping the words and saying the report runs onto a third
  page, or asking what to drop, is the model's call.
- **Claiming what no result showed.** docx-report r3 (a chart), docx-long-edit r1 (the figure fixed everywhere),
  docx-report r1 and r2 ("a two-page report" with three pages in `length`). Each time the answer was one look away: no
  chart in `reads` or the outline, three pages in `length`, a search for the old figure it never ran. Checking a claim
  against the last result before making it is the model's. One part of r3 is a gap in the editor: an entry in `blocks`
  that no line places is dropped without a word, where a `<?block?>` line with no entry is refused.
- **Fixing a figure everywhere it appears.** docx-long-edit r1; 1/3, a new task. The search view finds every place: r2
  and r3 searched "45,800" and found all four. r1 searched three words at once, found none, fell back on the outline's
  cut-short texts and never searched after the edit. Searching for the exact string, and again after, is the model's.
- **Copying names exactly.** xlsx-dates r1, "Copy final" written "Final"; 1/3 (full-002 1/3, full-003 0/3). A name
  that starts with a verb was read as an instruction. Taking the user's list items as written is the model's.
- **Cross-sheet formulas: gone, a check fault.** xlsx-multi-sheet r1 and r3 were right and failed by the check, which
  is fixed, and both runs are re-scored with it (above); every xlsx-multi-sheet rollout's totals come by formula from
  the month sheets. Where the model did slip, r2's capitals, the editor stored nothing without a word: a gap in the
  editor, which cost 36 calls, not a wrong answer.
- **Gone:** a question heading written as a statement (docx-faq r1 in full-003, 0/3 now; the FAQ template writes every
  heading as a question); amounts not all in the user's currency (xlsx-budget r1, 0/3; the template writes the user's
  currency into its number formats); a turn ended on a call written as text (docx-guide r1, 0; the extra `}` that
  began it is read as meant); a how-to under its floor (docx-guide r2, 0/3).
