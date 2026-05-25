# Inline small attachments at send time — design

**Date:** 2026-05-25
**Status:** Awaiting implementation plan
**Scope:** `surogates` harness (`/work/surogates/`)
**Depends on:** [2026-05-24-harness-native-document-read-design.md](2026-05-24-harness-native-document-read-design.md)

## Problem

When a user attaches a small document (PDF, Word, spreadsheet, text
file) to a chat message and asks "summarise this", the agent today has
to call `read_file(path)` as a separate tool turn before it can act.
That extra round-trip is wasted work for the common case of *short
prompt + one small attachment*: the user clearly wants the content
read, and the harness can do that synchronously at send time.

Images already travel through the existing `images` payload: vision-
capable models receive base64 data URLs, and non-vision models are
handled by the current vision-preflight path in `harness/loop.py`.
Documents, spreadsheets, and plain text files referenced through
`AttachmentRef` do not get any equivalent treatment.

## Goal

When a user posts a message with attachments, the harness's message-
resolution path inlines parsed content directly into the user message
for files that meet a size threshold and a supported format.  The agent
sees the content immediately in the user turn; it doesn't need to call
`read_file` for the small-attachment case.

## Non-goals

- Pre-parsing at upload time (parsing during `POST /workspace/upload`).
  Send-time is simpler and parses on use; can be added later if the
  per-message latency becomes problematic.
- Image OCR inlining as a default.  Scanned image-only PDFs continue to
  go through the `ocr-and-documents` skill on agent demand.
- Streaming the inlined content chunk-by-chunk.  Whole-block is fine;
  the parsers are fast enough for files under the cap.
- Per-org or per-user threshold overrides.  A single constant is good
  enough for v1; make it env-var-driven later if needed.
- Re-running document parsing when a model swap changes session
  capability mid-conversation.  Replay shows the artifact that was
  produced at original send time.
- Format autodetection by file header.  Extension only, matching the
  policy in `read_file`.
- Changing image handling.  Image `ImageBlock` processing stays in the
  existing base64/vision-preflight pipeline; this design covers
  document/text `AttachmentRef` entries only.

## Architecture

The change point is the attachment-resolution loop in
[surogates/api/routes/sessions.py:486-561](../../../surogates/api/routes/sessions.py).
That loop already validates each `AttachmentRef`, fetches its size
from storage, and persists the reference into the `user.message` event.
We extend the loop so that for every attachment under a fixed raw-byte
cap and of a supported type, the harness:

1. Loads the file bytes from the storage backend.
2. Parses (documents) or decodes (text) the content into UTF-8 text.
3. Stores the rendered text on the `AttachmentRef` as `inlined_text`,
   along with an `inlined_render_kind` discriminator.  If a supported
   attachment was considered but skipped, stores `inline_skip_reason`
   so the prompt note can explain the fallback.
4. Emits the augmented attachments list on the `user.message` event so
   the rendered text persists in history.

When the harness main loop assembles the LLM prompt for the user turn,
it walks each attachment with `inlined_text` set and appends a fenced
block to the user message text.  The system attachments note continues
to mention only the attachments that were not inlined.

Per-attachment decision tree:

```
size > 2 MB                          → path-only (today's behaviour)
ext in {.pdf, .docx, .xlsx, .pptx}   → parse via document cache
ext in {.txt, .md, .json,
        .csv, .tsv, .yaml, .yml,
        .log}                        → decode as UTF-8
otherwise                            → path-only
```

The parser pipeline is the one shipped in the earlier `read_file`
work: `pymupdf4llm` for PDFs and `markitdown` for office documents.
Use `surogates.tools.utils.document_cache.default_cache().get_or_parse`
rather than calling `_parse_document_to_markdown` directly.  For local
storage the source path can be the resolved workspace file.  For S3 or
other object backends, materialize the object into a deterministic temp
source path keyed by `(bucket, storage_key, size, modified)` before
calling the cache, so sending the same file across multiple messages
does not re-parse it.

Two caps gate the inline path:

- **Raw file cap (`_INLINE_MAX_BYTES = 2 MB`):** files larger than
  this never enter the parser.  Cheap to evaluate, predictable cost.
- **Rendered text cap (`_INLINE_RENDERED_CAP_CHARS = 200 K`):** belt
  and suspenders against a small file producing huge markdown (text
  files with very long lines, weird OCR output).  Files that exceed
  the secondary cap fall back to path-only.

### Why send-time and not upload-time

- Parse only when the file is referenced from a message.  Uploads that
  never get attached to a turn (drag-and-drop then cancelled) don't
  pay the parser cost.
