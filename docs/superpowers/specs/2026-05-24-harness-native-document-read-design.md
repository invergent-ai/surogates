# Harness native document Read — design

**Date:** 2026-05-24
**Status:** Reviewed, awaiting implementation plan
**Scope:** `surogates` harness (`/work/surogates/`)

## Problem

When an agent receives a non-text file (PDF, .docx, .xlsx, .pptx), it currently
has to upload the file to its workspace, install extraction libraries via the
terminal, write a script to extract text, run it, and only then analyze the
content. This burns turns on bootstrap work and produces noisy transcripts.

Claude Code's `Read` tool handles these formats natively — the agent calls
`Read(path)` once and gets parsed text back. We want the surogates harness to
behave the same way.

The libraries needed are already pre-installed in the sandbox image
(`pypdf`, `python-docx`, `openpyxl`, `python-pptx`, `markitdown`, `pandoc`);
`read_file` just doesn't call them. This is a wiring change, not an
infrastructure change.

## Goal

`read_file(path)` (the Surogates equivalent of Claude Code's `Read`) returns
parsed markdown for `.pdf`, `.docx`, `.xlsx`, `.pptx`,
and a vision-analysis description for image files, using the existing
`offset`/`limit` semantics. Same tool, same interface, expanded coverage.

## Non-goals

- Legacy binary formats `.doc/.xls/.ppt` (require LibreOffice subprocess).
- OCR for scanned PDFs.
- Image extraction from PDFs / docx (markitdown skips embedded images;
  we don't override that).
- Format autodetection by file header. Extension only.
- Per-format pagination parameters (`page=N`, `sheet=Name`). The existing
  line-based `offset`/`limit` is sufficient.
- A new tool. We extend `read_file`; we do not add `read_document`.

## Architecture

The document change point is `_read_file_handler` in
[surogates/tools/builtin/file_ops.py:804](../../../surogates/tools/builtin/file_ops.py).
In production, `read_file` is sandbox-routed; the K8s sandbox runs
[images/sandbox/tool-executor](../../../images/sandbox/tool-executor), which
imports the same `surogates.tools.builtin.file_ops` module inside the sandbox
image. That means changing `file_ops.py` is still the right source-level edit
for document reads, but production behavior changes only after rebuilding the
sandbox image.

Important runtime boundary: image analysis cannot run inside `tool-executor`.
The sandbox process has the workspace files, but it does not have the worker's
LLM clients or vision configuration. Image reads are therefore handled by a
worker-side pre-dispatch branch in
[surogates/harness/tool_exec.py](../../../surogates/harness/tool_exec.py):
after the existing governance/path checks and before sandbox dispatch, if
`tool_name == "read_file"` and `path` has an image extension, the worker calls
`vision_analyze` with the same path and renders the response as a `read_file`
result. Document reads continue through the sandbox.

Inside `file_ops.py`, `_read_file_handler` is reshaped into an explicit format
dispatcher:

```python
async def _read_file_handler(path, offset, limit, ...):
    ext = path.suffix.lower()
    handler = _resolve_format_handler(ext)  # document | text
    return await handler(path, offset, limit, ...)
```

Two sandbox-safe handlers, each independently testable:

- `_handle_document(path, offset, limit)` — routes `.pdf/.docx/.xlsx/.pptx`
  through `markitdown`, returns markdown.
- `_handle_text(path, offset, limit, ...)` — the existing UTF-8 / BOM /
  encoding path, lifted verbatim into its own function. No behavior change.

`_resolve_format_handler` consults `_DOCUMENT_EXTENSIONS` and falls through to
text. `.docx/.xlsx/.pptx` are removed from `BINARY_EXTENSIONS` in
[surogates/tools/utils/binary_extensions.py:21](../../../surogates/tools/utils/binary_extensions.py).
`.pdf` is already absent.

A shared helper `_apply_line_window(lines, offset, limit)` is extracted so
that the document, image, and text paths use identical slicing logic.

### Why a refactor instead of an inline branch

The current `_read_file_handler` mixes encoding detection, character-count
guards, and content shaping. Splitting it means the new document path
cannot regress the UTF-8 path, and each handler is independently testable.
The refactor itself is mechanical function extraction; the risk surface is
small.

## Components

### `_handle_document(path, offset, limit) -> ReadFileResult`

New, in `file_ops.py`.

- Calls `_document_cache.get_or_parse(path)` → markdown string.
- Splits markdown into lines.
- Applies `offset` / `limit` via `_apply_line_window`.
- Returns a `ReadFileResult` with `content`, `total_lines`, `truncated`
  identical in shape to the text path.

### Worker image read branch

New, in `tool_exec.py`, before sandbox dispatch.

- Runs only when the original tool call is `read_file` and the requested path
  has an image extension.
- Preserves the emitted tool name/result as `read_file`; this is an
  implementation detail, not a new user-facing tool.
- Calls `_image_cache.get_or_analyze(path, kwargs)` in the worker process.
- Renders the result as markdown: a one-line header `# Image: {filename}`
  followed by the analysis body.
- Uses the same `_apply_line_window` slicing as documents.
- If vision LLM is unconfigured, `_vision_analyze_handler` returns a JSON
  error; we unwrap it and surface a tool-error envelope:
  > `"Read cannot analyze images: no vision LLM is configured. Use the terminal to inspect the file (e.g. file <path>) or ask an operator to configure vision."`

The standalone `vision_analyze` tool remains available for question-specific
analysis and bypasses this cache.

### `_document_cache` — sandbox-local file cache

New helper in `file_ops.py`.

- **Key:** `(absolute_path, mtime_ns, size)`. Including mtime+size means
  edits invalidate naturally without manual flushing.
- **Value:** rendered markdown string.
- **Bound:** 8 entries, per-entry cap of 2 MB markdown (oversized docs are
  not cached, just re-parsed). Worst-case memory footprint ~16 MB.
  Constants are tunable.
- **Storage:** files under `/tmp/surogates-read-cache/documents/`, keyed by a
  SHA-256 digest of `(absolute_path, mtime_ns, size, ext)`.
- **Eviction:** LRU by cache-file access time, bounded to 8 entries.
- **Safety:** atomic `os.replace` writes. A best-effort `fcntl.flock` lock file
  prevents two concurrent `tool-executor` invocations from writing the same
  cache entry at once.

This must be file-backed, not only module-level: the K8s sandbox starts a
fresh `tool-executor` Python process for every sandbox tool call, so an
in-memory LRU would be empty on every subsequent `read_file` call and would not
make pagination free.

### `_image_cache` — worker in-memory LRU

- **Key:** local workspaces use `(absolute_path, mtime_ns, size)`; storage-backed
  workspaces use `(session_id, workspace_key, size, modified)` from
  `StorageBackend.stat(...)`. Both are implicitly scoped to the default
  analysis prompt (`read_file` always uses it).
- **Value:** analysis text string. Same 2 MB / 8-entry bounds.
- **Storage:** worker-process memory. This is enough for repeated reads within
  a worker-handled turn/session, but it is not a cross-worker distributed
  cache. A later shared cache can be added if image costs show up in
  production telemetry.

### `_parse_document_to_markdown(path) -> str`

New private helper.

- Single backend: `markitdown.MarkItDown().convert(str(path)).text_content`.
- Imports `markitdown` lazily inside the helper. `file_ops.py` is imported by
  the worker to register tool schemas, and the worker Python environment does
  not currently declare `markitdown` in `pyproject.toml`; eager imports would
  break worker startup unless we also promote the dependency to the worker
  image.
- Wrapped in `asyncio.to_thread(...)` — markitdown is sync and PDF parsing
  can take seconds.
- Wrapped in `asyncio.wait_for(..., timeout=30)` for a 30 s wall-clock cap.
- Raises `DocumentParseError(path, reason)` on any exception, preserving
  the original message.

### `DocumentParseError`

New exception type. Caught by `_handle_document`, converted into the
harness's standard tool-error envelope with this message:

> `"Could not parse {path} as a {ext} document: {reason}. You can retry with a subprocess fallback: try running `pdftotext`, `pandoc`, or a Python script using pypdf/python-docx/openpyxl (all pre-installed)."`

### Tool description update

The `read_file` tool's description (registered in `file_ops.py`) gets an added
sentence:

> *"For .pdf, .docx, .xlsx, .pptx, and image files this tool returns the document parsed to markdown (or a vision analysis for images) — do not pre-extract with subprocess tools."*

This is the **load-bearing** piece for behavior change: without it, the
agent will keep reflexively reaching for `pip install pypdf`. The harness
can support native parsing, but the agent has to know to trust it.

### `BINARY_EXTENSIONS` shrink

In [binary_extensions.py:21-23](../../../surogates/tools/utils/binary_extensions.py),
remove `.docx`, `.xlsx`, `.pptx`. Keep `.doc/.xls/.ppt` (legacy binary
formats — markitdown can't handle them without LibreOffice).

## Data flow

### Happy path — `read_file("workspace/report.pdf")`

1. `_read_file_handler` resolves `.pdf` → `_handle_document`.
2. `_document_cache.get_or_parse(path)` checks
   `(abs_path, mtime_ns, size)`:
   - **Hit** → loads cached markdown from `/tmp/surogates-read-cache`.
   - **Miss** → takes the cache lock, calls
     `asyncio.to_thread(_parse_document_to_markdown, path)`, atomically stores
     result (if ≤ 2 MB), releases lock.
3. Markdown is split into lines; `_apply_line_window(lines, offset, limit)`
   slices.
4. Result returned with `content`, `total_lines`, `truncated`.

### Subsequent reads with different offsets

Hit the cache — no re-parse. Pagination is effectively free.

### File replaced after upload

mtime changes → cache miss → re-parse. No manual flush.

### Image read — `read_file("workspace/chart.png")`

1. `execute_single_tool` recognizes `read_file` + image extension after the
   normal policy checks.
2. `_image_cache.get_or_analyze(path)` checks the same key shape:
   - **Hit** → returns cached analysis text immediately.
   - **Miss** → calls `_vision_analyze_handler` with the default prompt,
     stores result, releases lock.
3. Rendered as `# Image: chart.png\n\n{analysis}`, sliced by
   `_apply_line_window`.
4. The returned tool result is shaped like `read_file`, not `vision_analyze`,
   so the LLM sees one consistent file-reading interface.

## Error handling

| Case | Behavior |
|---|---|
| File not found | Existing `FileNotFoundError` path — unchanged |
| Permission denied | Existing handler — unchanged |
| Markitdown raises (encrypted PDF, corrupt zip, bad xlsx) | `DocumentParseError` → tool-error envelope with fallback hint |
| Parsed markdown > 2 MB | Don't cache; still return the windowed result. Log a debug line. |
| Parser exceeds 30 s wall-clock | `asyncio.wait_for` timeout → `DocumentParseError("parse timeout after 30s")` with fallback hint |
| `offset` beyond `total_lines` | Existing behavior — empty content + `total_lines`, identical to text path |
| Path is a directory or symlink to one | Existing handler — unchanged |
| No vision LLM configured | Tool-error envelope (above); no cache write |
| Vision API timeout / network error | Tool-error envelope; no cache write; agent can retry |
| Image unreadable (corrupt, unsupported format) | Surface `vision_analyze`'s own error message |

### Explicit non-behaviors

- **No retry loop on parse failure.** One attempt; if it fails, hand off
  to the agent.
- **No partial extraction.** If markitdown fails halfway through a 200-page
  PDF, we don't return the first 100 pages with a warning — that hides
  bugs. Whole-or-error.
- **No format autodetection.** Extension only. A `.txt` file with a PDF
  header is still treated as text; an actual PDF without `.pdf` is treated
  as text (and will likely be unreadable, which is fine — the agent
  renames or extracts manually).

## Logging

One structured log per parse:

```
event=document.parse path=<rel> ext=<.pdf> bytes_in=<N>
  bytes_md=<M> duration_ms=<T> cached=<bool>
```

One structured log per vision call:

```
event=image.analyze path=<rel> bytes_in=<N> duration_ms=<T> cached=<bool>
```

Both go through the existing harness logger; no new sink.

## Cost profile change

Every `read_file(image.png)` on a cache miss now spends a vision-LLM call.
This is what Claude Code does, and the cache makes pagination free and
makes opening the same image twice in a session free. Opening many
distinct images, however, is no longer free.

An env var `READ_IMAGE_CACHE_DISABLED` (default off) lets operators kill
the image cache if vision costs become visible. Documents don't need this
switch; their backend is local.

## Testing

### Unit tests — `tests/tools/test_file_ops_documents.py` (new)

| Test | Fixture | Asserts |
|---|---|---|
| `read_pdf_returns_markdown` | tiny 2-page PDF with known headings | output contains both headings; `total_lines > 0`; `truncated=False` |
| `read_docx_returns_markdown` | docx with H1, bullet list, table | bullets render as `- `, table as pipe table |
| `read_xlsx_multi_sheet` | xlsx with 2 sheets | both sheet names appear in output |
| `read_pptx_returns_markdown` | minimal pptx | slide text appears |
| `pagination_uses_offset_limit` | larger PDF | `read_file(path, offset=50, limit=20)` returns lines 51–70 of full markdown |
| `cache_hit_skips_reparse` | any document | second read with different offset doesn't call markitdown again (monkeypatched counter) |
| `cache_invalidates_on_mtime` | overwrite file between calls | second read re-parses |
| `cache_survives_tool_executor_processes` | any document | two separate `tool-executor read_file ...` subprocesses share the `/tmp` cache |
| `oversized_markdown_not_cached` | PDF that renders > 2 MB markdown | still returns content; cache stays empty |
| `corrupt_pdf_returns_error_with_fallback_hint` | truncated PDF bytes | tool-error envelope; message mentions `pdftotext` / `pandoc` |
| `parse_timeout_returns_error` | mock markitdown to sleep > 30 s | tool-error envelope with "parse timeout" |
| `legacy_doc_still_blocked` | `.doc` (legacy binary) | still blocked via `BINARY_EXTENSIONS` |
| `text_path_unchanged` | `.py`, `.md`, BOM-prefixed UTF-16 | identical bytes to pre-refactor |

### Image tests — same file

| Test | Asserts |
|---|---|
| `read_png_routes_to_worker_vision` | monkeypatched vision handler returns "test analysis"; `read_file` output contains it under `# Image:` header and sandbox dispatch is not called |
| `read_image_caches_analysis` | second read doesn't re-invoke vision |
| `read_image_no_vision_configured` | tool-error envelope with "vision LLM is configured" hint |

### Integration test — `tests/integration/test_read_document_e2e.py` (new)

Boot a minimal harness session, upload a real PDF via the workspace upload
route, have the agent call `read_file(path)`, assert the returned content
matches expected markdown. One test per format; uses fixtures that ship
with the repo.

### Regression guard

Snapshot test on the text-handler output for 5 representative files
(Python, Markdown, JSON, UTF-16-LE, empty file). The refactor is
mechanical, but snapshots catch any drift from the function extraction.

## Rollout

1. **No feature flag.** This is a Read-tool behavior change; gating it
   would just delay the win and require an agent to know the flag exists.
   The fallback hint in error paths is the safety net.
2. **One PR.** Refactor + new handlers + cache + tests + tool-description
   update + `BINARY_EXTENSIONS` edit, all in one commit. Easier to review
   as a unit than split.
3. **Rebuild the sandbox image.** Document parsing runs inside the sandbox
   `tool-executor`, so deploying code without rebuilding
   `ghcr.io/invergent-ai/surogates-agent-sandbox` leaves production document
   reads on the old behavior.
4. **Image cache disable switch.** `READ_IMAGE_CACHE_DISABLED` env var
   (default off) so operators can kill the image cache if vision costs
   become visible.
5. **Documentation:** update [CLAUDE.md](../../../CLAUDE.md) to mention
   `read_file` now handles PDF/Office/images natively, so agents reading the
   project guide see the new behavior.

## Open questions

- Whether the worker image should also declare `markitdown[pptx]` in
  `pyproject.toml`. It is not required for production document reads because
  those run in the sandbox image, but adding it would make harness-local
  fallback behavior identical in dev/test environments.
- Whether `/tmp/surogates-read-cache` is the right cache location for K8s pods.
  It survives repeated exec calls in the same pod and avoids polluting the
  user workspace, but it disappears when the pod is replaced.
- Implementation may surface markitdown quirks on specific real-world
  documents — those are addressed during the test pass.
