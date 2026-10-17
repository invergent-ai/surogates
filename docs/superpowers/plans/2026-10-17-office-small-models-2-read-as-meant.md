# Office Edits Read as Meant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `office_edit` reads what a small model clearly meant, its results show what a reader will see, documents are only made in the editor, and the rules and reference for each document type reach the model with its first office call.

**Architecture:** Input normalisation lives in one new module the tool calls before any op runs (`surogates/office/ops_input.py`). Word results gain the reader's view in the in-page script (`runtime-office/editor/word.js`) and pass through runtime-office unchanged. The guide for a document type is fetched from the office skill through the API client and recorded in the session's config. Fallback skills are hidden where their tool is offered, in the API's skill routes and in the worker's skill tools. A terminal call that leaves office documents behind gets a note.

**Tech Stack:** Python 3.12 (harness), TypeScript and in-page JavaScript (runtime-office, vitest), pytest.

**Spec:** `docs/superpowers/specs/2026-10-17-office-editing-for-small-models-design.md` ("Step 1")

## Global Constraints

- A new worktree `.worktrees/office-read-as-meant` on branch `feat/office-read-as-meant` from `origin/master` once the benchmark PR has merged. Never work in the main checkout itself.
- Conventional Commits; `git add` named files only; no Co-Authored-By trailer; never mention plans, tasks or reviews in code, comments or commit messages.
- Python tests: the main checkout's `.venv/bin/python -m pytest <files> -q -p no:cacheprovider`, never `uv run`. Integration tests (tests/integration) need Docker. A whole-suite run uses `--basetemp` under `/dev/shm` (the development machine's /tmp has few inodes).
- Node only via the machine's nvm install of Node 26 on `PATH`; `cd runtime-office && npm ci` once; unit tests `npx vitest run <file>`.
- Integration tests of runtime-office run one file at a time against the DEV document server, with `OFFICE_IT_DS_URL`, `OFFICE_IT_HOST` and `OFFICE_IT_JWT_SECRET` (`office.jwt_secret` in the development config; never print it). Let editor pages finish loading pictures before closing them.
- **Public repository:** no hosts, IPs, buckets, keys or agent names in code, tests, commits or PRs.
- **No silent repair:** whatever the tool reads differently from what the model sent, its result says so (`read_as`).
- No legacy fallback: renamed references and changed forms leave no old name behind.
- `error` in a tool result means the call failed (the chat UI marks it red); notes go in other keys.
- Ask Flavius before restarting DEV processes, re-seeding DEV's skills bundle, merging or releasing. Every subagent uses model `opus`.

## Review Focus

