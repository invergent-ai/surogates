# Office Bench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Commit `benchmarks/office_bench` with nothing private in it, with overflow measured from a render, contrast and "made in the editor" checks, every deck task checking that shapes stay on the slide, and the bar model's baseline recorded.

**Architecture:** office_bench is a standalone HTTP client of the agent harness (package `obench`), like `benchmarks/workspace_bench`. A rollout's documents are read into plain dataclasses (`obench/docs.py`) and judged by checks (`obench/checks.py`, `obench/tasks.py`, `obench/evaluate.py`). A new `obench/render.py` turns a deck into a PDF through the document server's own converter (x2t), run by a configurable shell prefix, and measures where each text box's lines really fall.

**Tech Stack:** Python 3.12, httpx, python-docx, openpyxl, python-pptx, pypdf, pytest.

**Spec:** `docs/superpowers/specs/2026-10-17-office-editing-for-small-models-design.md` ("The benchmark")

## Global Constraints

- Work in the worktree `.worktrees/office-small-models`, on a branch `feat/office-bench` made from `docs/office-small-model-editing` (it carries the design and these plans). Never work in the main checkout itself.
- Conventional Commits; `git add` named files only; no Co-Authored-By trailer; never mention plans, tasks or reviews in code, comments or commit messages.
- The benchmark has its own venv: `cd benchmarks/office_bench && uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -e ".[dev]"`, tests with `.venv/bin/pytest -q -p no:cacheprovider`. Never `uv sync` at the repo root, never `uv run`.
- **Public repository:** no IP addresses, host or node names, cluster names, buckets, keys, tokens, agent names or ids, session ids or service-account names in any committed file, test, commit message or PR. Endpoints, tokens, agent ids and the renderer come from the environment.
- No legacy fallback: the geometry overflow estimate is deleted, not kept beside the measurement.
- A check's name is its identity in `checks.json` and reports: keep existing names, except the overflow check, which becomes `no text overflows its box` (it is no longer an estimate); new checks get new names.
- Benchmark runs on DEV: one session at a time, the advisor off, the bar model (`surogate3.7-flash`) as the agent's base tier. Ask Flavius before starting or restarting DEV processes, and before merging.
- Every subagent uses model `opus`.

## Review Focus

