# Office-Bench

## Description

Finds where a model fails at **creating and editing office documents** (Word, spreadsheet, slide deck, PDF) when it
runs inside the real agent harness, with every production tool and system prompt. A person's request goes into a
Surogate Studio Work chat; the agent works through `office_read` / `office_edit` (the live editor the user watches in
the file pane); the saved document is downloaded and checked automatically.

Almost half the tasks **create new material from scratch** (17 of 36), because that is what the model must do well;
the rest edit runtime-office's sample documents, or copies made from them (a twelve-page report, a table with a Total
row, five comment threads). Each task's checks read the saved file: structure (headings, lists, tables,
charts, slides, fields), values (facts carried over, formulas present and computing the right number, stored values
agreeing with them), and **nothing the task did not ask for destroyed** (the rest of a sentence, other slides, a bold
phrase, other cells, notes). Per rollout it also records what the trace shows: model calls, tool calls by name,
`office_edit` refusals and errors with their texts, whether the agent read the office skill (a `skill_view` of it, or
the guide an office call delivers), file/terminal tool use, turns, wall time, and whether the final message claims
something the checks contradict.

It is a measurement tool for the model-in-the-harness, not a unit test of the editor: the same philosophy as
`benchmarks/gaia` and `benchmarks/workspace_bench`. A task fails when the document is wrong; the trace says which call
made it wrong.

**The benchmark is a client, not a library.** It talks to the harness over its HTTP API and never imports
`surogates` or `surogate_ops` (`tests/test_isolation.py` enforces this).

### How a task runs

1. **Create a Work chat.** `POST /v1/api/sessions?agent_id=…` with `{"system": null, "config": {"ops": {"user_id",
   "username"}}, "channel": "studio"}` — what surogate-ops' `create_live_session` sends when an operator opens a Work
   chat. The channel matters: `office_read`/`office_edit` are offered only where the chat has a file pane (`web` or
   `studio`; `has_file_pane` in `surogates/harness/tool_schemas.py`). A plain `/v1/api` session is channel `api` and
   gets no office tools. The runtime reserves `channel="studio"` for the per-operator `ops-chat-*` service account ops
   mints, so the token must be that account's (any other gets HTTP 403, and the run stops at once).
2. **Upload the inputs** at the workspace root, as the file pane's upload does
   (`POST /v1/api/sessions/{id}/workspace/upload`), under the names a person would use (`quarterly-report.docx`, not
   `sample.docx`).
3. **Send the request** as the user's message; a non-2xx is an error for that rollout, never ignored (a session left
   without a `user.message` is swept later as a `recovery_loop` failure that looks like a harness hang).
4. **Stream to a terminal state** with GAIA's discipline: the server closes every stream after 300 s and a failed
   session never closes its own, so poll the status and reconnect from the last event id. The stream's `stream.live`
   markers and keepalives are not events.
   **A question to the user is answered like a busy user would.** A session that asks its user (`inbox.input_required`:
   an `ask_user_question` call, or a final reply the harness turned into one) waits for the answer, and nobody else
   gives one: unanswered, it would wait out the cap. So the runner answers the ask through the chat's answer endpoint
   (`POST /v1/api/sessions/{id}/ask_user_question/{tool_call_id}/respond`), each of its questions with *"I can't answer
   that now: do what you can with what you have, and tell me what's missing."*, for at most two asks per session. A
   third ask, or an input request with no question to answer, ends the rollout at once with the status
   `waiting_on_user`. `meta.json` records how many times the session asked (`asked_user`) and each ask (`asks`: its
   questions, whether it was answered, and why not); the report gives the count per rollout, lists the questions, and
   counts the sessions that asked.
5. **Wait for the save.** The editor saves each document the turn edited as the turn ends; the save lands in the
   workspace a few seconds *after* `session.complete` and emits no event when it works (`office.saved` is only sent
   after a save that had to wait; `office.save_failed`/`office.conflict` when one fails). So the runner downloads
   each document an `office_edit` named until two downloads 3 s apart agree (and an uploaded one differs from its
   upload), at least 8 s after completion, up to `--save-wait` (60 s). It then re-reads the stream for any late
   `office.*` events. The workspace tree's sizes are not used: a listing can report 0 bytes for a document that has
   been saved.
