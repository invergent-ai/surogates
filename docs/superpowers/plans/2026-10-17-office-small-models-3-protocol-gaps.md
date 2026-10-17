# Office Protocol Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the five protocol gaps the benchmark found: a Word list item continues its list, placeholder sections are written inline, slides get a notes-only op, unreadable slide text is refused, and a PDF note is placed by its words.

**Architecture:** Each gap is closed where its op is checked (`runtime-office/src/formats/*.ts`, `src/markdown.ts`), applied (the in-page scripts `runtime-office/editor/*.js`) or measured (`src/slides/inspect.ts`), and taught in the tool description (`surogates/tools/builtin/office.py`), the op table (`surogates/office/ops_input.py`) and the references (`skills/productivity/office/references/{docx,pptx,pdf}.md`).

**Tech Stack:** TypeScript and in-page JavaScript (vitest, Playwright Chromium), Euro-Office builder API, Python 3.12.

**Spec:** `docs/superpowers/specs/2026-10-17-office-editing-for-small-models-design.md` ("Step 2")

## Global Constraints

- A new worktree `.worktrees/office-protocol-gaps` on branch `feat/office-protocol-gaps` from `origin/master` once the read-as-meant PR has merged. Never work in the main checkout itself.
- Conventional Commits; `git add` named files only; no Co-Authored-By trailer; never mention plans, tasks or reviews in code, comments or commit messages.
- Python tests: the main checkout's `.venv/bin/python -m pytest <files> -q -p no:cacheprovider`, never `uv run`.
- Node only via the machine's nvm install of Node 26 on `PATH`; `npm ci` once; unit tests `npx vitest run <file>`; integration files one at a time with `OFFICE_IT_DS_URL`, `OFFICE_IT_HOST`, `OFFICE_IT_JWT_SECRET` (never print the secret); let pages finish loading pictures before closing them.
- The fixtures in `runtime-office/test/fixtures/` stay byte-identical: the benchmark's edit tasks read them.
- **No legacy fallback:** a pending block in `blocks` is refused with an error naming the inline form, not accepted beside it.
- Every new op or field is taught in three places at once: the tool description, `OP_NAMES`/`OP_SHAPES`, and the reference with a valid example (the skill-examples test checks the examples).
- **Public repository:** no hosts, IPs, buckets or keys anywhere committed.
- Ask Flavius before restarting DEV processes, re-seeding DEV's skills, merging or releasing. Every subagent uses model `opus`.

## Review Focus

1. A bullet item inserted next to a numbered list: a new bulleted list, never joined to the numbered one. Test in Task 1.
2. A nested item (markdown indented one level) inserted after a top-level item: joins the same list one level down. Test in Task 1.
3. A `notes` op and a `slides` rebuild of the same slide in one call: refused, as two changes to one slide. Test in Task 3.
4. Text on a translucent card over a dark band: the card is composited over the band before the ratio is taken, so readable text is not refused. Test in Task 4.
5. An anchor's `nth` beyond the matches: refused, saying how many there are. Test in Task 5.

---

### Task 1: A Word list item continues its list

**Files:**
- Modify: `runtime-office/src/ops.ts:9-17,30-31` (insert's `list`), `runtime-office/editor/word.js` (`build` 338-358, the insert edit 468-473), `skills/productivity/office/references/docx.md` (the sentence "A list you insert starts its own numbering", plus an example), `surogates/tools/builtin/office.py` (EDIT_DESCRIPTION's Word line)
- Test: `runtime-office/test/ops.test.ts`, `runtime-office/test/word-edit.it.test.ts`

**Interfaces:**
- Produces: insert takes `"list": "new"` (compiled as `list: "new"`); any other value is refused with `"list" is "new" (a list of its own) or left out.` In word.js, an insert whose target block is a list item of the same kind (numbered or bulleted, as `kindOf`) as the content's first list joins it: that list's items take `target.GetNumbering()`'s numbering, at the target's level plus their own, capped at 8; `list: "new"` skips the join.

