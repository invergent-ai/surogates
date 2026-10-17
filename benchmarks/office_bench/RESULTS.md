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