- History replay determinism is easier when the produced artifact is
  attached to the same event that introduced it.

## Components

### `AttachmentRef` extension

In [surogates/api/routes/sessions.py:88](../../../surogates/api/routes/sessions.py):

```python
class AttachmentRef(BaseModel):
    path: str
    filename: str
    mime_type: str | None = None
    size: int | None = None
    inlined_text: str | None = None
    inlined_render_kind: Literal[
        "markdown", "text",
    ] | None = None
    inline_skip_reason: Literal[
        "parse_error", "parse_timeout", "decode_error",
        "oversize_output", "empty_output",
    ] | None = None
```

All new fields are server-set.  A `model_validator(mode="before")` in
the request model strips them from any client payload defensively so a
hostile client cannot inject content into its own user message or spoof
an inline failure reason.

### `_INLINE_MAX_BYTES` constant

```python
_INLINE_MAX_BYTES = 2 * 1024 * 1024  # 2 MB raw cap for document/text inline
```

### `_inline_extension_kind`

Pure-function dispatcher in `sessions.py`:

```python
def _inline_extension_kind(
    filename: str,
) -> Literal["document", "text"] | None:
    ext = os.path.splitext(filename)[1].lower()
    if ext in {".pdf", ".docx", ".xlsx", ".pptx"}:
        return "document"
    if ext in {".txt", ".md", ".json", ".csv", ".tsv",
               ".yaml", ".yml", ".log"}:
        return "text"
    return None
```

### `_try_inline_attachment`

Async helper that runs the per-attachment decision and returns
`(inlined_text, render_kind, skip_reason)`.  Lives in `sessions.py`.

```python
async def _try_inline_attachment(
    attachment: AttachmentRef,
    raw_bytes: bytes,
    document_path: Path | None,
) -> tuple[str | None, str | None, str | None]:
    if attachment.size is not None and attachment.size > _INLINE_MAX_BYTES:
        return None, None, None
    kind = _inline_extension_kind(attachment.filename)
    if kind == "document":
        if document_path is None:
            return None, None, "parse_error"
        try:
            md = await default_cache().get_or_parse(
                document_path, _parse_document_to_markdown,
            )
        except DocumentParseError as exc:
            reason = (
                "parse_timeout"
                if "timeout" in exc.reason.lower()
                else "parse_error"
            )
            logger.info(
                "event=attachment.inline result=skip reason=%s "
                "filename=%s err=%s", reason, attachment.filename, exc.reason,
            )
            return None, None, reason
        if not md.strip():
            return None, None, "empty_output"
        if len(md) > _INLINE_RENDERED_CAP_CHARS:
            logger.info(
                "event=attachment.inline result=skip reason=oversize_output "
                "filename=%s chars=%d", attachment.filename, len(md),
            )
            return None, None, "oversize_output"
        return md, "markdown", None
    if kind == "text":
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return None, None, "decode_error"
        if len(text) > _INLINE_RENDERED_CAP_CHARS:
            return None, None, "oversize_output"
        return text, "text", None
    return None, None, None
```

Secondary cap to prevent the "small file, huge markdown" edge case:

```python
_INLINE_RENDERED_CAP_CHARS = 200_000  # 200 KB of text / markdown
```

### Renderer in `harness/loop.py`

A new `_render_inlined_attachments(text, attachments)` function emits
one fenced block per inlined attachment, appended to the user's text
content in attachment order:

````
<user's original prompt>

---
**Attachment: report.pdf**
*(parsed via markitdown/pymupdf4llm — to re-read or paginate, use `read_file("uploads/report.pdf")`)*

<parsed markdown body>
---
````

For `text` kind, the subtitle is omitted (the file is just plain text;
no parser involvement to disclose).

### `_attachments_note` revision

At [surogates/harness/loop.py:221](../../../surogates/harness/loop.py),
the existing note generator is modified to skip any attachment whose
`inlined_text` is set.  If all attachments were inlined the function
returns `None` and the harness omits the system note entirely.

When inline failed for a specific attachment (parse error, decode
error, etc.), the note for that file gains a `reason` annotation —
e.g., `"path=uploads/foo.pdf (inline skipped: parse_error — try read_file with pdftotext/pandoc fallbacks)"` —
so the agent has context for why it has to call `read_file` for that
particular path.

## Data flow

### Happy path — small document attachment

1. Client posts `POST /sessions/{id}/messages` with content + one
   attachment (`report.pdf`, 800 KB).
2. The route handler validates the attachment, resolves its storage
   key, fetches the raw bytes.
