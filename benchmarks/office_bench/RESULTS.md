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
| Text unreadable against what is under it (below 3:1) | pptx-onboarding r1 and r2, pptx-pitch r1, r2 and r3 | onboarding r1 and pitch r3 have near-white text on white cards (1.0:1); all five put orange, yellow or teal accent text on white or cream (1.7 to 2.8:1). Contrast is a guideline in the slide reference that nothing checks while editing |
| Adding a third item to a numbered list gives "1. 2. 1." | docx-continue-list 3/3 | An inserted list starts its own numbering, and neither the edit result nor the outline shows list numbers, so the model reports "3." |
| Adding speaker notes moves, resizes or adds to slide 1's shapes | pptx-notes 3/3 | Notes can only be set by rebuilding the whole slide (`slides` with an `<aside>`) |
| Something asked for left out | docx-proposal r2 (the total cost), docx-report r2 (the chart) | No check against the request before answering |
| Text running out of its box | pptx-pitch r2: the bullet lists on slide 1 and slide 6 end 17 and 18 pt below their boxes in the document server's render | A python-pptx deck whose boxes the script sized; the model last looked at it in a LibreOffice render made before its final edits |
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
- **Waste is in the trace, not the score.** 309 `office_edit` calls: 7 refused and 89 errors, 31 of the errors
  `"ops" is a non-empty list of ops.` in 9 sessions (mostly a JSON brace missing four or five levels deep, which the
  model server passes on as a string).
- **One run is three samples.** A class at 3/3 (the numbered list, the speaker notes) is systematic; a class at 1/3
  needs another run before a fix is believed.
