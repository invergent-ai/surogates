# Word templates: a new document's first call

Start a new document from the template of its kind. Send its call with the user's path, title and subject; change,
add or drop a heading to fit the request, each section keeping its pending line and its length. When no kind fits,
build the outline the same way from the closest one's parts. Then fill one section per call, replacing its pending
block:
`{"op": "replace", "target": {"pending": "findings"}, "content": {"markdown": "…"}}`.

Each intent ends with the section's length in words, and the line under the call gives the total: about 450 words of
body text fill a page. For another length the user asks for, scale every section's words to it. Write each section to
its words, within a tenth: each result's `length` gives the document's words so far; check it before you say the
document is done. A section the user asked for but did not detail (the points to make, the steps of a process) is
drafted from common practice to its length, as a proposal. A specific they did not give (a name, date, time, figure,
place, source or contact) is left out, never made up, and so is anything said to have happened that their material does
not say: a section made of those (a meeting's decisions, a review's findings) comes out shorter, never padded. Ask only
when the document cannot be written without the answer; say in the chat what you drafted, for them to check, and what
was missing.

## Report

For findings a reader must act on: a review, an analysis, a status or research report.

```json
{"path": "reports/support-review.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {"markdown":
  "# Q3 customer support review\n\n## Summary\n<?pending summary: the answer first, then the findings from the material in one sentence each and the recommendation in one — 100 words?>\n\n## Background\n<?pending background: why this review was done, the period it covers and the data it uses, as the material gives them — 120 words?>\n\n## Findings\n<?pending findings: the three findings from the material, each led by its figure, then a table of those results and a chart of them — 350 words?>\n\n## Risks\n<?pending risks: the risks, as a list, each with what we do about it, and how likely it is where the material says — 150 words?>\n\n## Recommendation\n<?pending recommendation: what to do, with who does it, the date and the cost where the material gives them — 180 words?>"}}]}
```

Fill findings first (its table in markdown, its chart a `<?block chart?>` line with its entry in `blocks`, as in
"Fill one section" in `references/docx.md`), then background, risks and recommendation, and the summary last: about
900 words, two pages.

## Letter

For a formal letter to one reader. Write the real sender, date, recipient, the recipient's address and the subject into
this one call: only the body is left to fill. A sender, date, recipient or address the user did not give is left out,
never made up; the subject is drafted from the request; the greeting and the closing follow the names.

```json
{"path": "letters/room-booking.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {"markdown":
  "Dana Brooks, Bookings Manager, Riverside Community Centre\n\n14 October 2026\n\nPriya Shah, Chair, Eastside Walking Club\n\n18 Park Lane, Easton\n\nSubject: Your room booking from 1 November\n\nDear Ms Shah,\n\n<?pending body: why you write in the first sentence, then the details with the dates and amounts the user gave, then what you ask the reader to do and any deadline the user gave, in three short paragraphs — 170 words?>\n\nKind regards,\n\nDana Brooks"}}]}
```

Fill the body in one call: the letter comes to about 200 words, within 150–250, under a page.

## Meeting minutes

For the record of a meeting, from the user's notes or transcript.

```json
{"path": "minutes/steering-2026-10-14.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {"markdown":
  "# Website project steering group: minutes, 14 October 2026\n\n## Attendees\n<?pending attendees: a line Present, each name with its role, and a line Apologies only if the notes give any — 40 words?>\n\n## Decisions\n<?pending decisions: a numbered list, one line per decision the notes record, saying what was decided, not what was discussed — 80 words?>\n\n## Discussion\n<?pending discussion: each agenda item the notes cover, under a level-3 heading of its own, with what was raised and where it landed in two or three sentences — 250 words?>\n\n## Actions\n<?pending actions: a table with columns Action, Owner and Due date, one row per action, a cell the notes do not fill left empty — 60 words?>\n\n## Next meeting\n<?pending next: its date, time, place and items if the notes give them — 30 words?>"}}]}
```

Fill them in order from the notes, the decisions and actions as they were agreed: about 460 words, about a page.

## Proposal

To ask for a decision and a budget: a project, a purchase, a change.

```json
{"path": "proposals/booking-system.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {"markdown":
  "# Proposal: one booking system for all meeting rooms\n\n## Why\n<?pending why: what this proposal asks for in one sentence, then the problem it solves, with the figure that shows it if the user gave one — 150 words?>\n\n## Scope\n<?pending scope: what is included and what is not, as two lists — 150 words?>\n\n## Timeline\n<?pending timeline: a table with columns Phase and Deliverable, one row per phase, and a column Duration or End date only if the user gave durations or dates — 100 words?>\n\n## Cost\n<?pending cost: a table with columns Item and Cost, one row per item, each cost only where the user gave it, else left empty, and a last row Total only if they gave every cost; then how it is paid if the user said — 100 words?>\n\n## Recommendation\n<?pending recommendation: the decision asked for, who makes it and by when if given, and the first step once approved — 100 words?>"}}]}
```

Fill scope, timeline and cost first, a Total row the sum of the costs above it, then why and the recommendation: about
600 words, under a page and a half.

## FAQ

For the questions people ask about a change, a service or an event. Keep the questions that fit, in the reader's
words, one heading each.

```json
{"path": "guides/expenses-faq.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {"markdown":
  "# New expense system: questions and answers\n\n## What is changing?\n<?pending what: the change in two or three sentences, the answer first — 60 words?>\n\n## Who does it affect?\n<?pending who: the teams or people it affects as the user gave them, and anyone it does not — 50 words?>\n\n## When does it start?\n<?pending when: the start date and any date before it that matters if given, or that the date is not set yet — 40 words?>\n\n## What do I need to do?\n<?pending todo: the steps the reader takes, and by when if given, as a numbered list — 80 words?>\n\n## What happens if I do nothing?\n<?pending nothing: what happens, once it starts, to someone who has not acted — 50 words?>\n\n## Who can I ask?\n<?pending help: who answers questions and how to reach them, as the user gave them, or that this is not set yet — 50 words?>"}}]}
```

Fill them in order, each answer's first sentence the answer itself: about 330 words, under a page.

## How-to guide

For a task the reader follows step by step: a procedure, a setup, a routine.

```json
{"path": "guides/expense-claim.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {"markdown":
  "# How to submit an expense claim\n\n## Purpose\n<?pending purpose: what this guide helps the reader do, who it is for and what they need before they start — 60 words?>\n\n## Steps\n<?pending steps: a numbered list, one action per step, each starting with a verb — 200 words?>\n\n## If something goes wrong\n<?pending problems: a table with columns Problem and What to do, one row per problem the reader is likely to meet — 100 words?>\n\n## Remember\n<?pending remember: the one thing to remember, as one sentence in bold — 20 words?>"}}]}
```

Fill the steps first, then the table, the purpose and the one thing to remember: about 380 words, under a page.