3. `_try_inline_attachment(...)`:
   - Size ≤ 2 MB → proceed.
   - Ext `.pdf` → parse through the document cache (uses
     pymupdf4llm + the `/tmp` cache).
   - Markdown ≤ 200 KB → return `(md, "markdown", None)`.
4. `ref.inlined_text = md`; `ref.inlined_render_kind = "markdown"`.
5. `user.message` event emitted with the augmented attachments list.
6. Harness main loop runs:
   - `_attachments_note` returns `None` (all attachments inlined).
   - `_render_inlined_attachments` composes the user text:
     `<original prompt> + "\n\n---\n**Attachment: report.pdf**\n…"`.
7. LLM call sees one user message with the markdown embedded.  Agent
   does not call `read_file`.

### History replay

The persisted `user.message` event already contains
`attachments[*].inlined_text`.  On compaction, resume-after-pause, or
worker restart, the harness rebuilds the LLM history from events.  The
same fenced block reappears in the same user message — bit-for-bit
identical to what the LLM saw originally.  No re-parsing on replay.
Model changes between original send and replay are explicitly out of
scope for v1.

### Mixed batch (some inlined, some not)

User attaches `small.pdf` (300 KB) + `huge.pdf` (8 MB) to one message:

- `small.pdf`: parsed, inlined.
- `huge.pdf`: skipped (over 2 MB cap), stays path-only.
- LLM user message: original prompt + fenced block for `small.pdf`.
- System attachments note: covers `huge.pdf` only.

## Error handling

| Case | Behaviour |
|---|---|
| File > 2 MB | Skip inline silently; path-only. Existing attachments-note covers it. |
| Extension not in inline set | Skip inline silently. |
| `DocumentParseError` (encrypted PDF, corrupt zip, etc.) | Skip inline; path-only; attachments-note gains `(inline skipped: parse_error — try read_file with pdftotext/pandoc fallbacks)`. Log `reason=parse_error`. |
| Parse timeout (30 s wall clock from `_DOCUMENT_PARSE_TIMEOUT_S`) | Same as parse failure with `reason=parse_timeout`. |
| Text file not UTF-8 decodable | Skip inline; path-only. The agent can still call `read_file` (which has the full BOM ladder). Log `reason=decode_error`. |
| Parsed markdown > 200 KB secondary cap | Skip inline; path-only; attachments-note gains `(inline skipped: oversize_output — use read_file)`. Log `reason=oversize_output`. |
| Parsed markdown empty (markitdown sometimes returns `""` for scans) | Skip inline; path-only; same `reason=empty_output`. |
| Storage backend fails to fetch the file | Surface the existing 404/500 to the client — do not swallow; the upload state is broken and the user should know. |
| Client supplies inline fields themselves | Strip `inlined_text`, `inlined_render_kind`, and `inline_skip_reason` server-side before processing. Same defence we use for other server-set fields. |

### Explicit non-behaviours

- **No retries.**  One attempt per inline path, then path-only fallback.
  The agent can still call `read_file` later.
- **No partial inline.**  If markdown is truncated by the secondary
  cap, we don't show the head and hide the rest — that would mislead
  the LLM into thinking it has the whole document.  Whole or nothing.
- **No async parsing.**  The parse runs synchronously in the
  send-message path (with the 30 s parser timeout).  A 2 MB PDF parses
  in under a second with pymupdf4llm; the latency is acceptable.
  Going async would require event re-emission later, which complicates
  history determinism.
- **No image inlining.**  Image uploads continue through the existing
  `images` payload and model-vision compatibility code.

## Logging

One structured log line per inline attempt:

```
event=attachment.inline filename=<name> bytes=<N> rendered_chars=<M>
  kind=document|text|none result=ok|skip reason=<...>
```

Goes through the existing harness logger; no new sink.

## Cost profile change

- **Send-message latency** increases by ~50-500 ms per inlined document
  attachment.  Sub-second for the common single-PDF case.
- **Prompt tokens** for the message that carries the attachment
  increase by up to 200 KB of text (~50 K tokens worst case).  On a
  200 K-context model that's 25% of the window for one turn — fine
  for the common case where the user explicitly wants the content.
- **Subsequent turns** see the same inlined text (history is rebuilt
  from events).  When prompt compaction runs, the inlined blocks
  compress like any other text content; no special handling.
- **Image attachments:** zero change.  Same base64 or existing
  vision-preflight path, same costs.

## Testing

### Unit tests — `tests/api/test_attachment_inlining.py` (new)

