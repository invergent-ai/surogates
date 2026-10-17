# office_edit for Word documents: formats and examples

## Working on a Word document

- Target blocks by the `id` and `hash` the outline gives. Quote words with `find` only for a phrase inside a block.
- A new document: one call inserts the title and every section's heading, each followed by a pending block saying what
  it will hold. The user sees the whole shape at once and can delete a section before you write it. Then fill one
  section per call, replacing its pending block. A pending block the user deleted is a section they do not want: leave
  it out.
- Re-word with `replace` on quoted words; replace a whole block only when it all changes.
- Ops in one call run in order, each on what the earlier ops left: a block an earlier op replaced, moved or deleted is
  gone for the later ones (a moved block gets a new id, found in that op's result `ids`), and one it re-worded has a
  new hash.
- For a length the user asks for, count words: about 450 words of body text fill a page.

## Read

```json
{"path": "reports/Q3.docx"}
{"path": "reports/Q3.docx", "view": "outline", "start": 200}
{"path": "reports/Q3.docx", "view": "block", "target": "853"}
{"path": "reports/Q3.docx", "view": "search", "query": "revenue"}
```

The outline lists blocks as `{"id", "hash", "kind", "text"}`, where `text` is the first words only; a long document
comes in pages, continue with `start` = its `next`. Kinds include `heading1`–`heading9`, `paragraph`, `bullet`,
`numbered`, `table`, `image`, `pending` (with `key` and `intent`). Comment threads come with their `id`, a string.

The `block` view gives one block's exact text, tabs and spaces kept (a table's cells): copy the words for a
`{"find": …}` target from it. `find` searches the whole document and `nth` counts its exact matches (case and spacing
kept) from the top, from 1: quote enough words to be unique (the `search` view ignores case, so its count can differ).

## Plant a new document's outline

```json
{"path": "reports/Q3.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {
  "markdown": "# Q3 report\n\n## Summary\n<?block summary?>\n\n## Results\n<?block results?>\n\n## Risks\n<?block risks?>",
  "blocks": {
    "summary": {"type": "pending", "intent": "Three sentences: what happened this quarter and what it means"},
    "results": {"type": "pending", "intent": "Revenue and costs by region, with a chart"},
    "risks": {"type": "pending", "intent": "What could slip next quarter, and what we do about it"}
  }}}]}
```

## Fill one section

```json
{"path": "reports/Q3.docx", "ops": [{"op": "replace", "target": {"pending": "results"}, "content": {
  "markdown": "Revenue grew **12%** in the north and 8% in the east.\n\n<?block chart?>\n\n| Region | Revenue | Growth |\n|---|---|---|\n| North | 4.2M | 12% |\n| East | 3.1M | 8% |",
  "blocks": {"chart": {"type": "chart", "chart": "bar", "title": "Revenue by region",
                        "rows": [["", "Q2", "Q3"], ["North", 3.75, 4.2], ["East", 2.87, 3.1]]}}}}]}
```

A picture from the workspace: `{"type": "image", "path": "charts/funnel.png", "width": 480}` (PNG or JPEG, at most 5 MB).

## Change what is there

```json
{"op": "replace", "target": {"find": "revenue target", "nth": 1}, "content": {"text": "sales goal"}}
{"op": "insert", "target": {"block": "853", "hash": "a1b2c3d4"}, "side": "after", "content": {"markdown": "One more paragraph."}}
{"op": "replace", "target": {"block": "861", "hash": "0f9e8d7c"}, "content": {"markdown": "A rewritten paragraph."}}
{"op": "delete", "target": {"block": "870", "hash": "11aa22bb"}}
{"op": "move", "target": {"block": "880", "hash": "33cc44dd"}, "to": {"block": "853", "hash": "a1b2c3d4"}, "side": "before"}
```

A list you insert starts its own numbering. A block you replace or move keeps the comment threads anchored in it: over
the same words when they are still in it, otherwise over the whole new block.

## Comments

```json
{"op": "comment", "anchor": {"find": "grew 12%"}, "body": "Source: the regional sales export, 3 Oct."}
{"op": "reply", "thread": "4697b1d5", "body": "Updated the figure to 12.4%, from the October export."}
{"op": "resolve", "thread": "4697b1d5"}
```

`thread` is a comment's `id` from the outline, or the `thread` a `comment` op returned. The outline lists each thread's
latest replies. A reply in a thread states only the result, in one or two sentences (at most 400 characters): the
reasoning and the conversation stay in the chat.

## Refusals

`{"applied": false, "refused": {"op": 1, "reason": "…", "block": {…}}}` means nothing of the call applied, and no
co-editor saw any of it. `op` is the refused op's place in `ops`, counting from 0. `block` is how the targeted block
reads now, or null when the `reason` is something else (a block someone deleted is "gone", quoted words are no longer
there). Read again, then edit from what is there.