1. Broken JSON whose failure is not a missing closer (a missing comma, a stray quote, a closer missing in the middle of a list followed by more ops): refused with the position, never guessed. Test in Task 1.
2. The guide cannot be fetched (no API client, a 5xx): the call's own result still returns, without `guide`, and the type is not marked as given, so the next call tries again. Test in Task 3.
3. A mention acknowledgement (`acknowledge_mention`, which has no `tool_call_id`) never takes the guide or marks the type as given. Test in Task 3.
4. A terminal call that only reads an office document (`pdftotext a.pdf -`): no "made by a script" note, because nothing in the workspace changed. Test in Task 5.
5. A computer-bound session (tools run on the user's machine): no listing is attempted and the terminal result is unchanged. Test in Task 5.

---

### Task 1: `ops` read as meant

**Files:**
- Create: `surogates/office/ops_input.py`
- Modify: `surogates/tools/builtin/office.py:158-166` (`_office_edit`)
- Test: `tests/test_office_ops_input.py`, `tests/integration/test_office_tools.py`

**Interfaces:**
- Produces:
  - `class OpsInputError(ValueError)`
  - `OP_NAMES: dict[str, tuple[str, ...]]` — `docx`: insert, replace, delete, move, comment, reply, resolve; `xlsx`: set, format, chart, sheet, reply, resolve; `pptx`: slides, text, order, remove, reply, resolve; `pdf`: pages, fill, add_field, redact, annotate, reply, resolve
  - `OP_SHAPES: dict[str, dict[str, str]]` — one-line shape per op name, as in the tool description (`"text": '{"op": "text", "slide": "<id>", "hash": "<hash>", "shape": "<id>", "text": "…"}'`)
  - `read_ops(raw: object, kind: str) -> tuple[list[dict], list[str]]` — the ops, and what was read differently (empty when nothing was)
  - `close_missing(text: str) -> tuple[str, list[str]] | None` — the text with missing closers added, and what was added; None when the failure is anything else

- [ ] **Step 1: Write the failing tests** in `tests/test_office_ops_input.py`:
  - `test_list_passes_unchanged`: `read_ops([{"op": "delete", ...}], "docx") == ([...], [])`.
  - `test_valid_json_string`: `read_ops('[{"op": "delete", "target": {"block": "1", "hash": "h"}}]', "docx")` gives the list and one note containing `"text"`.
  - `test_missing_brace_before_bracket`: the string `'[{"op": "insert", "target": {"end": true}, "content": {"markdown": "# A\\n<?block why?>", "blocks": {"why": {"type": "pending", "intent": "Why"}}}]'` (one `}` short, as the benchmark saw) parses to one insert op whose `content.blocks.why.intent == "Why"`, with a note containing `"missing '}'"`.
  - `test_missing_closers_at_end`: `'[{"op": "delete", "target": {"block": "1", "hash": "h"'` closes to one op; the note names `'}}]'`.
  - `test_other_breakage_is_refused`: `'[{"op": "delete" "target": {}}]'` (a missing comma) raises `OpsInputError` whose message contains `"character"` and `"not valid JSON"`; so does `'[{"op": "insert", "content": {"markdown": "a"}, {"op": "delete"}]'` (a closer missing mid-list, then another op).
  - `test_single_op_object`: a dict with `"op"` gives a list of one and a note containing `"one op"`; a JSON string of one object does the same.
  - `test_op_name_as_key`: `read_ops([{"text": {"slide": "1", "hash": "h", "shape": "2", "text": "Q3"}}], "pptx")[0] == [{"op": "text", "slide": "1", "hash": "h", "shape": "2", "text": "Q3"}]`, with a note.
  - `test_type_as_op`: `[{"type": "fill", "fields": {"client": "A"}}]` on pdf gives op `fill`; `[{"type": "text", "page": 1, "rect": [1, 1, 2, 2], "name": "n"}]` on pdf raises (`text` is not a PDF op) naming `add_field`.
  - `test_unknown_op_lists_the_ops_and_the_nearest`: `[{"op": "rewrite"}]` on docx raises a message containing `"insert, replace, delete, move, comment, reply or resolve"` and `OP_SHAPES["docx"]["replace"]`.
  - `test_empty_is_refused`: `[]`, `""`, `None` raise `'"ops" is a non-empty list of ops.'`.
- [ ] **Step 2: Run them** — Expected: FAIL (module missing).
- [ ] **Step 3: Implement `ops_input.py`.** `close_missing` scans the text keeping a stack of open `{`/`[` outside strings (honouring `\"`). It repairs only two failures: the text ends with containers still open (append their closers, innermost first), and a closer arrives that does not match the innermost open container (insert the missing closers right before it until it matches). Any other parse failure, and a repaired text that still does not parse, gives None. `read_ops` order: string → `json.loads`, else `close_missing` then `json.loads`, else refuse with `ops is not valid JSON: <msg> at character <pos>`; a dict → list of one; per op: alias the op-name key or `type`, then check the name against `OP_NAMES[kind]` (`difflib.get_close_matches` picks the nearest).
- [ ] **Step 4: Run them** — Expected: PASS.
- [ ] **Step 5: Use it in `_office_edit`.** The kind comes from `office_kind(relative_path(...)).file_type` (`surogates/office/kinds.py:32-35`); `OpsInputError` becomes `_error(str(exc))`; the notes go in the result as `"read_as": [...]` (beside `document`, `open_by`). Write the integration test `test_broken_ops_string_is_read_and_said` in `tests/integration/test_office_tools.py`: the broken string from Step 1 reaches `FakeRuntime.calls` as the parsed list and the result's `read_as` names the added brace.
- [ ] **Step 6: Run** `tests/test_office_ops_input.py tests/integration/test_office_tools.py` — Expected: PASS.
- [ ] **Step 7: Commit** — `git commit -m "feat(office): read office_edit ops as the model meant them"`

### Task 2: Word results show the reader's view and the length

**Files:**
- Modify: `runtime-office/editor/word.js` (`describe` 59-70, `EDITS` 468-519, `edit` 553-571), `runtime-office/src/formats/format.ts:8`, `runtime-office/src/office.ts:52-63`
- Test: `runtime-office/test/office.test.ts`, `runtime-office/test/word-edit.it.test.ts`, `runtime-office/test/word-read.it.test.ts`

**Interfaces:**
- Produces:
  - `labelOf(el) -> string` in word.js: a list item's visible label (`"3."`), `"•"` for a bullet, `""` otherwise — from `GetText({ Numbering: true })` before its first tab.
  - `describe()` text begins with `labelOf(el) + " "` for a list item (the hash stays on `textOf`, without the label).
  - insert, replace (block and pending) and move results carry `reads: [{id, kind, text}]` for the blocks they made, text cut at 24 words.
  - `Outcome = { results: unknown[]; length?: { words: number; pages: number } } | { refused: unknown }` in format.ts; word.js `edit` returns `length` from `doc.GetStatistics()` (`WordsCount`, `PageCount`); `Office.edit` returns `{ applied, results, length, flushed }` when there is one.

- [ ] **Step 1: Write the failing tests.**
  - `office.test.ts` `passes the document's length through`: a fake format whose `edit` answers `{results: [{op: 0}], length: {words: 120, pages: 1}}` gives a response with that `length`.
  - `word-read.it.test.ts` `shows list labels`: in `sample.docx` the outline's numbered items read `"1. …"` and `"2. …"`, the bullets `"• …"`; their hashes are the ones the test already pins.
  - `word-edit.it.test.ts` `shows what an insert made and how long the document is`: insert `"- Renew the support contract"` after the second numbered item; `results[0].reads[0].text` starts with `"1."` (a new list today; the join comes with the protocol-gaps work), and `length.words` is greater than 0, `length.pages` at least 1.
- [ ] **Step 2: Run them** — `npx vitest run test/office.test.ts`, then each IT file alone — Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces. Check in the IT run that `PageCount` reflects the edit just made; if it lags, take pages from `doc.GetPageCount()` instead.
- [ ] **Step 4: Run them** — Expected: PASS. Run all runtime-office unit tests (`npx vitest run`) and `word-edit`, `word-read`, `word-comments` IT files one at a time.
- [ ] **Step 5: Commit** — `git commit -m "feat(runtime-office): Word results show list labels and the document's length"`

### Task 3: Rules and references with the first office call; references renamed

**Files:**
- Rename: `skills/productivity/office/references/{word,cell,slide,pdf}.md` → `{docx,xlsx,pptx,pdf}.md` (`git mv`)
- Modify: `skills/productivity/office/SKILL.md:44-51`, `surogates/tools/builtin/office.py:82` (EDIT_DESCRIPTION) and `_run` (186-227), `runtime-office/test/skill-examples.test.ts:13,44-49,75`, `tests/test_office_skill.py:23`
- Create: `surogates/office/guide.py`
- Test: `tests/integration/test_office_tools.py`, `tests/test_office_skill.py`

**Interfaces:**
- Produces:
  - `GUIDES_KEY = "office_guides"` (session config list of kinds already given)
  - `async def guide_for(kind: str, api_client: Any) -> dict[str, str] | None` — `{"rules": <SKILL.md body without its frontmatter>, "reference": <references/<kind>.md>}` through `api_client.view_skill("office")` and `view_skill("office", f"references/{kind}.md")`; None when either fails (logged)
  - In `_run`: for a model's call (`kwargs.get("tool_call_id")`), a successful result whose kind is not in `session.config[GUIDES_KEY]` gets `"guide"`, and the kind is recorded with `store.append_session_config_list(session_id, GUIDES_KEY, kind)` after the guide was attached.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_office_skill.py`: the references are `("docx.md", "xlsx.md", "pptx.md", "pdf.md")`; `test_guides_stay_small`: for each kind, SKILL.md's body plus its reference is under 40 000 characters.
  - `tests/integration/test_office_tools.py`, with a fake API client answering `view_skill`:
    - `test_first_call_of_a_type_carries_its_guide`: the first `office_read` of `a.docx` has `guide.reference` starting `"# office_edit for Word documents"` and `guide.rules` without `---`; the session's config then holds `["docx"]`.
    - `test_second_call_does_not`: a second `office_edit` on `a.docx` has no `guide`.
    - `test_each_type_once`: a first call on `b.xlsx` carries the spreadsheet reference.
    - `test_unfetchable_guide_is_left_out`: a client raising `httpx.HTTPStatusError` gives a normal result without `guide`, and the config stays without the kind.
    - `test_acknowledgement_never_takes_the_guide`: `acknowledge_mention` on a docx leaves `guide` out and the config unchanged.
  - `runtime-office/test/skill-examples.test.ts` reads the new names (its minimum example counts stay).
- [ ] **Step 2: Run them** — Expected: FAIL.
- [ ] **Step 3: Implement**, and rename the references with every mention: SKILL.md's links, EDIT_DESCRIPTION's `references/slide.md`, the two tests. Old docs under `docs/superpowers/` stay as written (history).
- [ ] **Step 4: Run them** plus `npx vitest run test/skill-examples.test.ts` — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(office): the first office call for each document type carries its rules and reference"`

### Task 4: Fallback skills hidden everywhere; the pdf skill split; a missing skill file lists what exists

**Files:**
- Create: `surogates/tools/skill_visibility.py` (beside the skills loader, `surogates/tools/loader.py`); `skills/productivity/pdf-forms/SKILL.md`; move `skills/productivity/pdf/FORMS.md` and `skills/productivity/pdf/scripts/{check_fillable_fields,extract_form_field_info,extract_form_structure,fill_fillable_fields,fill_pdf_form_with_annotations,check_bounding_boxes,create_validation_image,convert_pdf_to_images}.py` into `skills/productivity/pdf-forms/` (`git mv`), with `LICENSE.txt` copied
- Modify: `skills/productivity/pdf/SKILL.md` (description; Overview line 11; "Rotate Pages" section 80-91 moves to pdf-forms; the rotate lines in the qpdf and pdftk blocks; Quick Reference "Fill PDF forms" row; Next Steps), `surogates/api/routes/skills.py` (list 466-533, view, `read_skill_file` 648-800), `surogates/tools/builtin/skills.py` (`skills_list` 239-309, `skill_view` 418-773), `surogates/harness/prompt.py:743-766`, `surogates/workstreams/__init__.py:80`, `docs/skills/index.md:45,59`
- Test: `tests/test_office_skill.py`, `tests/test_fallback_skills_hidden.py`, `tests/integration/test_skills_fallback.py`, `tests/test_skill_view_handler.py`, `tests/test_project_chat_leaves_work_to_threads.py:37`

**Interfaces:**
- Produces:
  - `def offered_fallback_tools(channel: str | None, config: dict | None) -> frozenset[str]` — `{"office_read", "office_edit"}` when `load_settings().office.agent_editing` and `office_tools_offered(SimpleNamespace(channel=channel, config=config or {}))`, else empty
  - `def fallback_hidden(fallback_for_tools: Sequence[str], offered: frozenset[str]) -> bool` — True when the list is non-empty and every tool in it is offered (the rule `_skill_visible` applies; prompt.py calls this helper too)
  - The API routes take the session from `session_id` (`_authorize_session_for_staging`, routes/skills.py:222-246): the list leaves hidden skills out; view and file answer 403 with detail `"<name> is for chats without the office tools: this chat makes and edits documents live with office_edit (the office skill)."` Without `session_id` (the web UI's slash menu) nothing changes.
  - `skill_view` in API mode: a 403 returns `{"success": false, "error": <detail>}`; a 404 for a file returns `{"success": false, "error": "File '<path>' not found in skill '<name>'.", "available_files": <the skill's linked_files>}`.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_office_skill.py`: `pdf-forms` has `fallback_for_tools == ["office_edit"]`; `pdf` has none and its description no longer mentions office_edit, forms or rotation; `pdf-forms` holds FORMS.md and the form scripts; `tests/test_pdf_watermark.py` still passes.
  - `tests/test_fallback_skills_hidden.py` (worker-local mode, prompt), parametrized over a chat with the office tools (`channel="web"`) and one without (`channel="api"`): docx, xlsx, pptx, pdf-forms are absent from the prompt index, `skills_list` and `skill_view` with the tools, present without; `pdf` is present in both.
  - `tests/integration/test_skills_fallback.py` (API routes): with `session_id` of a web session, `GET /v1/skills` leaves the four out and `GET /v1/skills/docx` is 403 with the detail; without `session_id` all are listed.
  - `tests/test_skill_view_handler.py` `test_missing_file_lists_what_exists`: a MockTransport answering 404 for `references/docx.md` and the skill's view with `linked_files` gives the `available_files` result, not `"Tool execution failed"`.
- [ ] **Step 2: Run them** — Expected: FAIL.
- [ ] **Step 3: Implement.** Split the pdf skill: `pdf-forms` (description: "Fill in a PDF form, annotate or redact a PDF, or add, remove or rotate its pages, when the office_edit tool is not available. …") holds FORMS.md, the form scripts and the rotate section; `pdf` keeps merging, splitting, text and tables, creating PDFs, OCR, watermarks, encryption, images; where pdf mentioned forms it now says "fill, annotate, redact or rotate: office_edit where the chat has it, else the pdf-forms skill". Add `"pdf-forms"` to `DOCUMENT_SKILLS` and to the mirror list in `tests/test_project_chat_leaves_work_to_threads.py:37`. Update `docs/skills/index.md` to say a fallback skill is hidden from the index, `skills_list` and `skill_view`.
- [ ] **Step 4: Run them** plus `tests/test_skill_requires_agents.py tests/test_project_chat_leaves_work_to_threads.py tests/test_skill_view_handler.py` — Expected: PASS.
- [ ] **Step 5: Commit** in two: `refactor(skills): split form filling out of the pdf skill into pdf-forms`, then `feat(skills): hide a fallback skill everywhere its tool is offered`.

### Task 5: A script that leaves office documents behind is told

**Files:**
- Create: `surogates/office/scripted.py`
- Modify: `surogates/harness/tool_exec.py` (after dispatch, beside 2119-2125; `_noted` 221-229)
- Test: `tests/test_office_scripted.py`, `tests/test_tool_exec_office_notes.py`, `tests/integration/test_thread_helpers.py:437-439`

**Interfaces:**
- Produces:
  - `async def scripted_documents(session: Any, since: float, *, sandbox_pool: Any) -> list[str]` — workspace-relative paths of `.docx .xlsx .pptx .pdf` files changed in the session's sandbox at or after `since - 2` (one in-pod `find /workspace -maxdepth 6 -type f ( -iname '*.docx' -o … ) -newermt @<epoch> -not -path '*/.skills/*'`, through `sandbox_pool.execute` as `surogates/harness/loop_arbor.py:283` calls it, 5 s timeout); `[]` for a computer-bound session or any failure
  - `SCRIPTED_NOTE = "{paths} changed by a script: the user can neither watch nor co-edit a document made that way. Make and change documents with office_edit (read them with office_read)."`
  - `_noted(content, note)` appends to an existing `note` (`"<old> <new>"`) instead of overwriting it, and keeps `ensure_ascii=False`.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_office_scripted.py`, with a fake pool: the find output `"/workspace/deck.pptx\n/workspace/sub/b.xlsx\n"` gives `["deck.pptx", "sub/b.xlsx"]`; an empty output gives `[]`; a pool raising gives `[]`; a computer-bound session never calls the pool.
  - `tests/test_tool_exec_office_notes.py` (the `tests/test_tool_arg_repair.py` pattern, `execute_single_tool` with a stub terminal): in a web session with the office tools, a terminal call after which the fake pool reports `deck.pptx` gets `SCRIPTED_NOTE` naming it; with nothing changed (`pdftotext a.pdf -`) the result is unchanged; in an `api` session (no office tools) the pool is never asked. A terminal command naming a document open in the editor gets `open_document_note`'s text.
  - `tests/integration/test_thread_helpers.py`: `_noted` on content that already has a `note` keeps both.
- [ ] **Step 2: Run them** — Expected: FAIL.
- [ ] **Step 3: Implement.** tool_exec records `time.time()` before dispatching `terminal`; after a successful terminal call in a session where `offered_fallback_tools(...)` is non-empty, it lists and notes. Background terminal processes are not followed.
- [ ] **Step 4: Run them** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(office): tell the agent when a script leaves office documents behind"`

### Task 6: Tool descriptions, slide CSS that changes nothing, skill text

**Files:**
- Modify: `surogates/tools/builtin/office.py:41-93`, `skills/productivity/office/SKILL.md`, `skills/productivity/office/references/docx.md:14`, `runtime-office/src/slides/inspect.ts` (style loop 303-315; the check phase's return 336), `runtime-office/src/slides/measure.ts` (`SlideSpec`), `runtime-office/editor/slide.js:738-756`
- Test: `tests/test_office_descriptions.py`, `runtime-office/test/measure.test.ts`, `runtime-office/test/slide-script.test.ts`

**Interfaces:**
- Produces: `SlideSpec.ignored: string[]` — one line per property taken as it is but changing nothing (`"border-collapse: collapse on a table: native tables already collapse"`); slide.js puts them in the op result's `note` after `magicNote`'s.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_office_descriptions.py`: EDIT_DESCRIPTION's first sentence says documents, new ones too, are made and changed only with office_edit, naming scripts and python libraries; it holds one fenced example call per document type, each parsing as JSON with an `ops` list whose op names are in `OP_NAMES` of its type; it says `450 words` fill a page and `office_read` before you answer. READ_DESCRIPTION names checking your edits with it.
  - `measure.test.ts`: a table with `border-collapse:collapse` measures, `spec.ignored` names it; `border-collapse:separate` is still refused; a `<ul style="list-style-type:disc">` and an `<ol style="list-style-type:decimal">` measure with notes; `list-style-type:lower-roman` is still refused.
  - `slide-script.test.ts`: a slides op whose spec has `ignored` returns a `note` that contains it.
- [ ] **Step 2: Run them** — Expected: FAIL.
- [ ] **Step 3: Implement.** Rewrite the descriptions in this order: made and changed only with these tools; per document type, one complete valid example call; words per page (450) and "check with office_read before you answer"; then the op summaries as today. SKILL.md: open with "make a new document with office_edit too", add the "before you answer" check (read the outline again; every fact the user gave is in it; the length is what was asked; then say what was done), and keep the reference names current.
- [ ] **Step 4: Run them** — `tests/test_office_descriptions.py`, `npx vitest run test/measure.test.ts test/slide-script.test.ts test/skill-examples.test.ts` — Expected: PASS.
- [ ] **Step 5: Commit** in two: `feat(office): tool descriptions lead with making documents in the editor`, then `feat(runtime-office): take slide CSS that changes nothing, with a note`.

### Task 7: Measure, and the pull request

- [ ] **Step 1: Run the touched suites once on the branch head**: the Python files of Tasks 1–6 and `tests/integration/test_office_tools.py tests/integration/test_office_guard.py tests/integration/test_skills_fallback.py`; runtime-office `npx vitest run`; each runtime-office IT file the work touched (word-read, word-edit, slide, slide-style), one at a time.
- [ ] **Step 2: Ask Flavius**, then on DEV: re-seed the system skills bundle from this branch's `skills/` (`surogate-ops seed-builtin-skills`), restart the runtime api, worker and runtime-office from this worktree, the way the DEV processes run today.
- [ ] **Step 3: Full run** — `obench run --runs 3` from `benchmarks/office_bench` (one session at a time, 10-minute health checks as before). Then `obench report <run>` and a RESULTS.md row beside the baseline; note which failure classes moved.
- [ ] **Step 4: Push and open the PR** with the before/after rows. Ask Flavius before merging.
