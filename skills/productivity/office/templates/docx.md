# Word templates: a new document's first call

Start a new document from the template of its kind. Send its call with the user's path, title and subject; change,
add or drop a heading to fit the request, each section keeping its pending line and its length. Then fill one section
per call, replacing its pending block:
`{"op": "replace", "target": {"pending": "findings"}, "content": {"markdown": "…"}}`.

Each intent ends with the section's length in words, and the line under the call gives the total: about 450 words of
body text fill a page. For another length the user asks for, scale every section's words to it.

## Report

For findings a reader must act on: a review, an analysis, a status or research report.

```json
{"path": "reports/support-review.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {"markdown":
  "# Q3 customer support review\n\n## Summary\n<?pending summary: the answer first, then the three findings in one sentence each and the recommendation in one — 100 words?>\n\n## Background\n<?pending background: why this review was done, the period it covers and the data it uses — 120 words?>\n\n## Findings\n<?pending findings: the three findings, each led by its figure, then a table of the results and a chart of them — 350 words?>\n\n## Risks\n<?pending risks: three risks as a list, each with how likely it is and what we do about it — 150 words?>\n\n## Recommendation\n<?pending recommendation: what to do, who does it, by when and what it costs — 180 words?>"}}]}
```

Fill findings first (its table in markdown, its chart a `<?block chart?>` line with its entry in `blocks`), then
background, risks and recommendation, and the summary last: about 900 words, two pages.

## Letter

For a formal letter to one reader. Write the real sender, date, recipient and subject into this one call: only the
body is left to fill.

```json
{"path": "letters/room-booking.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {"markdown":
  "Dana Brooks, Bookings Manager, Riverside Community Centre\n\n14 October 2026\n\nPriya Shah, Chair, Eastside Walking Club\n\nSubject: Your room booking from 1 November\n\nDear Ms Shah,\n\n<?pending body: why you write in the first sentence, then the details with dates and amounts, then what you ask the reader to do and by when, in three short paragraphs — 200 words?>\n\nKind regards,\n\nDana Brooks"}}]}
```

Fill the body in one call: the letter comes to about 230 words, within 150–250, under a page.

## Meeting minutes

For the record of a meeting, from the user's notes or transcript.

```json
{"path": "minutes/steering-2026-10-14.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {"markdown":
  "# Website project steering group: minutes, 14 October 2026\n\n## Attendees\n<?pending attendees: two lines, Present and Apologies, each name with its role — 40 words?>\n\n## Decisions\n<?pending decisions: a numbered list, one line per decision, saying what was decided, not what was discussed — 80 words?>\n\n## Discussion\n<?pending discussion: each agenda item under a level-3 heading of its own, with what was raised and where it landed in two or three sentences — 250 words?>\n\n## Actions\n<?pending actions: a table with columns Action, Owner and Due date, one row per action — 60 words?>\n\n## Next meeting\n<?pending next: the date, time and place, and the items it will take — 30 words?>"}}]}
```

Fill them in order from the notes, the decisions and actions as they were agreed: about 460 words, one page.

## Proposal

To ask for a decision and a budget: a project, a purchase, a change.

```json
{"path": "proposals/booking-system.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {"markdown":
  "# Proposal: one booking system for all meeting rooms\n\n## Why\n<?pending why: what this proposal asks for in one sentence, then the problem it solves, with the figure that shows it — 150 words?>\n\n## Scope\n<?pending scope: what is included and what is not, as two lists — 150 words?>\n\n## Timeline\n<?pending timeline: a table with columns Phase, Deliverable and End date, one row per phase — 100 words?>\n\n## Cost\n<?pending cost: a table with columns Item and Cost, its last row the total, then one sentence on how it is paid — 100 words?>\n\n## Recommendation\n<?pending recommendation: the decision asked for, who makes it and by when, and the first step once approved — 100 words?>"}}]}
```

Fill scope, timeline and cost first, the total the sum of its rows, then why and the recommendation: about 600 words,
under a page and a half.

## FAQ

For the questions people ask about a change, a service or an event. Keep the questions that fit, in the reader's
words, one heading each.

```json
{"path": "guides/expenses-faq.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {"markdown":
  "# New expense system: questions and answers\n\n## What is changing?\n<?pending what: the change in two or three sentences, the answer first — 60 words?>\n\n## Who does it affect?\n<?pending who: the teams or people it affects, and anyone it does not — 50 words?>\n\n## When does it start?\n<?pending when: the start date and any date before it that matters — 40 words?>\n\n## What do I need to do?\n<?pending todo: the steps the reader takes and by when, as a numbered list — 80 words?>\n\n## What happens if I do nothing?\n<?pending nothing: what happens on the start date to someone who has not acted — 50 words?>\n\n## Who can I ask?\n<?pending help: who answers questions, how to reach them and when — 50 words?>"}}]}
```

Fill them in order, each answer's first sentence the answer itself: about 330 words, under a page.

## How-to guide

For a task the reader follows step by step: a procedure, a setup, a routine.

```json
{"path": "guides/expense-claim.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {"markdown":
  "# How to submit an expense claim\n\n## Purpose\n<?pending purpose: what this guide helps the reader do, who it is for and what they need before they start — 60 words?>\n\n## Steps\n<?pending steps: a numbered list, one action per step, each starting with a verb — 200 words?>\n\n## If something goes wrong\n<?pending problems: a table with columns Problem and What to do, one row per common problem — 100 words?>\n\n## Remember\n<?pending remember: the one thing to remember, as one sentence in bold — 20 words?>"}}]}
```

Fill the steps first, then the table, the purpose and the one thing to remember: about 380 words, under a page.