6. **Collect** every `.docx/.xlsx/.pptx/.pdf` in the workspace (inputs included, with whether they changed), and list
   any other file the agent wrote.
7. **Release the session** once everything is downloaded: `DELETE /v1/api/sessions/{id}` archives it and deletes its
   workspace, as deleting a chat does (`--keep-sessions` opts out). runtime-office keeps a document's page open until
   it is idle, and holds a limited number of pages (`RUNTIME_OFFICE_MAX_PAGES`); sessions left open back to back fill
   them, and the next session's `office_read` gets "The office service has as many documents open as it can hold". The
   runner deletes each session after collecting it so pages are freed: the office reaper closes an archived chat's
   documents on its next pass. A session ended as `waiting_on_user` is deleted too, which ends its question's wait.
   The events stay in the database; the documents are in `runs/`.
8. **Check** (also offline, `obench check <run_id>`): the task's checks on its document, the trace metrics, the claim
   analysis. Writes `checks.json` and `trajectory.md`. A deck whose task checks overflow is first rendered to PDF by
   the document server's own converter, x2t (`OFFICEBENCH_RENDER_SHELL`); each rendered line of text is matched to
   the text box whose text it is, and a box overflows when a line's baseline sits more than 2 pt below the box, or a
   line ends more than 2 pt right of it (descenders below the box do not count). The render is kept in `render/`, so a
   re-check measures the same PDF without the converter. A deck's contrast is read from the colours it stores: a text
   box's first run's explicit sRGB colour against the box's own solid fill, else the fill of the nearest shape drawn
   before it whose box holds its centre, else the slide's solid background, at least 3:1 (WCAG). Theme colours are not
   resolved: text using them, or sitting on a picture, table, chart or theme fill, is not judged.

### Layout

| File | Responsibility |
| --- | --- |
| `obench/client.py` | Async harness API client: sessions, upload, tree, download, messages, answers, SSE. HTTP only |
| `obench/runner.py` | One task to one rollout: create, upload, send, stream and answer, wait for the save, collect |
| `obench/docs.py` | Read a saved .docx/.xlsx/.pptx/.pdf into a plain model (styles, lists, charts, comments, shapes, fields) |
| `obench/formula.py` | Small spreadsheet formula evaluator, so checks know what a formula computes |
| `obench/render.py` | Render a deck with the document server's converter; measure text overflow on the PDF |
| `obench/checks.py` | Check primitives (factories), each returning pass / fail / unknown with a detail |
| `obench/tasks.py` | The 36 tasks: prompt, inputs, target document, checks |
| `obench/evaluate.py` | Score a stored rollout: checks, trace metrics, claim vs checks, trajectory.md |
| `obench/report.py` | Per-rollout table, questions to the user, failed checks, totals by kind and mode |
| `obench/cli.py` | `list` / `run` / `check` / `invalidate` / `report` |

Each run writes `runs/<run_id>/<task_id>/r<k>/`: `events.jsonl` (every session event), `meta.json` (session, uploads,
save wait, collected files, the questions the session asked its user), `documents/` (what was downloaded), `render/` (a
deck's PDF from the document server's converter), `checks.json`, `trajectory.md`.
`runs/` and `.env` are git-ignored (`.gitignore`). Results worth keeping are recorded in [RESULTS.md](RESULTS.md).

A check returns **pass**, **fail** or **unknown** (`None`): unknown is reported, never counted. It is used where the
benchmark cannot tell — a formula using a function `formula.py` does not implement, a file that stores no computed
values, a chart that is a picture so its values are unreadable, a number-format code the editor stored that Excel may
not read the same way (`[Euro]#,##0.00`), a deck nothing rendered (no `OFFICEBENCH_RENDER_SHELL`, the converter
failed, or no document was saved), so its overflow was not measured, a deck where no text box's contrast could be
judged (theme colours, or text on a picture or a theme fill).

"Claim vs checks": each check names the words that would claim what it verifies (`chart`, `bold`, `table`, `page`…).
A failed check whose words appear in the agent's final message is a **contradicted claim**; a run with failed checks
whose final message admits no problem is flagged **claims success despite failed checks**. Heuristic, and visible:
the report quotes the words and the check.

