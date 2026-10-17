# Office Templates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** New Word documents, spreadsheets and decks start from templates good enough to hand to a user, the templates reach the model with its first office call, and the benchmark gains harder editing tasks.

**Architecture:** Templates are files of the office skill's bundle, one per document type (`skills/productivity/office/templates/{docx,xlsx,pptx}.md`), written as the same JSON calls the references hold, so the existing skill-examples test checks every one against its format (and measures every slide on both canvases). The guide that the first office call carries (`surogates/office/guide.py`) adds its type's templates.

**Tech Stack:** Markdown + JSON calls, TypeScript (vitest), Python 3.12, the benchmark (`obench`).

**Spec:** `docs/superpowers/specs/2026-10-17-office-editing-for-small-models-design.md` ("Step 3", "The benchmark")

## Global Constraints

- A new worktree `.worktrees/office-templates` on branch `feat/office-templates` from `origin/master` once the protocol-gaps PR has merged. Never work in the main checkout itself.
- Conventional Commits; `git add` named files only; no Co-Authored-By trailer; never mention plans, tasks or reviews in code, comments or commit messages.
- Python tests: the main checkout's `.venv/bin/python -m pytest <files> -q -p no:cacheprovider`, never `uv run`. Node only via the machine's nvm install of Node 26 on `PATH`. Benchmark tests in its own venv (`benchmarks/office_bench/.venv/bin/pytest -q -p no:cacheprovider`).
- Templates follow the skill's writing rules (lead with the answer, specifics over adjectives, the document's own styles) and the slide reference's design rules (a type scale of four or five sizes, a hex palette chosen once, square corners, nothing under 24 px, 4.5:1 contrast); content is fictional and generic, with no real people or companies.
- Each template is a call the model can send as it is: the skill-examples test holds every one to its format.
- A first-call guide stays under 40 000 characters per type (rules, reference and templates together).
- **Public repository:** nothing private in templates, tasks or fixtures.
- Ask Flavius before restarting DEV processes, re-seeding DEV's skills, merging or releasing. Every subagent uses model `opus`.

## Review Focus

1. A template slide on a 4:3 deck: valid and readable there too, not only on 16:9. Test in Task 3.
2. The guide for a type with no templates (PDF): no `templates` key and no error. Test in Task 4.
3. A first call whose guide would pass 40 000 characters with its templates: the size test fails before release. Test in Task 4.
4. A template's pending section intents state a length in words, so the model writes to a length. Test in Task 1.
5. A spreadsheet template whose totals are formulas, not typed numbers. Test in Task 2.

---

### Task 1: Word templates

**Files:**
- Create: `skills/productivity/office/templates/docx.md`
- Modify: `runtime-office/test/skill-examples.test.ts` (read `templates/<name>` beside `references/<name>`; `docx.md` at least 6 template calls), `skills/productivity/office/SKILL.md` (Word documents: "start a new document from its template")
- Test: `runtime-office/test/skill-examples.test.ts`, `tests/test_office_skill.py`

**Interfaces:**
- Produces: `templates/docx.md` with six sections — `## Report`, `## Letter`, `## Meeting minutes`, `## Proposal`, `## FAQ`, `## How-to guide` — each: one line on when to use it; the first call (one `insert` at `{"end": true}`: the title, the headings, one `<?pending key: intent?>` per section, each intent saying what it holds and how many words); then one line on the order to fill the sections and their total length (a page is about 450 words).

- [ ] **Step 1: Write the failing tests.**
  - `skill-examples.test.ts`: `templates/docx.md` yields at least 6 calls and each passes `compileOps`.
  - `tests/test_office_skill.py` `test_word_templates_state_lengths`: every `<?pending` intent in `templates/docx.md` matches `\d+ words`; the six headings are present.
- [ ] **Step 2: Run them** — Expected: FAIL (no file).
- [ ] **Step 3: Write the templates.** Report (summary, background, findings with a table and a chart, risks, recommendation; two pages ≈ 900 words), letter (one call, no placeholders needed beyond the body; 150–250 words), minutes (attendees, decisions, discussion, actions with owner and date, next meeting), proposal (why, scope, timeline, cost with a total, recommendation), FAQ (six questions as headings, 40–80 words each), how-to guide (purpose, numbered steps, a table, the one thing to remember in bold).
- [ ] **Step 4: Run them** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(skills): Word templates for new documents"`

### Task 2: Spreadsheet templates

**Files:**
- Create: `skills/productivity/office/templates/xlsx.md`
- Modify: `runtime-office/test/skill-examples.test.ts` (`xlsx.md` at least 3 calls)
- Test: `runtime-office/test/skill-examples.test.ts`, `tests/test_office_skill.py`

**Interfaces:**
- Produces: `## Budget`, `## Invoice`, `## Tracker`, each one call of `set` and `format` ops (and a `chart` for the budget): headers, sample rows the model replaces, totals as live formulas (`=SUM(…)`), number formats (currency, dates, percentages), widths.

- [ ] **Step 1: Write the failing tests.**
  - `skill-examples.test.ts`: at least 3 calls, each passing `compileCellOps`.
  - `tests/test_office_skill.py` `test_sheet_templates_total_by_formula`: each template's `set` values hold at least one string starting `=SUM(`.