| Test | Asserts |
|---|---|
| `inline_pdf_under_cap_produces_markdown` | `.pdf` < 2 MB → `inlined_text` non-empty + probe string present + `inlined_render_kind="markdown"` |
| `inline_docx_under_cap_produces_markdown` | Same for docx |
| `inline_xlsx_under_cap_includes_sheet_names` | Both sheet names appear in the rendered markdown |
| `inline_pptx_under_cap_includes_slide_text` | Slide titles appear |
| `inline_txt_under_cap_uses_text_kind` | `.txt`/`.md`/`.json`/`.csv` → `inlined_render_kind="text"`, raw UTF-8 verbatim |
| `inline_skipped_for_oversize_raw_bytes` | File > 2 MB → `inlined_text is None`, path-only |
| `inline_skipped_for_unsupported_extension` | `.zip`/`.exe`/`.unknown` → no inline |
| `inline_skipped_when_parse_raises` | Monkeypatched parser raises `DocumentParseError` → no inline, log line present |
| `inline_skipped_on_decode_error` | Non-UTF-8 `.txt` → no inline |
| `inline_skipped_when_markdown_exceeds_secondary_cap` | Parsed output > 200 KB → no inline |
| `inline_skipped_when_markdown_empty` | Parser returns `""` → no inline; `reason=empty_output` |
| `inline_skip_reason_persisted_for_supported_failures` | parse/decode/empty/oversize failures persist `inline_skip_reason` on the event |
| `client_supplied_inline_fields_are_stripped` | Inbound `AttachmentRef` with `inlined_text`/`inline_skip_reason` set by client → server discards them before processing |
| `document_materialization_uses_stable_cache_source_for_s3` | Re-sending the same S3-backed document reuses the document cache key instead of parsing twice |

### Renderer tests — `tests/harness/test_attachment_rendering.py` (new)

| Test | Asserts |
|---|---|
| `render_inlined_appends_fenced_block_to_text` | `_render_inlined_attachments` emits a fenced block with filename + body for `kind=markdown` |
| `render_inlined_text_kind_omits_parser_subtitle` | `kind=text` block has no "parsed via …" subtitle |
| `render_inlined_handles_multiple_attachments_in_order` | Two inlined attachments → two fenced blocks in attachment order |
| `render_inlined_skips_path_only_attachments` | Path-only attachments do not appear in the rendered text |
| `attachments_note_filters_inlined` | Note returns `None` when all attachments are inlined; non-empty when some are path-only |
| `attachments_note_includes_parse_failure_diagnostic` | For inline-skipped attachments with a known reason, the note mentions that reason next to the path |

### Integration test — `tests/integration/test_inline_attachments_e2e.py` (new)

Parametrised across `pdf / docx / xlsx / pptx / txt`:

1. Boot a minimal harness session (use the same fixtures as
   `test_read_document_e2e.py`).
2. Build the attachment file via the existing fixture builders.
3. Post a message that references the attachment.
4. Fetch the resulting `user.message` event.
5. Assert `data["attachments"][0]["inlined_text"]` is non-empty.
6. Assert the text contains the format-specific probe string.

### History-replay regression — `tests/integration/test_attachment_history_replay.py` (new)

Critical because we're persisting parser output into events:

1. Upload a PDF and send a message → captures
   `user.message.attachments[0].inlined_text`.
2. Pause the session.  Restart the worker process.
3. Resume the session.  The harness rebuilds the LLM history from
   events.
4. Assert the rebuilt user message text contains the exact same fenced
   block (byte-for-byte).  No re-parse was triggered.

## Rollout

1. **No feature flag.**  Same reasoning as the `read_file` work — gating
   it just delays the win and forces the agent to know the flag exists.
   The size cap + parse-failure fallback are the safety nets.
2. **One PR.**  Model extension + helpers + renderer + note revision +
   tests, all together.
3. **API/harness image rebuild required.**  Inline parsing starts in
   the send-message route and rendering happens in the harness loop.
   After deploying the new code, restart both processes if they are
   packaged separately; existing sessions pick up the change on the
   next message.
4. **Sandbox image:** unaffected by this work.  `read_file` continues
   to function for the over-threshold and "agent-initiated re-read"
   cases.
5. **Docs:**
   - Update [docs/tools/index.md](../../tools/index.md) to mention that
     small attachments are inlined automatically.
   - The existing `pdf`/`docx`/`xlsx`/`pptx` skills already lead with
     `read_file` — no change needed.
6. **Frontend:** no change required.  Clients keep posting
   `LiveMessageRequest` as before; inlining is invisible to the UI.

## Open questions

- Whether to surface the inlined content to the frontend so the UI can
  show "expanded attachment preview" without re-fetching.  Not blocking
  v1 — the frontend already has the file path and can render its own
  preview.