`tests/` (offline, no network) pins the fixture contents the edit checks rely on, builds the
correct document with python-docx/openpyxl/python-pptx/pypdf (24 of the 36 tasks) and asserts **every task check passes it**
(not "made in the editor", which a scripted file fails by design), while the untouched input **fails** every edit task. That is what makes a check trustworthy.

## How to run

### Requirements

In `benchmarks/office_bench/.env` (git-ignored, mode 600):

| Variable | What |
| --- | --- |
| `SUROGATES_SA_TOKEN` | the token of the **per-operator Work-chat account** (`ops-chat-<org>-<user>`) that ops mints when that operator first opens a Work chat. Only it may create `channel="studio"` sessions |
| `OFFICEBENCH_BASE_URL` | harness API base (default `http://localhost:8000`) |
| `OFFICEBENCH_AGENT_ID` | the agent under test |
| `OFFICEBENCH_OPS_USER_ID` / `OFFICEBENCH_OPS_USERNAME` | that operator's ops user id and username, stamped in `config.ops` as ops stamps a Work chat (the user id is the last part of the account's name) |
| `OFFICEBENCH_RENDER_SHELL` | a command prefix that runs a shell where the document server's converter, x2t, exists — e.g. `docker run -i --rm <document server image>` or `kubectl exec -i <converter pod> -c <container> --`. The deck goes to the shell's stdin and its PDF comes back on stdout (120 s at most). Unset, or when the converter fails, "no text overflows its box" is unknown and says why. Not needed to re-check a rollout whose `render/` is there |
| `OFFICEBENCH_X2T` | x2t's path in that shell (default `/var/www/euro-office/documentserver/server/FileConverter/bin/x2t`) |

The agent's model, tools and prompts are whatever ops has configured for it. The prefix is `OFFICEBENCH_` on purpose:
`SUROGATES_API_URL`/`SUROGATES_AGENT_ID` are real harness variables.

### Setup

Its own venv. Never `uv sync` at the repo root and never `uv run` while working here: either reinstalls the pinned
`surogates` wheel over the local dev install, which is the tree being measured.

```bash
cd benchmarks/office_bench
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest          # offline suite, no network
```

### Running

**One session at a time, always** (the runner has no concurrency option): a harness worker has failed under
concurrent sessions before (stale-lease recovery), and a rollout degraded by load looks exactly like a model failure.

```bash
cd benchmarks/office_bench
set -a; . ./.env; set +a
.venv/bin/obench list                                  # tasks and their checks
.venv/bin/obench run --pilot                           # the 6 pilot tasks -> runs/pilot-00N
.venv/bin/obench run --runs 3                          # THE FULL RUN: all 36 tasks x 3 -> runs/full-00N
.venv/bin/obench run --kind xlsx,pdf --mode edit       # filtered (any of --tasks/--kind/--mode/--limit) -> runs/smoke-00N
.venv/bin/obench run --tasks docx-bold,pdf-redact      # re-check specific tasks
.venv/bin/obench check <run_id>                        # re-score stored documents after changing a check
.venv/bin/obench invalidate <run_id> TASK[:rK] --reason "..."  # set an infrastructure-broken rollout aside
.venv/bin/obench report <run_id> [<run_id> ...]        # table + failed checks, over one run or several
```

A rollout that already has `checks.json` is skipped, so `obench run --run-id <id> …` resumes a run that stopped. Before
resuming, wait until the interrupted session is no longer live (one session at a time) and delete its partial rollout
folder. A rollout the infrastructure broke (the session failed on storage, a sandbox that never started misled the
agent) is set aside with `obench invalidate`: it moves to `<task_id>/invalid-r<k>-<n>/` with the reason, is left out
of every check and report, and the next `run` of that run id redoes it.
Each session is capped at `--wall-clock-cap` (1800 s); a rollout takes a few minutes. A file or terminal tool call
made while the sandbox cannot start waits out the sandbox's readiness timeout before it returns `sandbox_unavailable`;
the office tools do not need the sandbox, and the report counts these results.

## Tasks