- [ ] **Step 1: Write the failing tests.**
  - `ops.test.ts`: `{"op": "insert", "target": {"end": true}, "content": {"markdown": "- a"}, "list": "new"}` compiles with `list: "new"`; `"list": "old"` is refused with the message above.
  - `word-edit.it.test.ts` on `sample.docx`:
    - `continues a numbered list`: insert `"3. Renew the support contract"` after the second numbered item; `results[0].reads[0].text` starts `"3."`; the outline's numbered items read `"1."`, `"2."`, `"3."`; the first two keep their hashes.
    - `starts a list on request`: the same with `"list": "new"` reads `"1."`.
    - `keeps bullets apart from numbers`: `"- An extra point"` after the second numbered item reads `"•"`.
    - `joins one level down`: `"  1. A sub-step"` (indented) after the first numbered item reads as level 1 of the same list (its `GetNumbering().GetLevelIndex()` is 1 and the first item still reads `"1."`).
- [ ] **Step 2: Run them** (`npx vitest run test/ops.test.ts`, then the IT file alone) — Expected: FAIL.
- [ ] **Step 3: Implement.** The join needs the target's numbering object: `target.GetNumbering()` is the target's `ApiNumberingLevel`; take its numbering (`.GetNumbering()`) and `.GetLevel(level)` for the items, as the connector verify notes record `SetNumbering(existingPara.GetNumbering())` continuing a list. The number written in the markdown is ignored.
- [ ] **Step 4: Run them** — Expected: PASS. Then `npx vitest run test/skill-examples.test.ts`.
- [ ] **Step 5: Commit** — `git commit -m "feat(runtime-office): a list item inserted next to a list continues it"`

### Task 2: Placeholder sections inline in the markdown