1. A deck whose text uses theme colours only (the edit tasks' `sample.pptx`): contrast is unknown for those shapes, never a failure. Test in Task 3.
2. A rollout with no saved document: rendering is skipped and overflow is unknown; scoring does not crash. Test in Task 2.
3. A renderer that is configured but fails (non-zero exit, empty output, timeout): overflow is unknown with the reason in its detail, and the run goes on. Test in Task 2.
4. Rendered text that belongs to no shape (a layout's slide number or footer): ignored, never an overflow. Test in Task 2.
5. Shapes inside a group (their offsets are in the group's own space): left out of overflow and contrast rather than judged at the wrong place. Test in Task 2.

---

### Task 1: The benchmark, committed with nothing private

**Files:**
- Create: `benchmarks/office_bench/` from the `office-plan-5` worktree's untracked `benchmarks/office_bench`, copying `pyproject.toml`, `README.md`, `obench/`, `tests/` only (not `runs/`, `.venv/`, `.env`, `PROGRESS.md`, `__pycache__/`)
- Create: `benchmarks/office_bench/.gitignore` (as `benchmarks/workspace_bench/.gitignore`: `.env`, `.venv/`, `runs/`, `__pycache__/`, `.pytest_cache/`, `*.pyc`)
- Create: `benchmarks/office_bench/RESULTS.md` (header and table columns only; filled in Task 6)
- Create: `benchmarks/office_bench/tests/test_public.py`
- Modify: `benchmarks/office_bench/README.md`, `benchmarks/office_bench/obench/runner.py:12,233-236`, `benchmarks/office_bench/obench/cli.py:11-17`, `benchmarks/office_bench/obench/report.py:51-52`

**Interfaces:**
- Produces: the package `obench` and CLI `obench list|run|check|invalidate|report` as they are today.

- [ ] **Step 1: Write the failing test** `tests/test_public.py::test_no_private_details`: walk every file under `benchmarks/office_bench` that git would track (skip `.venv`, `runs`, `.env`, `__pycache__`), and assert none matches: an IPv4 address other than `127.0.0.1` and `0.0.0.0`; the development machine's name; the cluster node's name; a host ending `.local`; `cloudflarestorage`; `ops-chat-` followed by a hex digit; a UUID; the agent's name. Also `test_no_progress_log`: `PROGRESS.md` does not exist.
- [ ] **Step 2: Copy the files and run the test** — Expected: FAIL, naming README lines (the agent's name and its id prefix, the machine's name) and PROGRESS.md.
- [ ] **Step 3: Rewrite what is private or stale**
  - README: drop "Pilot results" and "Suite run" (results go to RESULTS.md in Task 6). Rewrite the DEV-specific sentences (the 0-byte save report, the 6-page pool and its 10-minute idle close, `localhost:8000`, the single worker, minutes per rollout, the unavailable sandbox) as facts about the harness in general ("runtime-office keeps a document's page open until it is idle; the runner deletes each session after collecting it so pages are freed"). Keep the `ops-chat-*` explanation generic (it is how studio sessions work, not a secret). Fix "deck 9" to 10. Add `invalidate` to Layout and Running. Add a "Requirements" table row for `OFFICEBENCH_RENDER_SHELL` (Task 2).
  - The code comments at `runner.py:12`, `runner.py:233-236`, `cli.py:11-12`, `report.py:51-52` lose their DEV wording the same way.
- [ ] **Step 4: Run the whole suite** — `.venv/bin/pytest -q -p no:cacheprovider` — Expected: PASS, every test.
- [ ] **Step 5: Commit**
```bash
git add benchmarks/office_bench/.gitignore benchmarks/office_bench/pyproject.toml benchmarks/office_bench/README.md benchmarks/office_bench/RESULTS.md benchmarks/office_bench/obench benchmarks/office_bench/tests
git commit -m "feat(bench): office_bench, the office editing benchmark on the real harness"
```

### Task 2: Overflow measured from a render

**Files:**
- Create: `benchmarks/office_bench/obench/render.py`
- Create: `benchmarks/office_bench/tests/test_render.py`, `benchmarks/office_bench/tests/fixtures/fits.pptx`, `fits.pdf`, `overflows.pptx`, `overflows.pdf`
- Modify: `benchmarks/office_bench/obench/docs.py` (delete `_overflow` and `_DEFAULT_SIZE`; `Shape.overflow` starts as `None`), `benchmarks/office_bench/obench/evaluate.py:180-201`, `benchmarks/office_bench/obench/checks.py:375-380`

**Interfaces:**
- Produces:
  - `render_pdf(document: bytes, file_type: str) -> RenderResult` where `RenderResult = dataclass(pdf: bytes | None, reason: str)`; `reason` says why `pdf` is None.
  - `measure_overflow(pdf: bytes, deck: Deck) -> None`: sets `shape.overflow` on each measured text shape to `""` (fits) or a detail string; leaves `None` where it cannot measure.
  - `RENDER_SHELL_ENV = "OFFICEBENCH_RENDER_SHELL"`; `X2T_ENV = "OFFICEBENCH_X2T"` with default `/var/www/euro-office/documentserver/server/FileConverter/bin/x2t`.

- [ ] **Step 1: Make the fixtures.** With python-pptx, make `fits.pptx` (one slide: a 600×100 pt text box holding one short line at 24 pt) and `overflows.pptx` (one slide: a 300×40 pt box holding four lines at 28 pt, named `Crowded`). Render each to PDF on DEV through the converter, with `OFFICEBENCH_RENDER_SHELL` set as Task 6 Step 2 says, and commit the four files. Check `fits.pdf`/`overflows.pdf` hold no private metadata (`/Producer` is the editor's name only).
- [ ] **Step 2: Write the failing tests** in `tests/test_render.py`:
  - `test_no_renderer_means_unknown`: with `OFFICEBENCH_RENDER_SHELL` unset, `render_pdf(b"x", "pptx").pdf is None` and `"OFFICEBENCH_RENDER_SHELL" in reason`.
  - `test_renderer_output_is_the_pdf`: with the variable set to a Python one-liner that copies `fits.pdf` to stdout, the result's `pdf` equals the fixture's bytes.
  - `test_failing_renderer_is_unknown`: a shell exiting 1 gives `pdf is None` and the exit status in `reason`; one printing nothing gives `"empty"` in `reason`; one sleeping past `timeout` gives `"timed out"`.
  - `test_text_that_fits`: `measure_overflow(fits.pdf, read_pptx(fits.pptx))` sets the text box's `overflow == ""`.
  - `test_text_that_overflows`: the same on `overflows.*` sets `Crowded`'s overflow to a string naming how far past the bottom (`"pt past"` in it).
  - `test_text_of_no_shape_is_ignored`: a rendered line whose text is in no shape (append a fake chunk via the measuring function's input) changes nothing.
  - `test_group_children_are_not_measured`: a deck whose text box sits in a group keeps `overflow is None` for it.
  - In `tests/test_evaluate.py`, `test_unsaved_document_scores_overflow_unknown`: a pptx task with no collected document scores without raising, the overflow check `passed is None`.
- [ ] **Step 3: Run them** — Expected: FAIL (`obench.render` missing).
- [ ] **Step 4: Implement `render.py`.** `render_pdf` runs `shlex.split(os.environ[RENDER_SHELL_ENV]) + ["sh", "-c", SCRIPT]` with the document on stdin, 120 s timeout, the PDF on stdout. `SCRIPT` makes a temporary directory, writes stdin to `in.<file_type>`, writes the x2t parameters file and runs x2t on it, prints `out.pdf`, removes the directory:
```xml
<?xml version="1.0" encoding="utf-8"?><TaskQueueDataConvert xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema"><m_sFileFrom>$D/in.pptx</m_sFileFrom><m_sFileTo>$D/out.pdf</m_sFileTo><m_nFormatTo>513</m_nFormatTo><m_bIsNoBase64>true</m_bIsNoBase64></TaskQueueDataConvert>
```
  x2t runs from its own directory (`cd "$(dirname "$X2T")"`). `measure_overflow` reads each PDF page (one per slide) with pypdf's text visitor into lines `(text, x0, top, x1, bottom)` in points from the top-left (page height minus the PDF's y; a line's height is its font size × 1.2). A line belongs to the top-level text shape whose box horizontally contains the line's start and whose text contains the line's text (whitespace collapsed). A shape overflows when one of its lines ends more than 2 pt below its box or more than 2 pt right of it; the detail is `"text ends N pt past the box's bottom"`.
- [ ] **Step 5: Wire it into scoring.** In `evaluate()`, for a pptx document: reuse `<rollout>/render/<name>.pdf` when it exists, else call `render_pdf` and write the file when a PDF came back; then `measure_overflow`. `p_no_overflow` is renamed `no text overflows its box`; it returns `(None, reason)` when no shape of the deck was measured, else fails on the shapes with a non-empty `overflow`. Delete `_overflow`, `_DEFAULT_SIZE` and `test_pptx_overflow_is_caught` (its estimate is gone); the render tests replace it.
- [ ] **Step 6: Run the suite** — `.venv/bin/pytest -q -p no:cacheprovider` — Expected: PASS.
- [ ] **Step 7: Commit**
```bash
git add benchmarks/office_bench/obench/render.py benchmarks/office_bench/obench/docs.py benchmarks/office_bench/obench/evaluate.py benchmarks/office_bench/obench/checks.py benchmarks/office_bench/tests/test_render.py benchmarks/office_bench/tests/test_evaluate.py benchmarks/office_bench/tests/test_tasks.py benchmarks/office_bench/tests/fixtures/fits.pptx benchmarks/office_bench/tests/fixtures/fits.pdf benchmarks/office_bench/tests/fixtures/overflows.pptx benchmarks/office_bench/tests/fixtures/overflows.pdf
git commit -m "feat(bench): measure text overflow from the document server's render"
```

### Task 3: Contrast

**Files:**
- Modify: `benchmarks/office_bench/obench/docs.py:475-574` (`Shape`, `Slide`, `read_pptx`), `benchmarks/office_bench/obench/checks.py` (Deck section), `benchmarks/office_bench/obench/tasks.py` (every pptx task)
- Test: `benchmarks/office_bench/tests/test_checks_contrast.py`

**Interfaces:**
- Produces: `Shape.color: str | None` (the first run's explicit sRGB colour, `"RRGGBB"`), `Shape.fill: str | None` (a solid fill), `Slide.background: str | None` (a solid `<p:bg>`); theme and scheme colours give `None`. `p_contrast(min_ratio: float = 3.0) -> Check` named `"text readable against what is under it"`. `contrast_ratio(a: str, b: str) -> float` (WCAG relative luminance).

- [ ] **Step 1: Write the failing tests**, building decks with python-pptx in the test:
  - `test_white_on_white_card_fails`: a white-filled rectangle, then a text box over it with white text: fails, detail names the text and `"1.0:1"`.
  - `test_white_on_dark_band_passes`: white text over a `#14285A` rectangle: passes.
  - `test_own_fill_counts_first`: a text box filled `#14285A` holding white text on a white slide: passes.
  - `test_theme_colours_are_unknown`: `runtime-office/test/fixtures/sample.pptx` unchanged gives `passed is None`.
  - `test_contrast_ratio`: `contrast_ratio("FFFFFF", "000000") == 21.0`; `round(contrast_ratio("FFFFFF", "FFFFFF"), 1) == 1.0`.
- [ ] **Step 2: Run them** — Expected: FAIL.
- [ ] **Step 3: Implement.** The background under a text shape is its own fill; else the fill of the nearest earlier shape (z-order is document order) whose box contains the text box's centre; else the slide's background; else unknown (skip the shape). A deck where no shape could be judged gives `None`. Add `C.p_contrast()` to every pptx task in `tasks.py`.
- [ ] **Step 4: Run the suite** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add` the four files; `git commit -m "feat(bench): check text contrast on slides"`

### Task 4: Shapes on the slide, and made in the editor

**Files:**
- Modify: `benchmarks/office_bench/obench/tasks.py:980-986` and the change-title, reorder and notes tasks (add `C.p_in_bounds()`), `benchmarks/office_bench/obench/evaluate.py:186-195`, `benchmarks/office_bench/obench/docs.py` (producer)
- Test: `benchmarks/office_bench/tests/test_evaluate.py`

**Interfaces:**
- Produces: `producer(path: Path) -> str | None` in docs.py (docx/xlsx/pptx: `docProps/app.xml` `<Application>`; pdf: the `/Producer` metadata). `EDITOR_PRODUCER = "euro-office"`. An implicit check named `"made in the editor"`.

- [ ] **Step 1: Write the failing tests** in `test_evaluate.py`:
  - `test_scripted_document_is_not_made_in_the_editor`: a pptx saved by python-pptx (its app.xml names a PowerPoint) with an applied `office_edit` event on it: `"made in the editor"` fails, detail quotes the producer.
  - `test_editor_document_without_office_edit_fails`: a document whose app.xml says `euro-office/2.5.565.0` but no `office_edit` event names it: fails.
  - `test_editor_document_with_office_edit_passes`: both present: passes.
  - `test_pdf_rewritten_by_pypdf_fails`: a PDF whose producer is `pypdf`: fails.
  - In `test_tasks.py`, every pptx task carries the check `"every shape inside the slide"`.
- [ ] **Step 2: Run them** — Expected: FAIL.
- [ ] **Step 3: Implement.** The check passes when an applied `office_edit` call named the found document (tool.call `arguments.path` against `found["path"]`, `__WORKSPACE__/` stripped; its tool.result has `"applied": true`) and the saved file's producer starts with `EDITOR_PRODUCER` (case-insensitive). It runs for every task, beside "document saved and opens".
- [ ] **Step 4: Run the suite** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(bench): check a document was made in the editor and every slide's shapes stay on it"`

### Task 5: Length floors

**Files:**
- Modify: `benchmarks/office_bench/obench/tasks.py:306`
- Test: `benchmarks/office_bench/tests/test_tasks.py`

- [ ] **Step 1: Write the failing test** `test_minutes_floor`: the docx-minutes task's length check is `"length 80-600 words"`.
- [ ] **Step 2: Run it** — Expected: FAIL (`100-600`).
- [ ] **Step 3: Change docx-minutes to `w_words(80, 600)`.** The notes it is written from hold about 70 words; minutes of 96 words cover them. The other floors stay: they match what a reader asked for (a two-page report is at least 600 words).
- [ ] **Step 4: Run the suite** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "fix(bench): meeting minutes need 80 words, not 100"`

### Task 6: The baseline, re-scored

**Files:**
- Modify: `benchmarks/office_bench/RESULTS.md`, `benchmarks/office_bench/README.md` (Requirements)

- [ ] **Step 1: Bring the stored run.** Copy the stored run `full-001` from the worktree where it was made into this worktree's `benchmarks/office_bench/runs/` (git-ignored).
- [ ] **Step 2: Set the renderer for this shell only** (never committed): `OFFICEBENCH_RENDER_SHELL` = an exec into the DEV document server's converter, e.g. `kubectl exec -i <converter pod> --` with DEV's kubeconfig (never PROD's). Check with one deck: `obench check full-001` renders `pptx-pitch/r1` and writes `render/`.
- [ ] **Step 3: Re-score** — `.venv/bin/obench check full-001 && .venv/bin/obench report full-001` — Expected: no "estimated from geometry" check; the 9 rollouts that failed only on it now pass that check; pptx-onboarding r1 fails contrast; the 6 scripted rollouts fail "made in the editor"; docx-minutes r2 and r3 pass length.
- [ ] **Step 4: Write RESULTS.md**: one row `| 2026-10-17 | full-001 | DEV | surogate3.7-flash | 33 × 3 | <checks %> | <clean>/99 |`, the failing classes as in the design's table (no agent name, ids or hosts), and how to read the run (the advisor off, one session at a time). Run `tests/test_public.py` again.
- [ ] **Step 5: Commit** — `git add benchmarks/office_bench/RESULTS.md benchmarks/office_bench/README.md; git commit -m "docs(bench): the bar model's office baseline"`
- [ ] **Step 6: Push and open the PR** (`gh pr create --base master`), body: what the benchmark is, the baseline, how to run it. Ask Flavius before merging.