36 tasks: 17 create, 19 edit; Word 14, spreadsheet 9, deck 10, PDF 3. Edit inputs are
`runtime-office/test/fixtures/sample.{docx,xlsx,pptx,pdf}`; `xlsx-fix-formula` derives its input from `sample.xlsx`
at staging (East's total made `=B3+B2`), and `docx-long-edit`, `docx-table-edit` and `docx-many-threads` derive theirs
from `sample.docx` (each says how; the fixture itself is never changed). Every task also has the implicit first check **document saved and opens**,
then **made in the editor** (an applied `office_edit` call named the document, and the saved file names the editor as
its producer: `docProps/app.xml`'s `<Application>`, or a PDF's `/Producer`; a file a script wrote names its library,
such as python-pptx's PowerPoint, openpyxl or pypdf), and every edit task **the document changed** (fails when the
download is byte-identical to the upload). `docx-many-threads` reads comments as OOXML stores them: each comment, a
reply included, is a `w:comment` in `word/comments.xml`; `word/commentsExtended.xml` gives each a `w15:commentEx` keyed
by the `w14:paraId` of the comment's last paragraph, where `w15:paraIdParent` names the comment a reply answers and
`w15:done="1"` marks a resolved thread; a comment's text is what lies between its `w:commentRangeStart` and
`w:commentRangeEnd` in the document.

### Word

- **`docx-report`** (create, pilot) -> `four-day-week-report.docx`
  - *"Can you write up a two-page report on our four-day week pilot in a new Word doc, four-day-week-report.docx? Here's what we have: 40 staff in 3 teams worked four days a week from March to May 2026. Output per person went up 6%, sick days dropped from 31 to 18 over the three months, and customer satisfaction held at 4.4 out of 5. Overtime costs fell by €12,400. Two things went badly: clients who call on Fridays couldn't reach anyone, and handovers between the teams got messy. We want to recommend rolling it out to all 120 staff from September, with a Friday on-call rota. Please use proper headings, put the problems in a bulleted list, add a table comparing before and after, and a chart of sick days before vs after."*
  - Checks: at least 3 headings (heading styles); length 600-1400 words; a bulleted list of at least 2 items; a table of at least 2x2 holding [31, 18]; a native chart; mentions 8 key facts; no unfilled pending block.
- **`docx-reword`** (edit, pilot) -> `quarterly-report.docx`; input `quarterly-report.docx` (from sample.docx)
  - *"In quarterly-report.docx, the sentence 'North grew fastest, then the east.' is the wrong way round - the east grew fastest, then the north. Can you fix it? Don't change anything else."*
  - Checks: the sentence now puts the east first; the rest of the document is kept; no paragraph added or removed; 'revenue target' is bold.
- **`docx-letter`** (create) -> `lease-termination-letter.docx`
  - *"Write a letter to our landlord, Mr Daniel Costa of Costa Properties, Rua Augusta 112, Lisbon, saying we're ending the lease on our office at Rua do Ouro 45 on 31 January 2027, as the contract's 90-day notice requires. Ask him to arrange the final inspection in the week of 18 January and to return our €4,500 deposit to the account he has on file. It's from Ana Ribeiro, Operations Manager, Northwind Lda. Save it as lease-termination-letter.docx."*
  - Checks: mentions 10 key facts; opens with a salutation; closes with a sign-off; signed by Ana Ribeiro at the end; length 80-450 words; no table or chart in a letter; no unfilled pending block.
- **`docx-minutes`** (create) -> `minutes-2026-10-14.docx`
  - *"Can you turn my notes into proper minutes in minutes-2026-10-14.docx? Product sync, 14 Oct. There: Maria (PM), Jon (eng), Priya (design), Tom (sales). Decided: ship v2.3 on 28 Oct, and drop the dark-mode toggle from this release. Discussed: onboarding drop-off is 38% at step 2, Priya will mock up a shorter flow. Actions: Jon fixes the export bug by 21 Oct, Priya shares the onboarding mockups by 18 Oct, Tom tells the 3 pilot customers about the date by 16 Oct. Next meeting 21 Oct."*
  - Checks: at least 2 headings (heading styles); mentions 9 key facts; each action item carries its owner and due date; length 80-600 words; no unfilled pending block.
- **`docx-guide`** (create) -> `time-off-guide.docx`
  - *"Write a one-page guide for staff on how to request time off, time-off-guide.docx. The steps: submit the request in the HR portal at least 10 working days ahead; your manager approves or declines within 3 working days; HR confirms by email; then add it to the team calendar. Include a table of the leave types: annual 25 days, sick 10, parental 20, unpaid up to 30. And make it stand out that unused annual leave over 5 days expires on 31 March."*
  - Checks: at least 1 headings (heading styles); a numbered list of at least 4 items; a table of at least 4x2 holding ['annual', 25, 'sick', 10, 'parental', 20, 'unpaid', 30]; '31 March' stands out (bold or a heading); length 120-650 words; no unfilled pending block.
- **`docx-proposal`** (create) -> `website-proposal.docx`
  - *"Draft a one-page proposal for the board to redo our website, website-proposal.docx. Background: the site is 6 years old and 64% of visits are on mobile, where it's slow. Scope: a new design, moving to a new CMS, and fixing accessibility. Timeline: discovery 2 weeks, design 4 weeks, build 6 weeks, launch 1 week. Costs: design €18,000, build €32,000, CMS licences €4,800 a year, contingency €5,000. Put the timeline and the costs in tables, and give the total cost."*
  - Checks: at least 3 headings (heading styles); a table of at least 2x2; a table of at least 4x2 holding ['discovery', 'design', 'build', 'launch']; a table of at least 4x2 holding [18000, 32000, 4800, 5000, 59800]; mentions 2 key facts; length 150-650 words; no unfilled pending block.
- **`docx-faq`** (create) -> `pricing-faq.docx`
  - *"We're raising the Basic plan from €9 to €12 a month on 1 January. Write a customer FAQ, pricing-faq.docx, with six questions: why we're doing it, when it happens, what existing customers pay (they keep €9 until 1 April), whether features change (no - and we're adding two-factor login and daily backups), how to cancel (Settings > Billing, no fee), and who to contact (billing@shelfie.example). Each question as a heading with a short answer."*
  - Checks: at least 6 questions as headings; mentions 8 key facts; length 150-800 words; no unfilled pending block.
- **`docx-add-section`** (edit) -> `quarterly-report.docx`; input `quarterly-report.docx` (from sample.docx)
  - *"Add a section called 'Next steps' to the end of quarterly-report.docx, after Risks, with two or three sentences on what we'll do next quarter."*
  - Checks: a 'Next steps' section after 'Hiring could slip a quarter.'; 'Next steps' at the other sections' level (heading 2); the rest of the document is kept; 'revenue target' is bold; no unfilled pending block.
- **`docx-fix-table`** (edit) -> `quarterly-report.docx`; input `quarterly-report.docx` (from sample.docx)
  - *"The growth figure for North in the table in quarterly-report.docx is wrong - it should be 14%, not 12%."*
  - Checks: the table reads [['Region', 'Growth'], ['North', '14%']]; '12%' no longer appears; table style kept; the rest of the document is kept.
- **`docx-continue-list`** (edit) -> `quarterly-report.docx`; input `quarterly-report.docx` (from sample.docx)
  - *"Add 'Renew the support contract' as a third item on the numbered list in quarterly-report.docx."*
  - Checks: the numbered list continues with 'Renew the support contract'; the rest of the document is kept; a bulleted list of at least 2 items.
- **`docx-bold`** (edit) -> `quarterly-report.docx`; input `quarterly-report.docx` (from sample.docx)
  - *"In quarterly-report.docx, make 'Hiring could slip a quarter' bold."*
  - Checks: 'Hiring could slip a quarter' is bold; 'revenue target' is bold; 'This report covers the quarter' is not bold; the rest of the document is kept; no paragraph added or removed.
- **`docx-long-edit`** (edit) -> `operations-report.docx`; input `operations-report.docx` (from sample.docx, derived)
  - *"In section 17 of operations-report.docx the budget is wrong: it is €48,500, not €45,800. Fix it everywhere it appears, and add one sentence after the section's table saying the figure was corrected on 14 October."*
  - Input: sample.docx rebuilt as a twelve-page operations report in its styles, twenty numbered sections; €45,800 is in the summary, section 17's text and table, and the approvals.
  - Checks: '45,800' no longer appears anywhere except in the correction sentence; 48,500 stands in every place 45,800 was; a sentence right after section 17's table says it was corrected on 14 October; the rest of the document is kept; one paragraph added, none removed.
- **`docx-table-edit`** (edit) -> `quarterly-report.docx`; input `quarterly-report.docx` (from sample.docx, derived)
  - *"Add a row for South (Q1 70, Q2 65) to the table in quarterly-report.docx and fix its Total row."*
  - Input: sample.docx with its table made Region, Q1, Q2: North 120, 140; East 90, 100; Total 210, 240.
  - Checks: a South row reading 70 and 65 above the Total row; the Total row reads 280 and 305; the header and the North and East rows are kept; table style kept; the rest of the document is kept.
- **`docx-many-threads`** (edit) -> `quarterly-report.docx`; input `quarterly-report.docx` (from sample.docx, derived)
  - *"Reply to the three questions in the comments on quarterly-report.docx and resolve them; leave the other two."*
  - Input: sample.docx with five open comment threads: three questions its text answers (engineers to hire: two; North's growth: 12%; the new office: Lisbon) and two notes that ask nothing.
  - Checks: the reply to 'How many engineers do we plan to hire?' says two; the reply to 'How much did North grow this quarter?' says 12%; the reply to 'Which city is the new office in?' says Lisbon; the three questions are resolved; the two notes are left open, unanswered and as they were; the rest of the document is kept.

### Spreadsheet

- **`xlsx-budget`** (create, pilot) -> `budget.xlsx`
  - *"Make me a monthly household budget in budget.xlsx. Income: salary 3,200 and freelance 650. Expenses: rent 1,150, groceries 420, utilities 180, transport 140, insurance 95, savings 500, fun 200. I want totals done with formulas, a line showing what's left at the end of the month, the money formatted as euros, and a pie chart of the expenses."*
  - Checks: the 9 given figures are typed in as numbers; a formula computes total income = 3850; a formula computes total expenses = 2685; a formula computes left over = 1165; amounts formatted as euros; doughnut/pie chart from the data; stored values match what the formulas compute.
- **`xlsx-sales-table`** (create) -> `sales-2026.xlsx`
  - *"Put this sales data into sales-2026.xlsx - city, units sold, revenue: Lisbon 412, €48,200; Porto 530, €61,750; Madrid 351, €39,900; Barcelona 618, €72,300; Valencia 160, €18,450; Seville 233, €25,600. Sort it by revenue, highest first, make the header row bold, and show revenue with thousands separators."*
  - Checks: six rows with their units and revenue as numbers; sorted by revenue, highest first; header row bold; amounts formatted as thousands separators.
- **`xlsx-invoice`** (create) -> `invoice-1043.xlsx`
  - *"Make an invoice in invoice-1043.xlsx for Acme Corp, invoice number 1043: website redesign, 1 at 4,800; hosting, 12 months at 35; support, 18 hours at 85. Work out each line total, the subtotal, VAT at 23% and the total due, all with formulas."*
  - Checks: mentions ['Acme', 1043]; a formula computes redesign line total = 4800; a formula computes hosting line total = 420; a formula computes support line total = 1530; a formula computes subtotal = 6750; a formula computes VAT = 1552.5; a formula computes total due = 8302.5; stored values match what the formulas compute.
- **`xlsx-multi-sheet`** (create) -> `team-hours.xlsx`
  - *"Set up team-hours.xlsx with one sheet per month - Jan, Feb, Mar - listing hours per person (Ana: 120, 132, 128; Ben: 98, 110, 105; Carla: 140, 138, 150), and a Summary sheet that adds up each person's three months with formulas that point at the month sheets."*
  - Checks: sheets ['Jan', 'Feb', 'Mar', 'Summary']; Jan holds [120, 98, 140]; Feb holds [132, 110, 138]; Mar holds [128, 105, 150]; Ana's total = 380 by formula on Summary; Ben's total = 313 by formula on Summary; Carla's total = 428 by formula on Summary; stored values match what the formulas compute.
- **`xlsx-line-chart`** (create) -> `web-traffic.xlsx`
  - *"In a new web-traffic.xlsx, put our weekly visitors for the last 8 weeks - 1,200, 1,350, 1,410, 1,380, 1,620, 1,750, 1,900, 2,050 - and add a line chart of it called 'Weekly visitors'."*
  - Checks: the 8 given figures are typed in as numbers; line chart titled 'Weekly visitors' from the data; the chart covers all 8 points.
- **`xlsx-dates`** (create) -> `launch-plan.xlsx`
  - *"Make a launch plan in launch-plan.xlsx with task, owner, start date, end date, and a column that works out how many days each task takes. Copy final - Inês - 3 to 7 Nov; Landing page - Rui - 5 to 14 Nov; Email campaign - Ana - 10 to 21 Nov; Press kit - Inês - 12 to 19 Nov; Launch event - Marta - 24 Nov. All in 2026."*
  - Checks: mentions ['Copy final', 'Landing page', 'Email campaign', 'Press kit', 'Launch event', 'Inês', 'Rui', 'Ana', 'Marta']; start and end are real dates (9+ in Nov 2026); durations computed by formula; stored values match what the formulas compute.
- **`xlsx-add-column`** (edit) -> `sales.xlsx`; input `sales.xlsx` (from sample.xlsx)
  - *"In sales.xlsx, add a Growth column to the Sales sheet showing how much Q2 grew over Q1 for each region, as a percentage, with formulas."*
  - Checks: a Growth column of formulas with the right percentages; the existing table is kept; the Notes sheet is kept; stored values match what the formulas compute.
- **`xlsx-fix-formula`** (edit) -> `sales.xlsx`; input `sales.xlsx` (from sample.xlsx, derived)
  - *"The East total in sales.xlsx looks wrong to me - can you fix it?"*
  - Input: sample.xlsx with D3 changed to =B3+B2 (210 instead of 200).
  - Checks: East's total is =B3+C3 (200); the other cells are kept; the Notes sheet is kept.
- **`xlsx-rename-sheet`** (edit) -> `sales.xlsx`; input `sales.xlsx` (from sample.xlsx)
  - *"Rename the Notes sheet in sales.xlsx to 'Read me'."*
  - Checks: sheets are ['Sales', 'Read me']; Read me!A1 kept; the Sales sheet is kept.

### Deck

- **`pptx-pitch`** (create, pilot) -> `shelfie-pitch.pptx`
  - *"I need a 6-slide pitch deck, shelfie-pitch.pptx, for Shelfie - an app that gets a small bookshop selling its stock online in 10 minutes. Problem: there are 2,300 independent bookshops in Portugal and Spain and 70% of them don't sell online. Then the solution, how it works, the business model (€29 a month plus 2% per sale), traction (85 shops in the beta, €41k of sales in 3 months), and the ask: we're raising €600k for 18 months."*
  - Checks: 5-7 slides; mentions 8 key facts; no empty slide; no text overflows its box; every shape inside the slide; text readable against what is under it.
- **`pptx-add-bullet`** (edit, pilot) -> `quarterly-review.pptx`; input `quarterly-review.pptx` (from sample.pptx)
  - *"Add 'Costs came in 4% under budget' as another bullet on the first slide of quarterly-review.pptx."*
  - Checks: the bullet joins the slide's bullet list; slide 1 title kept; slide 2 unchanged; 2 slides; no text overflows its box; every shape inside the slide; text readable against what is under it.
- **`pptx-report-summary`** (create) -> `report-summary.pptx`; input `quarterly-report.docx` (from sample.docx)
  - *"Turn quarterly-report.docx into a 3-slide summary deck, report-summary.pptx."*
  - Checks: 3 slides; mentions 5 key facts; no empty slide; no text overflows its box; every shape inside the slide; quarterly-report.docx left as it was; text readable against what is under it.
- **`pptx-onboarding`** (create) -> `support-onboarding.pptx`
  - *"Make a 5-slide onboarding deck for new support staff, support-onboarding.pptx: a welcome slide; our three support tiers with their response targets (Tier 1: 1 hour, Tier 2: 4 hours, Tier 3: 24 hours) as a table; the tools we use (Zendesk, Slack, Notion); what the first week looks like; and who to ask (Marta, team lead; Rui, tooling)."*
  - Checks: 5 slides; a native table holding ['tier', 1, 4, 24]; mentions 5 key facts; no empty slide; no text overflows its box; every shape inside the slide; text readable against what is under it.
- **`pptx-kpi-chart`** (create) -> `kpi.pptx`
  - *"Create kpi.pptx: one slide titled 'Revenue is up 50% since January' with a chart of monthly revenue from Jan to Jun: 42, 45, 51, 49, 58, 63 (thousand euros)."*
  - Checks: 1 slides; the title says it; a chart on slide 1; the chart carries the six monthly values; no text overflows its box; every shape inside the slide; text readable against what is under it.
- **`pptx-workshop`** (create) -> `workshop.pptx`
  - *"Make workshop.pptx with a title slide, 'Pricing workshop - 12 November', and an agenda slide: 09:00 Welcome; 09:15 Where our prices are today; 10:00 Competitor review; 10:45 Break; 11:00 Three pricing options; 12:00 Vote and next steps; 12:30 Close."*
  - Checks: 2 slides; title slide; agenda slide has every item and time; no text overflows its box; every shape inside the slide; text readable against what is under it.
- **`pptx-change-title`** (edit) -> `quarterly-review.pptx`; input `quarterly-review.pptx` (from sample.pptx)
  - *"Change the title of the second slide in quarterly-review.pptx to 'Q3 plan'."*
  - Checks: slide 2 title is 'Q3 plan'; the rest of slide 2 is kept; slide 1 unchanged; 2 slides; every shape inside the slide; text readable against what is under it.
- **`pptx-reorder`** (edit) -> `quarterly-review.pptx`; input `quarterly-review.pptx` (from sample.pptx)
  - *"In quarterly-review.pptx, move the 'Next quarter' slide to the front."*
  - Checks: slides in the order ['Next quarter', 'Quarterly review']; the moved slide is intact (notes too); the other slide is intact; 2 slides; every shape inside the slide; text readable against what is under it.
- **`pptx-table-slide`** (edit) -> `quarterly-review.pptx`; input `quarterly-review.pptx` (from sample.pptx)
  - *"Add a slide at the end of quarterly-review.pptx with a table of Q1 and Q2 revenue by region: North 120 and 140, East 90 and 110, South 70 and 65."*
  - Checks: 3 slides; a native table on slide 3 holding ['North', 'East', 'South', 120, 140, 90, 110, 70, 65]; slide 1 unchanged; slide 2 unchanged; no text overflows its box; every shape inside the slide; text readable against what is under it.
- **`pptx-notes`** (edit) -> `quarterly-review.pptx`; input `quarterly-review.pptx` (from sample.pptx)
  - *"Add speaker notes to the first slide of quarterly-review.pptx: 'Thank the regional leads. Hiring slipped because two offers were declined.'"*
  - Checks: slide 1 speaker notes say it; slide 1's visible content is kept; slide 2 unchanged; 2 slides; every shape inside the slide; text readable against what is under it.

### PDF

- **`pdf-fill`** (edit, pilot) -> `service-agreement.pdf`; input `service-agreement.pdf` (from sample.pdf)
  - *"Please fill in service-agreement.pdf: the client is Northwind Lda, and tick the Signed box."*
  - Checks: client field says Northwind Lda; signed box ticked; 2 pages; the rest of the text is kept; form fields ['client', 'signed'] still there.
- **`pdf-annotate`** (edit) -> `service-agreement.pdf`; input `service-agreement.pdf` (from sample.pdf)
  - *"Put a note next to the 'thirty days' line in service-agreement.pdf saying: Finance wants 45 days, check with legal."*
  - Checks: a note saying '45 days' by 'thirty days'; 2 pages; the rest of the text is kept; form fields ['client', 'signed'] still there.
- **`pdf-redact`** (edit) -> `service-agreement.pdf`; input `service-agreement.pdf` (from sample.pdf)
  - *"Before I send service-agreement.pdf out, redact the start date - 'from 1 March 2026'."*
  - Checks: '1 March 2026' is gone from the text; the rest of the text is kept; 2 pages; form fields ['client', 'signed'] still there.

## Discipline

- **Failed checks are the product.** Each says what the document lacks or what an edit destroyed; read the
  rollout's `trajectory.md` before attributing it.
- **Single runs are noisy.** Compare means of ≥3 runs (`--runs 3`); `--tasks` is a fast filter, never the basis for a
  claim. One config change per run.
- **Pin the model.** The served model is in every `llm.response` (`served_models` in `checks.json`, "Run health" in
  the report); check it before attributing a delta to the harness.
- **Checks can be wrong too.** A new check gets a done-right test in `tests/test_tasks.py` before it counts; re-score
  old runs with `obench check`.
- **Never delete run folders**: the run-id sequence takes the highest existing number per prefix.