**Files:**
- Modify: `runtime-office/src/markdown.ts:28,37-61,179-201`, `skills/productivity/office/references/docx.md` (the outline and fill examples), `surogates/tools/builtin/office.py` (EDIT_DESCRIPTION's Word content line and its Word example), `skills/productivity/office/SKILL.md` (Word documents)
- Test: `runtime-office/test/markdown.test.ts`, `runtime-office/test/word-edit.it.test.ts`, `tests/test_office_descriptions.py`

**Interfaces:**
- Produces: a line `<?pending key: intent?>` (`/^<\?pending\s+([A-Za-z0-9_-]+)\s*:\s*(.+?)\s*\?>$/`, alone on its line) compiles to `{t: "pending", key, intent}` with the intent trimmed and cut at 300 characters. A key used twice in one op: `<?pending KEY: …?> is used twice in one op.` A `blocks` entry of type `pending`: ``Pending blocks are written in the markdown now: <?pending KEY: what it will hold?>.`` `<?block k?>` stays for charts and pictures.

- [ ] **Step 1: Write the failing tests.**
  - `markdown.test.ts`: `"# Q3\n\n## Summary\n<?pending summary: Three sentences on the quarter?>"` gives a heading and a pending block `{key: "summary", intent: "Three sentences on the quarter"}`; an intent of 400 characters is cut to 300; a repeated key and a `blocks` pending are refused with the messages above.
  - `word-edit.it.test.ts`: the existing pending test plants its outline inline, then fills `{"pending": "<key>"}` as before.
  - `tests/test_office_descriptions.py`: the Word example in EDIT_DESCRIPTION uses `<?pending` and no `"type": "pending"`.
- [ ] **Step 2: Run them** — Expected: FAIL.
- [ ] **Step 3: Implement**, and rewrite docx.md's "Plant a new document's outline" example inline (two levels of braces).
- [ ] **Step 4: Run them** plus `npx vitest run test/skill-examples.test.ts` — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(runtime-office): write placeholder sections inline in the markdown"`

### Task 3: A notes op for slides

**Files:**
- Modify: `runtime-office/src/formats/slide.ts:13-18,50-60`, `runtime-office/editor/slide.js` (`check` 672-697, `edit` 711-745, `emptyTemplate` 704), `surogates/office/ops_input.py` (`OP_NAMES["pptx"]`, `OP_SHAPES`), `surogates/tools/builtin/office.py` (Deck line), `skills/productivity/office/references/pptx.md` (Notes)
- Test: `runtime-office/test/formats-slide.test.ts`, `runtime-office/test/slide-script.test.ts`, `runtime-office/test/slide.it.test.ts`, `tests/test_office_ops_input.py`

**Interfaces:**
- Produces: `{"op": "notes", "slide": "<id>", "hash": "<hash>", "text": "…"}`. Checks: no hash, ``a notes change carries the hash its slide had when you read it.``; text not a string, `"text" is the slide's speaker notes, a line per paragraph.`; over 5000 characters, `"text" is at most 5000 characters.` The unknown-op message lists `slides, text, notes, order, remove, reply or resolve`. Applied: the notes body is emptied (`GetNotesPage().GetBodyShape().GetDocContent().RemoveAllElements()` when there are notes), then each line of `text` becomes a paragraph; empty text clears them; no shape changes. Result `{op, notes: <text>}`. A `notes` op on a slide that the same call rebuilds or removes is refused: `Slide X is rebuilt by this call: give its notes in the rebuild's <aside>.` / the existing removed message.

- [ ] **Step 1: Write the failing tests.**
  - `formats-slide.test.ts`: the op compiles; the three errors; the unknown-op list includes `notes`.
  - `slide-script.test.ts` (the stub deck): notes replace the old ones; every shape's geometry and text is unchanged; empty text clears; the slide's hash changes; a `notes` op with a `slides` rebuild of the same slide is refused; one deck slide plus an added slide in the same call does not crash `emptyTemplate`.
  - `slide.it.test.ts` on `sample.pptx` slide 2 (notes "Mention the lease."): set two lines; the slide view's `notes` reads them; the shapes' geometry equals what it was.
  - `tests/test_office_ops_input.py`: `{"notes": {"slide": "1", "hash": "h", "text": "Hi"}}` on pptx reads as op `notes`.
- [ ] **Step 2: Run them** — Expected: FAIL.
- [ ] **Step 3: Implement**, and teach it: the Deck line ("notes {slide, hash, text} sets a slide's speaker notes and changes nothing else"), pptx.md's Notes section with an example (a `slides` rebuild still carries notes in `<aside>`).
- [ ] **Step 4: Run them** plus `npx vitest run test/skill-examples.test.ts` — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(runtime-office): a notes op sets a slide's speaker notes alone"`

### Task 4: Unreadable slide text is refused

**Files:**
- Modify: `runtime-office/src/slides/inspect.ts` (the "shapes" phase after the overflow check, 350-397; colour helpers 59-68), `skills/productivity/office/references/pptx.md` (the contrast rule)
- Test: `runtime-office/test/measure.test.ts` (new cases; and the inline test slides at 108-120 and 212-214 recoloured), `runtime-office/test/measure-style.test.ts` (118, 126, 133 as needed)

**Interfaces:**
- Produces: `contrastRatio(a: Rgb, b: Rgb) -> number` (WCAG). For each element holding text, at the centre of each of its text's line boxes, the colour under the text is found with `document.elementsFromPoint`: the elements below the text in paint order, each solid background composited (alpha) over the next until opaque, the page's `#ffffff` last; a gradient counts as its worst stop; a picture under the text skips the check. The text's colour carries its opacity product as `add()` computes it. Below 3:1: ``${describe(el)}: its text (${fg}) on ${bg} reads at ${ratio}:1; text needs at least 3:1 here: try ${suggestion}.`` where the suggestion is `#14213D` or `#FBFBF8`, whichever reads better on `bg`.

- [ ] **Step 1: Write the failing tests** in `measure.test.ts`:
  - `refuses white text on a white card`: a white `<div>` card holding `<h2 style="color:#FBFBF8">`: rejects with `"reads at 1.0:1"` and `"#14213D"`.
  - `takes white text on a pinned dark band`: white text over a pinned `#14285A` band: measures.
  - `composites a translucent card`: a `rgba(255,255,255,0.1)` card on a `#14213D` section with white text: measures.
  - `skips text over a picture`: text over an `<img>`: measures.
- [ ] **Step 2: Run them** — Expected: FAIL. Then recolour the existing low-contrast test slides (white on `#22c55e`, `#38BDF8` on `#F8FAFC`, and any measure-style case that now fails) to readable colours that keep each test's purpose.
- [ ] **Step 3: Implement.** pptx.md: "Text holds 4.5:1 contrast on its background (3:1 from 44 px); below 3:1 the slide is refused."
- [ ] **Step 4: Run them** — `npx vitest run test/measure.test.ts test/measure-style.test.ts test/skill-examples.test.ts`, then `slide-style.it.test.ts` alone — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(runtime-office): refuse slide text that cannot be read against what is under it"`

### Task 5: A PDF note placed by its words

**Files:**
- Modify: `runtime-office/src/formats/pdf.ts:94-107`, `runtime-office/editor/pdf.js` (`check` 203-206, `APPLY.annotate` 257-275), `surogates/office/ops_input.py` (`OP_SHAPES["pdf"]["annotate"]`), `surogates/tools/builtin/office.py` (PDF line), `skills/productivity/office/references/pdf.md`
- Test: `runtime-office/test/formats-pdf.test.ts`, `runtime-office/test/pdf.it.test.ts`

**Interfaces:**
- Produces: annotate takes `"anchor": {"find": "…", "nth": 1, "match_case": false}` instead of `page` and `rect` (both: `annotate takes "anchor" or "page" and "rect", not both.`; neither: `annotate needs "anchor": {"find": "words on the page"}, or "page" and "rect".`). In pdf.js `check()`, the nth match across pages in order comes from `ApiPage.Search({text, matchCase, wholeWords: false})` (quads in points from the top-left); none: `"<find>" is not in the document.`; too few: `"<find>" is in the document N times: nth counts from 1 up to N.`. The place comes from `placeBeside`. The result is `{op, page, rect, beside: <the words matched>}`; a rect-placed annotate returns `{op, page, rect}`.
- `placeBeside(pageW, pageH, match, lines, taken, w, h)` — `match` is the first quad's box; `lines` the page's text line boxes (from `GetSelectionQuads()` after selecting the whole page, as `pageText` does); `taken` the page's annotation and field rects. Candidates, first that lies inside the page and overlaps no line box and no taken rect:
```
line = the line box holding the match (else the match box); gap = 8; top = clamp(match.top - 2, 0, pageH - h)
right of the line:  [line.right + gap, top, line.right + gap + w, top + h]
left margin:        [line.left - gap - w, top, line.left - gap, top + h]
below the line:     [match.left, line.bottom + gap, match.left + w, line.bottom + gap + h]
above the line:     [match.left, line.top - gap - h, match.left + w, line.top - gap]
none fits: refuse  There is no free place beside "<find>" on page P: give "page" and "rect".
```
  A note is `w = 200`, `h = 14 × ceil(len(text) × 5.5 / 192) + 8` capped at 300; a stamp `150 × 50`.

- [ ] **Step 1: Write the failing tests.**
  - `formats-pdf.test.ts`: an anchor compiles (nth defaults to 1, match_case to false); both and neither are refused; an empty `find` is refused; the existing rect form still compiles.
  - `pdf.it.test.ts` on `sample.pdf` (line "Fees are due within thirty days of each invoice.", 12 pt at x = 72, baseline about 130 pt from the top):
    - `places a note beside its words`: anchor `"thirty days"` gives `page: 1`, a rect whose vertical span overlaps 118–134, which overlaps none of page 1's text lines, and `beside: "thirty days"`; the outline lists a free-text annotation at that rect.
    - `refuses words that are not there`, and `refuses an nth past the matches` (`"thirty days"`, nth 2: `is in the document 1 times`).
    - `places a stamp by its words`.
- [ ] **Step 2: Run them** — Expected: FAIL.
- [ ] **Step 3: Implement**, and teach it: pdf.md shows the anchored form first ("place a note by the words it is about"), rect second; the PDF line of EDIT_DESCRIPTION gives `annotate {kind, anchor: {find, nth?} | page + rect, text | stamp}`.
- [ ] **Step 4: Run them** plus `npx vitest run test/skill-examples.test.ts` — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(runtime-office): place a PDF note or stamp beside the words it is about"`

### Task 6: Measure, and the pull request

- [ ] **Step 1: Run the touched suites once on the branch head**: runtime-office `npx vitest run`; the IT files word-edit, slide, slide-style, pdf one at a time; `tests/test_office_ops_input.py tests/test_office_descriptions.py tests/integration/test_office_tools.py`.
- [ ] **Step 2: Ask Flavius**, then re-seed DEV's skills from this branch and restart the runtime api, worker and runtime-office from this worktree.
- [ ] **Step 3: Full run** (`obench run --runs 3`), report, RESULTS.md row; say which failure classes moved (docx-continue-list, pptx-notes, pdf-annotate, pptx-onboarding).
- [ ] **Step 4: Push and open the PR.** Ask Flavius before merging.