- [ ] **Step 2: Run them** — Expected: FAIL.
- [ ] **Step 3: Write the templates.**
- [ ] **Step 4: Run them** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(skills): spreadsheet templates for new workbooks"`

### Task 3: Slide templates

**Files:**
- Create: `skills/productivity/office/templates/pptx.md`
- Modify: `runtime-office/test/skill-examples.test.ts` (measure `templates/pptx.md` slides on `{width: 1920, height: 1080}` and `{width: 1920, height: 1440}`)
- Test: `runtime-office/test/skill-examples.test.ts`

**Interfaces:**
- Produces: eight slide types, each one `slides` op with one `<section>`: `## Title`, `## Statement`, `## Big number`, `## Cards`, `## Table`, `## Chart`, `## Timeline`, `## Closing`. Layout by flex flow and padding (no fixed heights), so one section is valid on both canvases. Then `## Decks`: pitch, onboarding and review decks as orders of those types with what each slide holds (prose, no JSON).

- [ ] **Step 1: Write the failing test** in `skill-examples.test.ts`: `templates/pptx.md` yields 8 slide calls; each passes `checkSlideOps` and measures on both canvases (no refusal: overflow, past the edge, contrast).
- [ ] **Step 2: Run it** — Expected: FAIL.
- [ ] **Step 3: Write the templates**, one palette and one type scale across all eight, so slides from them sit together in one deck.
- [ ] **Step 4: Run it** — Expected: PASS (needs Chromium; the test is skipped without it, so run it where Chromium is installed).
- [ ] **Step 5: Commit** — `git commit -m "feat(skills): slide templates that fit 16:9 and 4:3 decks"`

### Task 4: The templates reach the model

**Files:**
- Modify: `surogates/office/guide.py` (`guide_for`), `skills/productivity/office/SKILL.md`
- Test: `tests/integration/test_office_tools.py`, `tests/test_office_skill.py`

**Interfaces:**
- Consumes: `guide_for(kind, api_client)` and `GUIDES_KEY` from the read-as-meant work.
- Produces: the guide adds `"templates": <templates/<kind>.md>` when the skill has the file (a 404 means none, and the key is left out).

- [ ] **Step 1: Write the failing tests.**
  - `tests/integration/test_office_tools.py`: the first call on `a.docx` has `guide.templates` containing `"## Meeting minutes"`; the first on `a.pdf` has no `templates` key.
  - `tests/test_office_skill.py` `test_guides_stay_small`: rules, reference and templates together under 40 000 characters for each type.
- [ ] **Step 2: Run them** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(office): the first office call carries its type's templates"`

### Task 5: Harder editing tasks in the benchmark

**Files:**
- Modify: `benchmarks/office_bench/obench/tasks.py` (three tasks, derived inputs from `sample.docx`), `benchmarks/office_bench/README.md` (Tasks)
- Test: `benchmarks/office_bench/tests/test_tasks.py`

**Interfaces:**
- Produces three edit tasks, each with an `Input(derive=…)` built from `runtime-office/test/fixtures/sample.docx` (the fixture itself stays unchanged):
  - `docx-long-edit`: the document grown to about twelve pages of numbered sections with figures; "In section 17 the budget is wrong: it is €48,500, not €45,800. Fix it everywhere it appears, and add one sentence after the section's table saying the figure was corrected on 14 October." Checks: every `45,800` gone and `48,500` in its places, the sentence after the table, nothing else changed (the existing "rest kept" pattern).
  - `docx-table-edit`: "Add a row for South (Q1 70, Q2 65) to the table and fix its Total row." Checks: the row, the totals, the table's style kept.
  - `docx-many-threads`: five comment threads added by the derive function (python-docx comments); "Answer the three questions in the comments and resolve them; leave the other two." Checks: three replies of at most 400 characters stating the answer, those three resolved, the other two open and untouched.
- Each task has a "done right" test and an "untouched input fails" test, as the existing edit tasks do.

- [ ] **Step 1: Write the failing tests** (`test_task_is_well_formed` covers the new ids; a done-right and an untouched test per task).
- [ ] **Step 2: Run them** — Expected: FAIL.
- [ ] **Step 3: Implement the derive functions and checks.**
- [ ] **Step 4: Run the suite** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(bench): long documents, Word tables and many comment threads"`

### Task 6: Measure, and the pull request

- [ ] **Step 1: Run the touched suites once on the branch head**: `npx vitest run test/skill-examples.test.ts`; `tests/test_office_skill.py tests/integration/test_office_tools.py`; the benchmark's own suite.
- [ ] **Step 2: Ask Flavius**, then re-seed DEV's skills from this branch and restart the runtime api, worker and runtime-office from this worktree.
- [ ] **Step 3: Full run** of all 36 tasks (`obench run --runs 3`), report, RESULTS.md row. Write down, for Flavius, every failure class still left after the three steps: these are what fine-tuning is for.
- [ ] **Step 4: Push and open the PR.** Ask Flavius before merging.
