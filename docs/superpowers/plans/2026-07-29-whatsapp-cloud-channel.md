# WhatsApp Business Cloud API Channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `whatsapp` as a fourth managed channel in the surogates harness, using Meta's official WhatsApp Business Cloud API, wired end-to-end through the ops control plane and Surogate Studio.

**Architecture:** A new `WhatsAppPlatform` implementing the structural `ChannelPlatform` protocol (`surogates/channels/registry.py:104`), mounted by the existing auto-generating webhook dispatcher at `/whatsapp/{phone_number_id}`. Each tenant brings their own Meta App; credentials live per-tenant in the surogates vault. One small framework change adds a GET route for Meta's callback-URL handshake. Outbound flows through the existing durable Postgres outbox unchanged.

**Tech Stack:** Python 3.12, FastAPI, httpx, SQLAlchemy (async), pytest + respx; React 19 + TypeScript for Studio.

**Source spec:** `docs/superpowers/specs/2026-07-29-whatsapp-cloud-channel-design.md` — read §1 (product decisions) before starting. Every section reference below (§4, §5.3, …) points at that file.

## Global Constraints

- **Graph API version: pin `v23.0` or later**, configurable per tenant via `config.api_version`. Never `v20.0` — Meta removes it 2026-09-24.
- **Reactive only.** The agent never initiates. No message templates, no 24-hour-window tracker, no hold queue. WhatsApp must not be added to any ambient or scheduled routing list.
- **DM-only.** Every WhatsApp conversation is 1:1. `thread_key` is always `None`, `is_dm=True`, `visibility="dm"`.
- **No native interactive buttons.** `ask_user_question` ships as a numbered text prompt (Task 8) — but WhatsApp *must* be in `INTERACTIVE_PROMPT_CHANNELS`, or the tool writes no outbox row at all.
- **No message editing.** Do not implement `supports_edit`, `post_thinking_placeholder`, `delete_message`, `enrich`, `fetch_channel_context`, `list_channel_files`, `interactive_paths`, `handle_interactive`, `handle_non_message_update`.
- **House style:** `from __future__ import annotations` first; module-level stateless functions for `identifier_of`/`verify`/`parse` with one-line class delegates; 75-dash banner comments at module level, 66-dash inside classes; `except Exception as exc:  # noqa: BLE001` with `logger.warning("...(%s)...", exc)`; best-effort optional members never raise.
- **Commit style:** Conventional Commits (`type(scope): subject`). **No `Co-Authored-By` trailer.**
- **Branches:** `surogates` work goes on the existing `feat/whatsapp-cloud-channel` (base `master`). `surogate-ops` work needs a new `feat/whatsapp-cloud-channel` (base `main`).
- **Tests:** `uv run pytest` from `/work/surogates`. **Never `uv run` in `/work/surogate-ops`** — it reinstalls the pinned surogates wheel and clobbers the local dev install; use `pytest` directly there.

---

## File Structure

### surogates (`/work/surogates`)

| File | Responsibility |
|---|---|
| `surogates/channels/platforms/whatsapp_format.py` | **Create.** Markdown → WhatsApp markup transcoder. Pure functions, no I/O. |
| `surogates/channels/platforms/whatsapp_api.py` | **Create.** Graph API client: URL building, sends, media upload/download, error formatting. The only module that performs HTTP. |
| `surogates/channels/platforms/whatsapp.py` | **Create.** `WhatsAppPlatform` + module-level `identifier_of`/`verify`/`parse`. Protocol implementation; delegates all HTTP to `whatsapp_api`. |
| `surogates/channels/platforms/__init__.py` | **Modify.** Add the import that triggers self-registration. |
| `surogates/channels/dispatcher.py` | **Modify.** GET handshake route mounting (§4); `MEDIA:` gate → capability check (§6.4). |
| `surogates/channels/constants.py` | **Modify.** `ADAPTER_CHANNELS`, `INTERACTIVE_PROMPT_CHANNELS`, `END_USER_CHANNELS`. |
| `surogates/channels/inbound.py` | **Modify.** Pending-input platform tuple (`:679`). |
| `surogates/channels/memory_boundary.py` | **Modify.** `MANAGED_CHANNELS`. |
| `surogates/channels/delivery.py` | **Modify.** `_PERMANENT_DELIVERY_ERRORS` prefixes. |
| `surogates/session/store.py` | **Modify.** `_THREAD_DEST_FIELDS` + destination builder branch. |
| `surogates/config.py` | **Modify.** `WhatsAppChannelSettings` + `ChannelsSettings.whatsapp`. |
| `tests/test_whatsapp_format.py` | **Create.** Transcoder unit tests. |
| `tests/test_whatsapp_api.py` | **Create.** Graph client tests (respx). |
| `tests/test_whatsapp_platform.py` | **Create.** Platform tests, mirroring `tests/test_telegram_platform.py`. |
| `docs/channels/whatsapp.md` | **Create.** Operator documentation. |

### surogate-ops (`/work/surogate-ops`)

| File | Responsibility |
|---|---|
| `surogate_ops/server/services/channel_provisioning.py` | **Modify.** `CHANNEL_PROVISIONERS["whatsapp"]` + `_whatsapp_*` helpers + prepare hook. |
| `surogate_ops/core/commerce/features.py` | **Modify.** `CHANNEL_VOCAB`, `CHANNEL_LABELS`. |
| `surogate_ops/core/surogates_client.py` | **Modify.** `_format_channel_name`. |
| `surogate_ops/server/routes/agent_runtime.py` | **Modify.** `_MANAGED_CHANNELS`, `_LINKABLE_CHANNEL_KINDS`. |
| `surogate_ops/server/routes/commerce_public.py` | **Modify.** Inline channel-kind list. |
| `surogate_ops/core/db/models/operate.py` | **Modify.** `ChannelKind` constant. |
| `frontend/src/features/agents/channels-tab.tsx` | **Modify.** `CHANNEL_ENV_KEYS` + admin WhatsApp form. |
| `frontend/src/features/work/work-agent-channels-tab.tsx` | **Modify.** Connect/manage views, routing chain, list cards. |
| six further frontend files | **Modify.** Label/enum lists — Task 15. |

---

## Task Dependency Order

```
T1 whatsapp_format ──┐
T2 whatsapp_api ─────┼──> T5 send ──> T7 send_files ──> T8 wiring ──> T10 docs
T3 platform inbound ─┘         │
T4 dispatcher GET ─────────────┘
T6 optional members (after T2)
T9 error classification (independent)
── ops ──
T11 provisioner ──> T12 ops vocab ──> T13 admin form ──> T14 Studio views ──> T15 remaining lists
```

---

## Task 1: WhatsApp markdown transcoder

**Files:**
- Create: `surogates/channels/platforms/whatsapp_format.py`
- Test: `tests/test_whatsapp_format.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `render_whatsapp(text: str) -> str` — converts markdown to WhatsApp markup. Used by Task 5's `send`.

WhatsApp markup is `*bold*`, `_italic_`, `~strike~`, ` ```mono``` `. Model output routinely contains markdown despite the prompt telling it not to, so this is not optional. Mirrors `telegram_format.py` in structure but emits WhatsApp markup, not HTML.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_whatsapp_format.py`:

```python
"""Tests for the markdown → WhatsApp markup transcoder.

Written BEFORE the implementation module exists (TDD).
"""

from __future__ import annotations

from surogates.channels.platforms.whatsapp_format import render_whatsapp


# ---------------------------------------------------------------------------
# Emphasis conversion
# ---------------------------------------------------------------------------


class TestEmphasis:
    def test_double_asterisk_becomes_single(self):
        assert render_whatsapp("**bold**") == "*bold*"

    def test_double_underscore_becomes_single_asterisk(self):
        assert render_whatsapp("__bold__") == "*bold*"

    def test_single_asterisk_italic_becomes_underscore(self):
        assert render_whatsapp("*italic*") == "_italic_"

    def test_double_tilde_becomes_single(self):
        assert render_whatsapp("~~struck~~") == "~struck~"

    def test_triple_asterisk_becomes_bold_italic(self):
        # The reference degrades this to "**bold italic**" (a stray asterisk on
        # each side). We emit valid WhatsApp nesting instead.
        assert render_whatsapp("***both***") == "*_both_*"

    def test_plain_text_unchanged(self):
        assert render_whatsapp("just words") == "just words"

    def test_empty_string(self):
        assert render_whatsapp("") == ""


# ---------------------------------------------------------------------------
# Headers and links
# ---------------------------------------------------------------------------


class TestHeadersAndLinks:
    def test_header_becomes_bold(self):
        assert render_whatsapp("# Title") == "*Title*"

    def test_deep_header_becomes_bold(self):
        assert render_whatsapp("###### Deep") == "*Deep*"

    def test_header_only_at_line_start(self):
        assert render_whatsapp("not # a header") == "not # a header"

    def test_link_becomes_text_then_url(self):
        assert render_whatsapp("[docs](https://x.dev)") == "docs (https://x.dev)"

    def test_image_link_drops_bang(self):
        # The reference emits "!alt (url)". We drop the bang.
        assert render_whatsapp("![alt](https://x.dev/i.png)") == "alt (https://x.dev/i.png)"

    def test_url_containing_parenthesis_is_preserved(self):
        # The [^)]+ capture stops at the first ')', but the uncaptured tail
        # passes through literally, so the output is byte-identical.
        src = "[wiki](https://x.dev/a(b))"
        assert "https://x.dev/a(b)" in render_whatsapp(src)


# ---------------------------------------------------------------------------
# Code protection — the sentinel technique
# ---------------------------------------------------------------------------


class TestCodeProtection:
    def test_fenced_block_contents_untouched(self):
        src = "```\n**not bold**\n```"
        assert "**not bold**" in render_whatsapp(src)

    def test_inline_code_contents_untouched(self):
        assert render_whatsapp("`**raw**`") == "`**raw**`"

    def test_text_outside_fence_still_converted(self):
        src = "**yes**\n```\n**no**\n```\n**yes**"
        out = render_whatsapp(src)
        assert out.startswith("*yes*")
        assert out.endswith("*yes*")
        assert "**no**" in out

    def test_eleven_fences_restore_in_order(self):
        # Guards the trailing-\x00 sentinel delimiter: without it, restoring
        # placeholder 1 corrupts placeholder 11.
        src = "\n".join(f"`c{i}`" for i in range(12))
        out = render_whatsapp(src)
        for i in range(12):
            assert f"`c{i}`" in out
        assert "\x00" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogates && uv run pytest tests/test_whatsapp_format.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.channels.platforms.whatsapp_format'`

- [ ] **Step 3: Write the implementation**

Create `surogates/channels/platforms/whatsapp_format.py`:

```python
"""Markdown → WhatsApp markup transcoder.

WhatsApp renders its own lightweight markup, not markdown and not HTML:
``*bold*``, ``_italic_``, ``~strike~`` and triple-backtick monospace.  The
platform prompt tells the model to emit no markdown, but models routinely
do, so every outbound body is transcoded before it is split and sent.

Code spans are protected by replacing them with ``\\x00FENCE{i}\\x00``
sentinels before any transformation and restoring them afterwards.  The
**trailing** ``\\x00`` is load-bearing: without it the sequential
``str.replace`` restore of index 1 would corrupt index 11.  ``\\x00`` never
appears in LLM output.
"""

from __future__ import annotations

import re

__all__ = ["render_whatsapp"]

# Fenced blocks first (greedy over newlines), then inline spans.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# ``***x***`` must run before ``**x**`` or the outer pair is consumed first.
_BOLD_ITALIC_RE = re.compile(r"\*\*\*(.+?)\*\*\*", re.DOTALL)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)]+)\)")

_SENTINEL = "\x00"


def _protect(text: str) -> tuple[str, list[str]]:
    """Replace code spans with sentinels; return (masked_text, spans)."""
    spans: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        spans.append(match.group(0))
        return f"{_SENTINEL}CODE{len(spans) - 1}{_SENTINEL}"

    text = _FENCE_RE.sub(_stash, text)
    text = _INLINE_CODE_RE.sub(_stash, text)
    return text, spans


def _restore(text: str, spans: list[str]) -> str:
    """Put the protected code spans back."""
    for index, span in enumerate(spans):
        text = text.replace(f"{_SENTINEL}CODE{index}{_SENTINEL}", span)
    return text


def render_whatsapp(text: str) -> str:
    """Convert *text* from markdown to WhatsApp markup.

    Nothing is escaped: WhatsApp has no escape syntax, so a literal
    asterisk in the source is not representable.  Lists, blockquotes and
    tables pass through untouched — WhatsApp renders none of them and the
    raw characters read acceptably.
    """
    if not text:
        return ""

    text, spans = _protect(text)

    text = _BOLD_ITALIC_RE.sub(r"*_\1_*", text)
    text = _BOLD_RE.sub(r"*\1*", text)
    text = _BOLD_UNDERSCORE_RE.sub(r"*\1*", text)
    text = _STRIKE_RE.sub(r"~\1~", text)
    text = _HEADER_RE.sub(r"*\1*", text)
    text = _LINK_RE.sub(r"\1 (\2)", text)
    # Italic last: by now every ``**`` pair is a single ``*`` bold marker,
    # and the lookarounds keep those from being re-matched.
    text = _ITALIC_RE.sub(r"_\1_", text)

    return _restore(text, spans)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /work/surogates && uv run pytest tests/test_whatsapp_format.py -v`
Expected: PASS — all tests green.

If `test_single_asterisk_italic_becomes_underscore` fails because bold conversion already consumed the markers, verify `_ITALIC_RE` runs last and its `(?<!\*)`/`(?!\*)` lookarounds are present.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/channels/platforms/whatsapp_format.py tests/test_whatsapp_format.py
git commit -m "feat(channels): markdown to WhatsApp markup transcoder"
```

---

## Task 2: Graph API client

**Files:**
- Create: `surogates/channels/platforms/whatsapp_api.py`
- Test: `tests/test_whatsapp_api.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `DEFAULT_API_VERSION: str = "v23.0"`, `GRAPH_API_BASE: str = "https://graph.facebook.com"`
  - `MEDIA_SIZE_LIMITS: dict[str, int]`, `MIME_EXTENSION_OVERRIDES: dict[str, str]`
  - `graph_url(phone_number_id: str, path: str, *, api_version: str = DEFAULT_API_VERSION) -> str`
  - `format_graph_error(status_code: int, body: dict) -> str`
  - `async send_message(client, *, token, phone_number_id, payload, api_version) -> tuple[str | None, str | None]` → `(wamid, error)`
  - `async upload_media(client, *, token, phone_number_id, filename, data, mime_type, api_version) -> tuple[str | None, str | None]` → `(media_id, error)`
  - `async download_media(client, *, token, media_id, max_bytes, api_version) -> tuple[bytes | None, str | None]` → `(data, mime_type)`
  - `ext_for_mime(mime: str) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_whatsapp_api.py`:

```python
"""Tests for the WhatsApp Cloud API Graph client.

Written BEFORE the implementation module exists (TDD).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from surogates.channels.platforms.whatsapp_api import (
    DEFAULT_API_VERSION,
    GRAPH_API_BASE,
    download_media,
    ext_for_mime,
    format_graph_error,
    graph_url,
    send_message,
    upload_media,
)

TOKEN = "EAAtest-token"
PNID = "7794189252778687"


# ---------------------------------------------------------------------------
# graph_url
# ---------------------------------------------------------------------------


class TestGraphUrl:
    def test_builds_phone_scoped_url(self):
        assert graph_url(PNID, "messages") == (
            f"{GRAPH_API_BASE}/{DEFAULT_API_VERSION}/{PNID}/messages"
        )

    def test_strips_leading_slash(self):
        assert graph_url(PNID, "/media").endswith(f"/{PNID}/media")

    def test_version_is_not_v20(self):
        # v20.0 is removed by Meta on 2026-09-24.
        assert DEFAULT_API_VERSION != "v20.0"

    def test_version_override(self):
        assert "/v25.0/" in graph_url(PNID, "messages", api_version="v25.0")


# ---------------------------------------------------------------------------
# format_graph_error
# ---------------------------------------------------------------------------


class TestFormatGraphError:
    def test_includes_code_and_status(self):
        body = {"error": {"message": "Re-engagement message", "code": 131047}}
        assert format_graph_error(400, body) == (
            "graph error 131047 (HTTP 400): Re-engagement message"
        )

    def test_codeless_error_has_no_graph_prefix(self):
        # Code-less errors must never match a permanent prefix, so they stay
        # retryable by construction.
        body = {"error": {"message": "boom"}}
        out = format_graph_error(500, body)
        assert out == "HTTP 500: boom"
        assert "graph error" not in out

    def test_missing_error_object(self):
        assert format_graph_error(502, {}) == "HTTP 502: unknown error"


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_returns_wamid_on_success(self):
        url = graph_url(PNID, "messages")
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=True) as router:
                router.post(url).mock(
                    return_value=httpx.Response(
                        200, json={"messages": [{"id": "wamid.OUT1"}]},
                    )
                )
                wamid, error = await send_message(
                    client,
                    token=TOKEN,
                    phone_number_id=PNID,
                    payload={"type": "text"},
                    api_version=DEFAULT_API_VERSION,
                )
        assert wamid == "wamid.OUT1"
        assert error is None

    @pytest.mark.asyncio
    async def test_sends_bearer_token(self):
        url = graph_url(PNID, "messages")
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=True) as router:
                route = router.post(url).mock(
                    return_value=httpx.Response(
                        200, json={"messages": [{"id": "wamid.X"}]},
                    )
                )
                await send_message(
                    client, token=TOKEN, phone_number_id=PNID,
                    payload={}, api_version=DEFAULT_API_VERSION,
                )
        assert route.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"

    @pytest.mark.asyncio
    async def test_returns_formatted_error_on_failure(self):
        url = graph_url(PNID, "messages")
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=True) as router:
                router.post(url).mock(
                    return_value=httpx.Response(
                        400, json={"error": {"message": "bad", "code": 100}},
                    )
                )
                wamid, error = await send_message(
                    client, token=TOKEN, phone_number_id=PNID,
                    payload={}, api_version=DEFAULT_API_VERSION,
                )
        assert wamid is None
        assert error == "graph error 100 (HTTP 400): bad"


# ---------------------------------------------------------------------------
# download_media — the two-hop fetch
# ---------------------------------------------------------------------------


class TestDownloadMedia:
    @pytest.mark.asyncio
    async def test_two_hop_fetch_returns_bytes_and_mime(self):
        meta_url = f"{GRAPH_API_BASE}/{DEFAULT_API_VERSION}/media_abc"
        blob_url = "https://lookaside.fbsbx.com/whatsapp/m/xyz"
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=True) as router:
                router.get(meta_url).mock(
                    return_value=httpx.Response(
                        200,
                        json={"url": blob_url, "mime_type": "image/jpeg", "file_size": 9},
                    )
                )
                blob = router.get(blob_url).mock(
                    return_value=httpx.Response(200, content=b"JPEGBYTES")
                )
                data, mime = await download_media(
                    client, token=TOKEN, media_id="media_abc",
                    max_bytes=1024, api_version=DEFAULT_API_VERSION,
                )
        assert data == b"JPEGBYTES"
        assert mime == "image/jpeg"
        # The signed lookaside URL still requires the bearer token.
        assert blob.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"

    @pytest.mark.asyncio
    async def test_oversize_rejected_before_body_fetch(self):
        meta_url = f"{GRAPH_API_BASE}/{DEFAULT_API_VERSION}/media_big"
        blob_url = "https://lookaside.fbsbx.com/whatsapp/m/big"
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=False) as router:
                router.get(meta_url).mock(
                    return_value=httpx.Response(
                        200,
                        json={"url": blob_url, "mime_type": "video/mp4",
                              "file_size": 99_000_000},
                    )
                )
                blob = router.get(blob_url).mock(
                    return_value=httpx.Response(200, content=b"never")
                )
                data, mime = await download_media(
                    client, token=TOKEN, media_id="media_big",
                    max_bytes=1024, api_version=DEFAULT_API_VERSION,
                )
        assert data is None
        assert len(blob.calls) == 0, "body was fetched despite exceeding the cap"

    @pytest.mark.asyncio
    async def test_expired_url_retries_metadata_once(self):
        meta_url = f"{GRAPH_API_BASE}/{DEFAULT_API_VERSION}/media_exp"
        stale = "https://lookaside.fbsbx.com/whatsapp/m/stale"
        fresh = "https://lookaside.fbsbx.com/whatsapp/m/fresh"
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=True) as router:
                router.get(meta_url).mock(
                    side_effect=[
                        httpx.Response(200, json={"url": stale, "mime_type": "image/png",
                                                  "file_size": 4}),
                        httpx.Response(200, json={"url": fresh, "mime_type": "image/png",
                                                  "file_size": 4}),
                    ]
                )
                router.get(stale).mock(return_value=httpx.Response(410))
                router.get(fresh).mock(return_value=httpx.Response(200, content=b"PNG!"))
                data, mime = await download_media(
                    client, token=TOKEN, media_id="media_exp",
                    max_bytes=1024, api_version=DEFAULT_API_VERSION,
                )
        assert data == b"PNG!"

    @pytest.mark.asyncio
    async def test_metadata_failure_returns_none_pair(self):
        meta_url = f"{GRAPH_API_BASE}/{DEFAULT_API_VERSION}/media_gone"
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=True) as router:
                router.get(meta_url).mock(return_value=httpx.Response(404, json={}))
                data, mime = await download_media(
                    client, token=TOKEN, media_id="media_gone",
                    max_bytes=1024, api_version=DEFAULT_API_VERSION,
                )
        assert data is None
        assert mime is None


# ---------------------------------------------------------------------------
# upload_media
# ---------------------------------------------------------------------------


class TestUploadMedia:
    @pytest.mark.asyncio
    async def test_returns_media_id(self):
        url = graph_url(PNID, "media")
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=True) as router:
                router.post(url).mock(
                    return_value=httpx.Response(200, json={"id": "media_new"})
                )
                media_id, error = await upload_media(
                    client, token=TOKEN, phone_number_id=PNID,
                    filename="a.png", data=b"x", mime_type="image/png",
                    api_version=DEFAULT_API_VERSION,
                )
        assert media_id == "media_new"
        assert error is None

    @pytest.mark.asyncio
    async def test_oversize_rejected_without_request(self):
        url = graph_url(PNID, "media")
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=False) as router:
                route = router.post(url).mock(
                    return_value=httpx.Response(200, json={"id": "nope"})
                )
                media_id, error = await upload_media(
                    client, token=TOKEN, phone_number_id=PNID,
                    filename="big.png", data=b"x" * (6 * 1024 * 1024),
                    mime_type="image/png", api_version=DEFAULT_API_VERSION,
                )
        assert media_id is None
        assert "cap" in (error or "")
        assert len(route.calls) == 0


# ---------------------------------------------------------------------------
# ext_for_mime
# ---------------------------------------------------------------------------


class TestExtForMime:
    @pytest.mark.parametrize(
        "mime,expected",
        [
            ("audio/ogg", ".ogg"),                 # not mimetypes' .oga
            ("audio/ogg; codecs=opus", ".ogg"),    # parameters stripped
            ("audio/x-opus+ogg", ".ogg"),
            ("audio/opus", ".ogg"),
            ("audio/mp4", ".m4a"),                 # iOS voice memos
            ("audio/x-m4a", ".m4a"),
            ("image/jpeg", ".jpg"),                # not legacy .jpe
        ],
    )
    def test_overrides(self, mime, expected):
        assert ext_for_mime(mime) == expected

    def test_unknown_mime_falls_back_to_bin(self):
        assert ext_for_mime("application/x-nonsense") == ".bin"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogates && uv run pytest tests/test_whatsapp_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.channels.platforms.whatsapp_api'`

- [ ] **Step 3: Write the implementation**

Create `surogates/channels/platforms/whatsapp_api.py`:

```python
"""WhatsApp Business Cloud API (Meta Graph) transport.

The only module in the WhatsApp platform that performs HTTP.  Every
function takes an ``httpx.AsyncClient`` and the per-tenant credentials
explicitly — nothing is held on the module or on an instance, because one
channels process serves every tenant.

All functions return ``(value, error)`` tuples rather than raising: the
platform layer's contract with the dispatcher is that a transport failure
degrades to a ``SendResult``/``None``, never an exception.
"""

from __future__ import annotations

import logging
import mimetypes
from typing import Any

import httpx

__all__ = [
    "DEFAULT_API_VERSION",
    "GRAPH_API_BASE",
    "MEDIA_SIZE_LIMITS",
    "MIME_EXTENSION_OVERRIDES",
    "download_media",
    "ext_for_mime",
    "format_graph_error",
    "graph_url",
    "send_message",
    "upload_media",
]

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com"

#: Meta removes v20.0 on 2026-09-24; never pin below v23.0.
DEFAULT_API_VERSION = "v23.0"

#: Per-kind upload caps, enforced client-side before the round trip.
MEDIA_SIZE_LIMITS: dict[str, int] = {
    "image": 5 * 1024 * 1024,
    "video": 16 * 1024 * 1024,
    "audio": 16 * 1024 * 1024,
    "document": 100 * 1024 * 1024,
    "sticker": 100 * 1024,
}

#: ``mimetypes`` guesses badly for the formats WhatsApp actually sends.
MIME_EXTENSION_OVERRIDES: dict[str, str] = {
    "audio/ogg": ".ogg",          # not mimetypes' .oga
    "audio/x-opus+ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/mp4": ".m4a",          # iOS voice memos
    "audio/x-m4a": ".m4a",
    "image/jpeg": ".jpg",         # not legacy .jpe
}

_UPLOAD_TIMEOUT = httpx.Timeout(120.0)


def graph_url(
    phone_number_id: str, path: str, *, api_version: str = DEFAULT_API_VERSION,
) -> str:
    """Return the phone-number-scoped Graph URL for *path*."""
    return f"{GRAPH_API_BASE}/{api_version}/{phone_number_id}/{path.lstrip('/')}"


def format_graph_error(status_code: int, body: dict) -> str:
    """Render a Graph error into the string the outbox classifier matches.

    With a code: ``graph error {code} (HTTP {status}): {message}``.  Without
    one: ``HTTP {status}: {message}`` — deliberately missing the ``graph
    error`` prefix, so a code-less error can never match a permanent-error
    prefix in ``delivery._PERMANENT_DELIVERY_ERRORS`` and stays retryable by
    construction.
    """
    err = (body or {}).get("error") or {}
    message = err.get("message") or "unknown error"
    code = err.get("code")
    if code is None:
        return f"HTTP {status_code}: {message}"
    return f"graph error {code} (HTTP {status_code}): {message}"


def ext_for_mime(mime: str) -> str:
    """Return a file extension for *mime*, preferring the override table."""
    base = (mime or "").split(";")[0].strip().lower()
    override = MIME_EXTENSION_OVERRIDES.get(base)
    if override:
        return override
    return mimetypes.guess_extension(base) or ".bin"


def _kind_for_mime(mime: str) -> str:
    """Map a MIME type to a WhatsApp media kind for cap lookup."""
    base = (mime or "").split("/")[0].strip().lower()
    if base in ("image", "video", "audio"):
        return base
    return "document"


async def send_message(
    client: httpx.AsyncClient,
    *,
    token: str,
    phone_number_id: str,
    payload: dict[str, Any],
    api_version: str = DEFAULT_API_VERSION,
) -> tuple[str | None, str | None]:
    """POST *payload* to ``/messages``.  Returns ``(wamid, error)``."""
    url = graph_url(phone_number_id, "messages", api_version=api_version)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        response = await client.post(url, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[whatsapp] /messages request failed (%s)", exc)
        return None, f"request failed: {exc}"

    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        data = {}

    if not response.is_success:
        return None, format_graph_error(response.status_code, data)

    messages = data.get("messages") or []
    wamid = messages[0].get("id") if messages else None
    return wamid, None


async def upload_media(
    client: httpx.AsyncClient,
    *,
    token: str,
    phone_number_id: str,
    filename: str,
    data: bytes,
    mime_type: str,
    api_version: str = DEFAULT_API_VERSION,
) -> tuple[str | None, str | None]:
    """Upload *data* to ``/media``.  Returns ``(media_id, error)``.

    The size cap is checked client-side before the round trip, with the cap
    value in the error string so an operator can see what was exceeded.
    """
    kind = _kind_for_mime(mime_type)
    cap = MEDIA_SIZE_LIMITS.get(kind, MEDIA_SIZE_LIMITS["document"])
    if len(data) > cap:
        return None, (
            f"File {filename} is {len(data)} bytes; "
            f"Cloud API {kind} cap is {cap} bytes"
        )

    url = graph_url(phone_number_id, "media", api_version=api_version)
    # No Content-Type header — httpx sets the multipart boundary itself.
    headers = {"Authorization": f"Bearer {token}"}
    files = {
        "file": (filename, data, mime_type),
        "messaging_product": (None, "whatsapp"),
        "type": (None, mime_type),
    }
    try:
        response = await client.post(
            url, files=files, headers=headers, timeout=_UPLOAD_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[whatsapp] /media upload failed for %s (%s)", filename, exc)
        return None, f"upload failed: {exc}"

    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = {}

    if not response.is_success:
        return None, format_graph_error(response.status_code, body)
    return body.get("id"), None


async def download_media(
    client: httpx.AsyncClient,
    *,
    token: str,
    media_id: str,
    max_bytes: int,
    api_version: str = DEFAULT_API_VERSION,
) -> tuple[bytes | None, str | None]:
    """Fetch inbound media by id.  Returns ``(data, mime_type)``.

    Two hops: ``GET /{media_id}`` yields a signed lookaside URL that expires
    in about five minutes, then a second GET fetches the bytes.  The signed
    URL **still requires** the bearer token — Meta documents this and it is
    the most common inbound-media mistake.

    ``max_bytes`` is checked against the metadata's ``file_size`` before the
    body is fetched.  On a 403/410 the metadata hop is retried once, since
    the URL may simply have expired.  Any failure returns ``(None, None)``
    so a media problem never kills the surrounding event.
    """
    headers = {"Authorization": f"Bearer {token}"}
    meta_url = f"{GRAPH_API_BASE}/{api_version}/{media_id}"

    for attempt in (1, 2):
        try:
            meta_response = await client.get(meta_url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[whatsapp] media metadata request failed (%s)", exc)
            return None, None
        if not meta_response.is_success:
            logger.warning(
                "[whatsapp] media metadata HTTP %s for %s",
                meta_response.status_code, media_id,
            )
            return None, None

        try:
            meta = meta_response.json()
        except Exception:  # noqa: BLE001
            return None, None

        blob_url = meta.get("url")
        mime_type = meta.get("mime_type") or ""
        if not blob_url:
            return None, None

        file_size = meta.get("file_size")
        if isinstance(file_size, int) and file_size > max_bytes:
            logger.warning(
                "[whatsapp] media %s is %d bytes, over the %d cap — skipping",
                media_id, file_size, max_bytes,
            )
            return None, None

        try:
            blob_response = await client.get(blob_url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[whatsapp] media body request failed (%s)", exc)
            return None, None

        if blob_response.status_code in (403, 410) and attempt == 1:
            # Signed URL expired between the two hops — re-resolve it once.
            continue
        if not blob_response.is_success:
            logger.warning(
                "[whatsapp] media body HTTP %s for %s",
                blob_response.status_code, media_id,
            )
            return None, None

        data = blob_response.content
        if len(data) > max_bytes:
            logger.warning(
                "[whatsapp] media %s body exceeded the %d cap — skipping",
                media_id, max_bytes,
            )
            return None, None
        return data, mime_type

    return None, None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /work/surogates && uv run pytest tests/test_whatsapp_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/channels/platforms/whatsapp_api.py tests/test_whatsapp_api.py
git commit -m "feat(channels): WhatsApp Cloud API Graph transport client"
```

---

## Task 3: Platform class — identifier, verify, parse

**Files:**
- Create: `surogates/channels/platforms/whatsapp.py`
- Test: `tests/test_whatsapp_platform.py`

**Interfaces:**
- Consumes: `whatsapp_api.DEFAULT_API_VERSION` (Task 2).
- Produces:
  - module-level `identifier_of(request, body) -> str`, `verify(request, raw_body, *, creds) -> bool | VerificationResult`, `parse(body, *, creds=None, identifier=None) -> InboundMessage | None`
  - `class WhatsAppPlatform` with `kind`, `topology`, `descriptor`, `handshake_get = True`, `route_path`, and the three delegates. `send` is added in Task 5.

Read §5.1–§5.5 of the spec before starting.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_whatsapp_platform.py`:

```python
"""Tests for the WhatsApp Business Cloud API channel platform.

Written BEFORE the implementation module exists (TDD).  Mirrors
tests/test_telegram_platform.py in structure.
"""

from __future__ import annotations

import hashlib
import hmac
import json as _json
from types import SimpleNamespace

import pytest

from surogates.channels.platforms.whatsapp import (
    WhatsAppPlatform,
    identifier_of,
    parse,
    verify,
)
from surogates.channels.registry import VerificationResult

PNID = "7794189252778687"
APP_SECRET = "0123456789abcdef0123456789abcdef"
VERIFY_TOKEN = "a7Fk2verify"
ACCESS_TOKEN = "EAAtoken"
WA_ID = "13557825698"


def _creds(**overrides) -> dict:
    """Credential dict as the dispatcher resolves it from the vault."""
    creds = {
        "access_token": ACCESS_TOKEN,
        "app_secret": APP_SECRET,
        "verify_token": VERIFY_TOKEN,
    }
    creds.update(overrides)
    return creds


def _sign(secret: str, body: bytes) -> str:
    """Recompute the X-Hub-Signature-256 header; never hardcode it."""
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256,
    ).hexdigest()


def _post_request(raw: bytes, *, secret: str = APP_SECRET, pnid: str = PNID):
    """A signed POST request double (only .headers/.path_params are read)."""
    return SimpleNamespace(
        method="POST",
        path_params={"phone_number_id": pnid},
        headers={"X-Hub-Signature-256": _sign(secret, raw)},
        query_params={},
    )


def _get_request(**query):
    """A GET handshake request double."""
    return SimpleNamespace(
        method="GET",
        path_params={"phone_number_id": PNID},
        headers={},
        query_params=query,
    )


def _text_message(**overrides) -> dict:
    """The canonical inbound text envelope, modelled on Meta's sample."""
    message = {
        "from": WA_ID,
        "id": "wamid.HBgLMTM1NTc4MjU2OTgVAGHAYWYET688aASGNTI1QzZFQjhEMDk2QQA=",
        "timestamp": "1758254144",
        "text": {"body": "Hi!"},
        "type": "text",
    }
    message.update(overrides)
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "215589313241560883",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "15551797781",
                        "phone_number_id": PNID,
                    },
                    "contacts": [
                        {"profile": {"name": "Jessica Laverdetman"}, "wa_id": WA_ID},
                    ],
                    "messages": [message],
                },
            }],
        }],
    }


# ---------------------------------------------------------------------------
# identifier_of
# ---------------------------------------------------------------------------


class TestIdentifierOf:
    def test_reads_phone_number_id_from_path(self):
        assert identifier_of(_post_request(b"{}"), None) == PNID

    def test_ignores_body(self):
        # The dispatcher calls this with body=None before parsing.
        assert identifier_of(_post_request(b"{}"), None) == PNID


# ---------------------------------------------------------------------------
# verify — GET handshake
# ---------------------------------------------------------------------------


class TestVerifyHandshake:
    def test_echoes_challenge_as_plain_string(self):
        request = _get_request(**{
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        })
        result = verify(request, b"", creds=_creds())
        assert isinstance(result, VerificationResult)
        assert result.accepted is True
        assert result.response_body == "1158201444"
        assert result.status_code == 200

    def test_rejects_wrong_token(self):
        request = _get_request(**{
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "123",
        })
        result = verify(request, b"", creds=_creds())
        assert result.accepted is False

    def test_rejects_wrong_mode(self):
        request = _get_request(**{
            "hub.mode": "unsubscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "123",
        })
        assert verify(request, b"", creds=_creds()).accepted is False

    def test_rejects_missing_challenge(self):
        request = _get_request(**{
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
        })
        assert verify(request, b"", creds=_creds()).accepted is False

    def test_rejects_when_verify_token_unconfigured(self):
        # An unset secret makes compare_digest("", "") true, so an attacker who
        # guesses the misconfiguration could subscribe their own webhook.
        request = _get_request(**{
            "hub.mode": "subscribe",
            "hub.verify_token": "",
            "hub.challenge": "123",
        })
        assert verify(request, b"", creds=_creds(verify_token="")).accepted is False

    def test_non_ascii_token_does_not_raise(self):
        # compare_digest on str raises TypeError on non-ASCII; compare bytes.
        request = _get_request(**{
            "hub.mode": "subscribe",
            "hub.verify_token": "tökén",
            "hub.challenge": "123",
        })
        assert verify(request, b"", creds=_creds()).accepted is False


# ---------------------------------------------------------------------------
# verify — POST signature
# ---------------------------------------------------------------------------


class TestVerifySignature:
    def test_accepts_valid_signature(self):
        raw = b'{"object":"whatsapp_business_account"}'
        assert verify(_post_request(raw), raw, creds=_creds()) is True

    def test_rejects_wrong_secret(self):
        raw = b'{"object":"whatsapp_business_account"}'
        request = _post_request(raw, secret="f" * 32)
        assert verify(request, raw, creds=_creds()) is False

    def test_rejects_missing_header(self):
        raw = b"{}"
        request = SimpleNamespace(
            method="POST", path_params={"phone_number_id": PNID},
            headers={}, query_params={},
        )
        assert verify(request, raw, creds=_creds()) is False

    def test_rejects_header_without_sha256_prefix(self):
        raw = b"{}"
        digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        request = SimpleNamespace(
            method="POST", path_params={"phone_number_id": PNID},
            headers={"X-Hub-Signature-256": digest}, query_params={},
        )
        assert verify(request, raw, creds=_creds()) is False

    def test_uppercase_hex_signature_accepted(self):
        raw = b"{}"
        digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        request = SimpleNamespace(
            method="POST", path_params={"phone_number_id": PNID},
            headers={"X-Hub-Signature-256": "sha256=" + digest.upper()},
            query_params={},
        )
        assert verify(request, raw, creds=_creds()) is True

    def test_rejects_when_app_secret_missing(self):
        raw = b"{}"
        assert verify(_post_request(raw), raw, creds=_creds(app_secret="")) is False

    def test_rejects_oversize_body_before_crypto(self):
        raw = b"x" * (3 * 1024 * 1024 + 1)
        assert verify(_post_request(raw), raw, creds=_creds()) is False


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


class TestParse:
    def test_text_message(self):
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        assert msg is not None
        assert msg.text == "Hi!"
        assert msg.identifier == WA_ID
        assert msg.platform_user_id == WA_ID
        assert msg.user_name == "Jessica Laverdetman"
        assert msg.is_dm is True
        assert msg.visibility == "dm"
        assert msg.thread_key is None
        assert msg.is_bot is False

    def test_ts_is_the_wamid_not_the_timestamp(self):
        # The WhatsApp timestamp is second-resolution and would collide across
        # senders in the shared dedup cache; the wamid is globally unique.
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        assert msg.ts.startswith("wamid.")

    def test_source_carries_tenant_and_wamid(self):
        # ack_received receives only (msg, creds, config) — no routing, no
        # identifier — so everything it needs must ride on msg.source.
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        assert msg.source["phone_number_id"] == PNID
        assert msg.source["wamid"] == msg.ts

    def test_tenant_mismatch_is_dropped(self):
        body = _text_message()
        body["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "999"
        assert parse(body, creds=_creds(), identifier=PNID) is None

    def test_wrong_object_dropped(self):
        body = _text_message()
        body["object"] = "page"
        assert parse(body, creds=_creds(), identifier=PNID) is None

    def test_non_messages_field_dropped(self):
        body = _text_message()
        body["entry"][0]["changes"][0]["field"] = "message_template_status_update"
        assert parse(body, creds=_creds(), identifier=PNID) is None

    @pytest.mark.parametrize(
        "msg_type", ["reaction", "system", "unsupported", "order", "location", "contacts"],
    )
    def test_non_message_types_return_none(self, msg_type):
        # Hermes maps these to TEXT with body="", so a thumbs-up starts a real
        # agent turn with an empty prompt.
        body = _text_message(type=msg_type)
        body["entry"][0]["changes"][0]["value"]["messages"][0].pop("text")
        assert parse(body, creds=_creds(), identifier=PNID) is None

    def test_statuses_only_payload_returns_none(self):
        body = _text_message()
        value = body["entry"][0]["changes"][0]["value"]
        value.pop("messages")
        value["statuses"] = [{
            "id": "wamid.OUT1", "status": "failed", "recipient_id": WA_ID,
            "errors": [{"code": 131047, "title": "Re-engagement message"}],
        }]
        assert parse(body, creds=_creds(), identifier=PNID) is None

    def test_statuses_logging_failure_does_not_raise(self, monkeypatch):
        # A parse exception becomes a 400 and a Meta retry loop.
        import surogates.channels.platforms.whatsapp as wa

        def _boom(*args, **kwargs):
            raise RuntimeError("log sink down")

        monkeypatch.setattr(wa.logger, "warning", _boom)
        body = _text_message()
        value = body["entry"][0]["changes"][0]["value"]
        value.pop("messages")
        value["statuses"] = [{"id": "w", "status": "failed"}]
        assert parse(body, creds=_creds(), identifier=PNID) is None

    def test_image_message_produces_file_ref(self):
        body = _text_message(
            type="image",
            image={"id": "media_image_abc", "mime_type": "image/jpeg",
                   "caption": "look at this"},
        )
        body["entry"][0]["changes"][0]["value"]["messages"][0].pop("text")
        msg = parse(body, creds=_creds(), identifier=PNID)
        assert msg is not None
        assert msg.text == "look at this"
        assert msg.kind == "image"
        assert len(msg.files) == 1
        assert msg.files[0].file_id == "media_image_abc"
        assert msg.files[0].mime_type == "image/jpeg"

    def test_document_uses_filename(self):
        body = _text_message(
            type="document",
            document={"id": "media_doc_abc", "mime_type": "text/plain",
                      "filename": "notes.txt"},
        )
        body["entry"][0]["changes"][0]["value"]["messages"][0].pop("text")
        msg = parse(body, creds=_creds(), identifier=PNID)
        assert msg.files[0].filename == "notes.txt"

    def test_missing_sender_is_refused(self):
        body = _text_message()
        body["entry"][0]["changes"][0]["value"]["messages"][0].pop("from")
        assert parse(body, creds=_creds(), identifier=PNID) is None

    def test_unicode_body_preserved(self):
        msg = parse(
            _text_message(text={"body": "héllo 👋 مرحبا"}),
            creds=_creds(), identifier=PNID,
        )
        assert msg.text == "héllo 👋 مرحبا"

    def test_multi_message_batch_returns_first(self):
        body = _text_message()
        value = body["entry"][0]["changes"][0]["value"]
        value["messages"].append({
            "from": WA_ID, "id": "wamid.SECOND", "timestamp": "1758254145",
            "text": {"body": "second"}, "type": "text",
        })
        msg = parse(body, creds=_creds(), identifier=PNID)
        assert msg.text == "Hi!"


# ---------------------------------------------------------------------------
# Platform object + descriptor
# ---------------------------------------------------------------------------


class TestWhatsAppPlatform:
    def test_kind_and_topology(self):
        p = WhatsAppPlatform()
        assert p.kind == "whatsapp"
        assert p.topology == "webhook"

    def test_declares_get_handshake(self):
        assert WhatsAppPlatform().handshake_get is True

    def test_route_path_template_when_no_identifier(self):
        assert WhatsAppPlatform().route_path() == "/whatsapp/{phone_number_id}"

    def test_route_path_concrete_with_identifier(self):
        assert WhatsAppPlatform().route_path(PNID) == f"/whatsapp/{PNID}"

    def test_no_supports_edit(self):
        # WhatsApp cannot edit a sent message.
        assert getattr(WhatsAppPlatform(), "supports_edit", False) is False

    def test_descriptor_vault_refs(self):
        refs = WhatsAppPlatform().descriptor.vault_refs(PNID)
        assert refs == {
            "access_token": "access_token",
            "app_secret": "app_secret",
            "verify_token": "verify_token",
        }

    def test_descriptor_registration_is_manual(self):
        # Meta has no setWebhook equivalent for the callback URL.
        assert WhatsAppPlatform().descriptor.webhook_registration == "manual"
        assert WhatsAppPlatform().descriptor.register_webhook is None

    def test_descriptor_config_keys_match_provisioner(self):
        # These names are the contract with the ops provisioner's config blob.
        assert set(WhatsAppPlatform().descriptor.config_keys) == {
            "require_mention", "allow_bots", "identity_policy",
            "waba_id", "api_version",
        }


class TestWhatsAppRegistration:
    def test_registered_in_registry(self):
        from surogates.channels.registry import registry
        assert registry.get("whatsapp") is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogates && uv run pytest tests/test_whatsapp_platform.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.channels.platforms.whatsapp'`

- [ ] **Step 3: Write the implementation**

Create `surogates/channels/platforms/whatsapp.py`:

```python
"""WhatsApp Business Cloud API webhook channel platform strategy.

Exposes three stateless module-level functions used by the dispatcher and
the registered :class:`WhatsAppPlatform` object that implements
:class:`~surogates.channels.registry.ChannelPlatform`.

Module-level functions
----------------------
identifier_of(request, body) -> str
    Reads the tenant's ``phone_number_id`` from the URL path parameter.
    The path is authoritative: the dispatcher resolves the tenant and its
    credentials before the body is parsed, and the body's copy of the id is
    only cross-checked afterwards in :func:`parse`.

verify(request, raw_body, *, creds) -> bool | VerificationResult
    Branches on the HTTP method.  ``GET`` is Meta's callback-URL handshake
    (``hub.mode``/``hub.verify_token``/``hub.challenge``) and returns a
    :class:`VerificationResult` echoing the challenge as plain text.
    ``POST`` validates the ``X-Hub-Signature-256`` HMAC-SHA256 over the raw
    body.

parse(body, *, creds, identifier) -> InboundMessage | None
    Converts a WhatsApp Business Account webhook envelope into an
    :class:`~surogates.channels.inbound.InboundMessage`.  Returns ``None``
    for everything that is not a user message — delivery statuses,
    reactions, system events — per the protocol's loop-safety contract.

WhatsApp Cloud API is DM-only for our purposes: every conversation is 1:1,
so ``thread_key`` is always ``None`` and ``visibility`` is ``"dm"``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from surogates.channels.inbound import InboundFileRef, InboundMessage
from surogates.channels.platforms.whatsapp_api import (
    DEFAULT_API_VERSION,
    ext_for_mime,
)
from surogates.channels.registry import ChannelDescriptor, VerificationResult

__all__ = [
    "WhatsAppPlatform",
    "identifier_of",
    "verify",
    "parse",
]

logger = logging.getLogger(__name__)

#: Meta's documented maximum webhook body size.  Checked before any crypto.
_MAX_BODY_BYTES = 3 * 1024 * 1024

#: Inbound message types that carry a user message.  Everything else —
#: reaction, system, unsupported, order, location, contacts — returns None
#: rather than starting an agent turn with an empty prompt.
_MEDIA_TYPES = ("image", "video", "audio", "document", "sticker")
_MESSAGE_TYPES = ("text",) + _MEDIA_TYPES


# ---------------------------------------------------------------------------
# identifier_of
# ---------------------------------------------------------------------------


def identifier_of(request: Any, body: Any) -> str:
    """Return the tenant's ``phone_number_id`` from the URL path parameter.

    Parameters
    ----------
    request:
        Starlette-like request exposing ``path_params["phone_number_id"]``.
    body:
        Parsed request body — intentionally ignored.  The dispatcher calls
        this before the body is read.
    """
    return request.path_params["phone_number_id"]


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _verify_handshake(request: Any, *, creds: dict) -> VerificationResult:
    """Handle Meta's GET callback-URL verification handshake.

    Every rejection renders 401: the dispatcher discards
    ``VerificationResult.status_code`` when ``accepted`` is False.  The
    distinguishing signal is therefore the log level, not the response —
    an unconfigured token logs at ERROR because it is a misconfiguration,
    a mismatch at WARNING because it may be an attack.
    """
    expected = (creds or {}).get("verify_token") or ""
    if not expected:
        logger.error(
            "[whatsapp] verify_token is not configured for this tenant — "
            "refusing the handshake",
        )
        return VerificationResult(accepted=False)

    query = request.query_params
    mode = query.get("hub.mode", "")
    token = query.get("hub.verify_token", "")
    challenge = query.get("hub.challenge", "")

    if mode != "subscribe":
        logger.info("[whatsapp] handshake rejected: hub.mode=%r", mode)
        return VerificationResult(accepted=False)

    # Compare bytes: compare_digest on str raises TypeError on non-ASCII,
    # and the token is attacker-controlled.
    if not hmac.compare_digest(
        token.encode("utf-8", "surrogatepass"), expected.encode("utf-8"),
    ):
        logger.warning("[whatsapp] handshake rejected: verify_token mismatch")
        return VerificationResult(accepted=False)

    if not challenge:
        logger.info("[whatsapp] handshake rejected: missing hub.challenge")
        return VerificationResult(accepted=False)

    return VerificationResult(accepted=True, response_body=challenge, status_code=200)


def _verify_signature(app_secret: str, raw_body: bytes, header: str) -> bool:
    """Constant-time X-Hub-Signature-256 check over the raw body bytes."""
    if not app_secret or not header:
        return False
    if not header.startswith("sha256="):
        return False
    expected_hex = header[len("sha256="):].strip()
    if not expected_hex:
        return False
    computed = hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed.lower(), expected_hex.lower())


def verify(
    request: Any,
    raw_body: bytes,
    *,
    creds: dict,
) -> bool | VerificationResult:
    """Validate a WhatsApp webhook request.

    Parameters
    ----------
    request:
        Starlette-like request exposing ``method``, ``headers`` and
        ``query_params``.
    raw_body:
        Raw request body bytes.  The HMAC must be computed over these, never
        over a re-serialisation.
    creds:
        Credential dict; ``app_secret`` for POST, ``verify_token`` for GET.
    """
    if getattr(request, "method", "POST").upper() == "GET":
        return _verify_handshake(request, creds=creds)

    if len(raw_body) > _MAX_BODY_BYTES:
        logger.warning(
            "[whatsapp] rejecting %d-byte body (cap %d)",
            len(raw_body), _MAX_BODY_BYTES,
        )
        return False

    app_secret = (creds or {}).get("app_secret") or ""
    if not app_secret:
        logger.error(
            "[whatsapp] app_secret is not configured for this tenant — "
            "refusing the webhook",
        )
        return False

    header = request.headers.get("X-Hub-Signature-256", "")
    return _verify_signature(app_secret, raw_body, header)


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def _log_statuses(value: dict, *, identifier: str) -> None:
    """Log delivery statuses and inbound errors.

    These carry asynchronous send failures the ``POST /messages`` 200 did
    not report, so they must be visible.  They are *not* emitted as channel
    observations: that queue is the follow-mode memory firehose and needs a
    redis handle and an ``agent_id`` that ``parse`` does not receive.

    Wrapped by the caller in try/except — a logging failure must never turn
    a status webhook into a 400 and a Meta retry loop.
    """
    for status in value.get("statuses") or []:
        if not isinstance(status, dict):
            continue
        state = status.get("status")
        if state == "failed":
            logger.warning(
                "[whatsapp] delivery failed pnid=%s wamid=%s errors=%s",
                identifier, status.get("id"), status.get("errors"),
            )
        else:
            logger.debug(
                "[whatsapp] delivery %s pnid=%s wamid=%s",
                state, identifier, status.get("id"),
            )
    for error in value.get("errors") or []:
        logger.warning("[whatsapp] inbound error pnid=%s error=%s", identifier, error)


def _file_ref(raw_message: dict, msg_type: str) -> InboundFileRef | None:
    """Build an :class:`InboundFileRef` from a media message.

    ``url`` carries the Cloud API media id, not an HTTP URL: the framework
    passes it straight back to ``download_file``, which does the two Graph
    hops.  This mirrors how Telegram carries its ``file_id``.
    """
    block = raw_message.get(msg_type)
    if not isinstance(block, dict):
        return None
    media_id = block.get("id")
    if not media_id:
        return None
    mime_type = block.get("mime_type") or "application/octet-stream"
    filename = block.get("filename") or f"{media_id}{ext_for_mime(mime_type)}"
    return InboundFileRef(
        url=media_id,
        filename=filename,
        mime_type=mime_type,
        size=None,
        file_id=media_id,
    )


def parse(
    body: Any,
    *,
    creds: dict | None = None,
    identifier: str | None = None,
) -> InboundMessage | None:
    """Map a verified WhatsApp webhook envelope to an :class:`InboundMessage`.

    Returns ``None`` for every payload that is not a user message.  A
    ``phone_number_id`` in the body that does not match the path
    ``identifier`` is dropped: one Meta App can subscribe several numbers,
    and routing another number's message to this tenant would be a
    tenant-isolation failure.
    """
    if not isinstance(body, dict):
        return None
    if body.get("object") != "whatsapp_business_account":
        return None

    for entry in body.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue

            metadata = value.get("metadata") or {}
            body_pnid = metadata.get("phone_number_id")
            if identifier is not None and body_pnid != identifier:
                logger.warning(
                    "[whatsapp] dropping payload: body phone_number_id=%s "
                    "does not match route identifier=%s",
                    body_pnid, identifier,
                )
                return None

            try:
                _log_statuses(value, identifier=identifier or "")
            except Exception:  # noqa: BLE001
                pass

            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name", "")
                for c in value.get("contacts") or []
                if isinstance(c, dict)
            }

            for raw_message in value.get("messages") or []:
                if not isinstance(raw_message, dict):
                    continue
                message = _build_message(raw_message, names=names, identifier=body_pnid)
                if message is not None:
                    return message
    return None


def _build_message(
    raw_message: dict, *, names: dict, identifier: str | None,
) -> InboundMessage | None:
    """Build one :class:`InboundMessage`, or ``None`` if it is not a message."""
    msg_type = str(raw_message.get("type") or "text").lower()
    if msg_type not in _MESSAGE_TYPES:
        logger.debug("[whatsapp] ignoring message type %r", msg_type)
        return None

    wa_id = raw_message.get("from")
    if not wa_id:
        # DM-only: without a resolvable 1:1 sender we cannot route a reply.
        # Log the raw type so we capture the real shape if a group payload
        # ever arrives.
        logger.warning(
            "[whatsapp] refusing message with no resolvable sender (type=%s, keys=%s)",
            msg_type, sorted(raw_message.keys()),
        )
        return None

    wamid = raw_message.get("id") or ""

    if msg_type == "text":
        text = ((raw_message.get("text") or {}).get("body")) or ""
        files: list[InboundFileRef] = []
    else:
        block = raw_message.get(msg_type) or {}
        text = block.get("caption") or ""
        ref = _file_ref(raw_message, msg_type)
        files = [ref] if ref is not None else []

    source: dict[str, Any] = {
        "phone_number_id": identifier,
        "wamid": wamid,
    }
    if msg_type == "audio":
        source["voice"] = bool((raw_message.get("audio") or {}).get("voice"))

    return InboundMessage(
        kind=msg_type,
        identifier=str(wa_id),
        thread_key=None,
        platform_user_id=str(wa_id),
        user_name=names.get(wa_id, "") or "",
        text=text,
        media_urls=[],
        media_types=[],
        is_dm=True,
        is_mention=False,
        ts=wamid,
        source=source,
        is_bot=False,
        visibility="dm",
        files=files,
    )


# ---------------------------------------------------------------------------
# Platform object
# ---------------------------------------------------------------------------


class WhatsAppPlatform:
    """Webhook-based WhatsApp Cloud API channel platform strategy.

    Implements :class:`~surogates.channels.registry.ChannelPlatform`.

    Each tenant brings their own Meta App and WhatsApp Business Account.
    The tenant is identified by its ``phone_number_id``, which is the
    ``{phone_number_id}`` path parameter in the webhook URL.  Credentials
    (``access_token``, ``app_secret``, ``verify_token``) are resolved by the
    dispatcher and passed to every method that needs them.
    """

    kind = "whatsapp"
    topology = "webhook"

    #: Meta verifies the callback URL with an unsigned GET on the same path.
    #: The dispatcher mounts a GET route only for platforms declaring this.
    handshake_get = True

    descriptor = ChannelDescriptor(
        vault_refs=lambda identifier: {
            "access_token": "access_token",
            "app_secret": "app_secret",
            "verify_token": "verify_token",
        },
        config_keys=(
            "require_mention",
            "allow_bots",
            "identity_policy",
            "waba_id",
            "api_version",
        ),
        # Meta has no setWebhook equivalent: the callback URL is configured
        # by hand in the App Dashboard.
        webhook_registration="manual",
    )

    # ------------------------------------------------------------------
    # Route path
    # ------------------------------------------------------------------

    def route_path(self, identifier: str | None = None) -> str:
        """Return the FastAPI path for this platform.

        Parameters
        ----------
        identifier:
            The tenant's ``phone_number_id``.  When ``None`` (template form
            used by ``build_app``), returns the parametrised path template.
        """
        if identifier is None:
            return "/whatsapp/{phone_number_id}"
        return f"/whatsapp/{identifier}"

    # ------------------------------------------------------------------
    # identifier_of / verify / parse — delegates to module functions
    # ------------------------------------------------------------------

    def identifier_of(self, request: Any, body: Any) -> str:
        return identifier_of(request, body)

    def verify(
        self, request: Any, raw_body: bytes, *, creds: dict,
    ) -> bool | VerificationResult:
        return verify(request, raw_body, creds=creds)

    def parse(
        self,
        body: Any,
        *,
        creds: dict | None = None,
        identifier: str | None = None,
    ) -> InboundMessage | None:
        return parse(body, creds=creds, identifier=identifier)


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------


def _register() -> None:
    """Register the singleton WhatsAppPlatform in the module-level registry.

    Called once at import time.  Guarded against double-registration so that
    test suites that reimport the module (e.g. via importlib.reload) do not
    raise a ValueError from the registry.
    """
    from surogates.channels.registry import registry

    if registry.get("whatsapp") is None:
        registry.register(WhatsAppPlatform())


_register()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /work/surogates && uv run pytest tests/test_whatsapp_platform.py -v`
Expected: PASS. `TestWhatsAppRegistration` passes because importing the module runs `_register()`.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/channels/platforms/whatsapp.py tests/test_whatsapp_platform.py
git commit -m "feat(channels): WhatsApp platform inbound half (verify, parse)"
```

---

## Task 4: Dispatcher GET handshake route

**Files:**
- Modify: `surogates/channels/dispatcher.py:146-163` (`_mount_platform`)
- Test: `tests/test_channel_dispatcher.py` (extend)

**Interfaces:**
- Consumes: `platform.handshake_get` (Task 3).
- Produces: a GET route on `platform.route_path()` for any platform declaring `handshake_get = True`.

Read §4 of the spec. The whole point is that `_resolve_and_verify` already does everything: path identifier → tenant → vault creds → verify, and it already renders a `str` response body as `PlainTextResponse`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_channel_dispatcher.py` (match the existing fakes in that file — `_FakeCache`, `_FakeVault`, `_FakePipeline`):

```python
# ---------------------------------------------------------------------------
# GET handshake route (platforms declaring handshake_get)
# ---------------------------------------------------------------------------


class _HandshakePlatform:
    """Minimal platform that echoes a GET challenge, like WhatsApp."""

    kind = "handshaker"
    topology = "webhook"
    handshake_get = True
    descriptor = ChannelDescriptor(
        vault_refs=lambda identifier: {"verify_token": "verify_token"},
        config_keys=(),
        webhook_registration="manual",
    )

    def route_path(self, identifier=None):
        if identifier is None:
            return "/handshaker/{tenant}"
        return f"/handshaker/{identifier}"

    def identifier_of(self, request, body):
        return request.path_params["tenant"]

    def verify(self, request, raw_body, *, creds):
        if request.method == "GET":
            challenge = request.query_params.get("hub.challenge", "")
            if request.query_params.get("hub.verify_token") != creds.get("verify_token"):
                return VerificationResult(accepted=False)
            return VerificationResult(accepted=True, response_body=challenge)
        return True

    def parse(self, body, *, creds=None, identifier=None):
        return None

    async def send(self, item, *, creds):
        raise AssertionError("not used")


async def test_get_handshake_echoes_challenge():
    platform = _HandshakePlatform()
    dispatcher = _dispatcher_with(platform)
    app = dispatcher.build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test",
    ) as client:
        response = await client.get(
            "/handshaker/t1",
            params={"hub.mode": "subscribe", "hub.verify_token": "vault-value",
                    "hub.challenge": "1158201444"},
        )
    assert response.status_code == 200
    assert response.text == "1158201444"


async def test_get_handshake_rejects_bad_token_with_401():
    platform = _HandshakePlatform()
    dispatcher = _dispatcher_with(platform)
    app = dispatcher.build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test",
    ) as client:
        response = await client.get(
            "/handshaker/t1",
            params={"hub.mode": "subscribe", "hub.verify_token": "wrong",
                    "hub.challenge": "x"},
        )
    assert response.status_code == 401


async def test_get_handshake_unknown_tenant_fast_acks_200_empty():
    # Preserves the no-liveness-oracle property; Meta reads the empty body as
    # a failed challenge, which is the correct outcome.
    platform = _HandshakePlatform()
    dispatcher = _dispatcher_with(platform)
    app = dispatcher.build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test",
    ) as client:
        response = await client.get(
            "/handshaker/unknown",
            params={"hub.mode": "subscribe", "hub.verify_token": "vault-value",
                    "hub.challenge": "x"},
        )
    assert response.status_code == 200
    assert response.text == ""


async def test_platform_without_handshake_get_has_no_get_route():
    # Slack/Telegram must not gain a GET route.
    platform = _HandshakePlatform()
    del type(platform).handshake_get
    try:
        dispatcher = _dispatcher_with(platform)
        app = dispatcher.build_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test",
        ) as client:
            response = await client.get("/handshaker/t1")
        assert response.status_code == 405
    finally:
        type(platform).handshake_get = True
```

You will need `_dispatcher_with(platform)` — a helper that builds a `ChannelWebhookDispatcher` with `_FakeCache` seeded for `handshaker:t1` and a `_FakeVault` returning `"vault-value"`. If the file already has an equivalent helper under another name, reuse it rather than adding a second one. Import `ChannelDescriptor` and `VerificationResult` from `surogates.channels.registry`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogates && uv run pytest tests/test_channel_dispatcher.py -k handshake -v`
Expected: FAIL — the GET requests return 405 Method Not Allowed because only POST is mounted.

- [ ] **Step 3: Implement the route mounting**

In `surogates/channels/dispatcher.py`, extend `_mount_platform` (after the `interactive_paths` block) and add the handler factory below `_make_handler`:

```python
        interactive = getattr(platform, "interactive_paths", ())
        if interactive:
            interactive_handler = self._make_interactive_handler(platform)
            for path in interactive:
                app.add_api_route(path, interactive_handler, methods=["POST"])

        # Platforms whose provider verifies the callback URL with an unsigned
        # GET on the same path (WhatsApp Cloud API's hub.challenge handshake).
        # Slack's challenge arrives as a signed POST and reuses `verify`;
        # WhatsApp's cannot, so it needs its own method on the same route.
        if getattr(platform, "handshake_get", False):
            app.add_api_route(
                platform.route_path(),
                self._make_handshake_handler(platform),
                methods=["GET"],
            )
```

```python
    def _make_handshake_handler(self, platform: ChannelPlatform):
        """Return a GET handler for a provider's callback-URL handshake.

        Reuses the shared secure front-half wholesale: path identifier →
        resolve_tenant → vault creds → verify.  An accepted
        :class:`VerificationResult` carrying a ``str`` body is already
        rendered as a :class:`PlainTextResponse` by
        :meth:`_resolve_and_verify`, which is exactly the un-quoted challenge
        echo the provider expects.  An unknown identifier fast-acks 200 with
        an empty body, preserving the no-liveness-oracle property — the
        provider reads the empty body as a failed challenge.
        """
        self_ = self

        async def _handler(request: Request) -> Response:
            *_, err = await self_._resolve_and_verify(platform, request, b"")
            if err is not None:
                return err
            # Only reachable if `verify` returned a bare truthy value instead
            # of a VerificationResult — an implementation bug in the platform.
            logger.warning(
                "[dispatcher] %s handshake returned no response — "
                "verify must return a VerificationResult on GET",
                platform.kind,
            )
            return Response(status_code=400)

        return _handler
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /work/surogates && uv run pytest tests/test_channel_dispatcher.py -v`
Expected: PASS — including every pre-existing dispatcher test.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/channels/dispatcher.py tests/test_channel_dispatcher.py
git commit -m "feat(channels): mount a GET handshake route for platforms that need one"
```

---

## Task 5: Outbound send + text-mode input prompt

**Files:**
- Modify: `surogates/channels/platforms/whatsapp.py` (add `send`, `_send_input_prompt`, client management)
- Test: `tests/test_whatsapp_platform.py` (extend)

**Interfaces:**
- Consumes: `whatsapp_format.render_whatsapp` (Task 1); `whatsapp_api.send_message`, `graph_url`, `DEFAULT_API_VERSION` (Task 2); `split_text` from `surogates.channels.text_split`.
- Produces: `async WhatsAppPlatform.send(item, *, creds) -> SendResult`.

`item` is an outbox row exposing `.destination` (dict) and `.payload` (dict). The destination is built by `store.py` in Task 8; this task assumes `destination = {"wa_id": ..., "phone_number_id": ..., "channel_identifier": ...}`.

Read §6.1 and §3.3 of the spec.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_whatsapp_platform.py`:

```python
# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

import httpx
import respx

from surogates.channels.platforms.whatsapp_api import DEFAULT_API_VERSION, graph_url

MESSAGES_URL = graph_url(PNID, "messages")


def _item(content: str, **payload_extra):
    """An outbox row double: only .destination and .payload are read."""
    payload = {"content": content}
    payload.update(payload_extra)
    return SimpleNamespace(
        destination={
            "wa_id": WA_ID,
            "phone_number_id": PNID,
            "channel_identifier": PNID,
        },
        payload=payload,
    )


class TestWhatsAppSend:
    @pytest.mark.asyncio
    async def test_sends_text_and_returns_wamid(self):
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.OUT1"}]},
                )
            )
            result = await p.send(_item("hello"), creds=_creds())
        assert result.success is True
        assert result.message_id == "wamid.OUT1"

    @pytest.mark.asyncio
    async def test_payload_shape(self):
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.X"}]},
                )
            )
            await p.send(_item("hi"), creds=_creds())
        body = _json.loads(route.calls[0].request.content)
        assert body["messaging_product"] == "whatsapp"
        assert body["recipient_type"] == "individual"
        assert body["to"] == WA_ID
        assert body["type"] == "text"
        assert body["text"]["body"] == "hi"

    @pytest.mark.asyncio
    async def test_markdown_is_transcoded(self):
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.X"}]},
                )
            )
            await p.send(_item("**bold**"), creds=_creds())
        body = _json.loads(route.calls[0].request.content)
        assert body["text"]["body"] == "*bold*"

    @pytest.mark.asyncio
    async def test_long_text_is_split(self):
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.X"}]},
                )
            )
            result = await p.send(_item("a " * 4000), creds=_creds())
            assert len(router.calls) >= 2
        assert result.success is True

    @pytest.mark.asyncio
    async def test_empty_content_sends_nothing_and_succeeds(self):
        # success=True/message_id=None is the correct terminal state:
        # _deliver_item has two branches and never reads SendResult.retryable,
        # so success=False would requeue an unsendable item for 30 minutes.
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=False) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(200, json={})
            )
            result = await p.send(_item("   "), creds=_creds())
        assert result.success is True
        assert result.message_id is None
        assert len(route.calls) == 0

    @pytest.mark.asyncio
    async def test_failure_returns_formatted_error(self):
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    400, json={"error": {"message": "Re-engagement message",
                                         "code": 131047}},
                )
            )
            result = await p.send(_item("hi"), creds=_creds())
        assert result.success is False
        assert result.error == (
            "graph error 131047 (HTTP 400): Re-engagement message"
        )

    @pytest.mark.asyncio
    async def test_partial_send_reports_delivered_prefix(self):
        # A mid-sequence failure must report success with the last delivered
        # id, so a retry does not duplicate already-delivered chunks.
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            router.post(MESSAGES_URL).mock(
                side_effect=[
                    httpx.Response(200, json={"messages": [{"id": "wamid.C1"}]}),
                    httpx.Response(500, json={"error": {"message": "boom"}}),
                ]
            )
            result = await p.send(_item("a " * 4000), creds=_creds())
        assert result.success is True
        assert result.message_id == "wamid.C1"

    @pytest.mark.asyncio
    async def test_uses_api_version_from_destination_config(self):
        p = WhatsAppPlatform()
        item = _item("hi")
        item.destination["api_version"] = "v25.0"
        url = graph_url(PNID, "messages", api_version="v25.0")
        with respx.mock(assert_all_called=True) as router:
            router.post(url).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.X"}]},
                )
            )
            result = await p.send(item, creds=_creds())
        assert result.success is True


# ---------------------------------------------------------------------------
# ask_user_question — text-mode prompt
# ---------------------------------------------------------------------------


class TestWhatsAppInputPrompt:
    @pytest.mark.asyncio
    async def test_renders_numbered_choices(self):
        p = WhatsAppPlatform()
        item = _item(
            "",
            input_prompt=True,
            tool_call_id="tc1",
            context="Need a decision.",
            questions=[{
                "question": "Which environment?",
                "options": [{"label": "staging"}, {"label": "production"}],
            }],
        )
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.Q1"}]},
                )
            )
            result = await p.send(item, creds=_creds())
        sent = _json.loads(route.calls[0].request.content)["text"]["body"]
        assert "Which environment?" in sent
        assert "1. staging" in sent
        assert "2. production" in sent
        assert result.success is True

    @pytest.mark.asyncio
    async def test_prompt_without_options_still_sends_question(self):
        p = WhatsAppPlatform()
        item = _item(
            "", input_prompt=True, tool_call_id="tc2", context="",
            questions=[{"question": "What is the deploy tag?", "options": []}],
        )
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.Q2"}]},
                )
            )
            await p.send(item, creds=_creds())
        sent = _json.loads(route.calls[0].request.content)["text"]["body"]
        assert "What is the deploy tag?" in sent
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogates && uv run pytest tests/test_whatsapp_platform.py -k "Send or InputPrompt" -v`
Expected: FAIL — `AttributeError: 'WhatsAppPlatform' object has no attribute 'send'`

- [ ] **Step 3: Implement `send`**

Add these imports to `whatsapp.py`:

```python
import httpx

from surogates.channels.base import SendResult
from surogates.channels.platforms.whatsapp_api import send_message
from surogates.channels.platforms.whatsapp_format import render_whatsapp
from surogates.channels.text_split import split_text
```

Add the constant near `_MAX_BODY_BYTES`:

```python
#: WhatsApp's ``text.body`` cap.  Unlike Telegram (which feeds split_text
#: 3500 to leave headroom for HTML-render inflation), WhatsApp markup does
#: not inflate and the transcoder runs *before* splitting, so the full cap
#: is correct here.
_MAX_MESSAGE_CHARS = 4096
```

Add to `WhatsAppPlatform`:

```python
    def __init__(self) -> None:
        # Shared HTTP client reused for every Graph call.  The access token
        # is per-request (it is a per-tenant credential), not per-client, so
        # one client per platform instance suffices.  WhatsAppPlatform
        # instances are process-lifetime singletons; no explicit close needed.
        self._http = httpx.AsyncClient(timeout=30.0)

    # ------------------------------------------------------------------
    # send
    # ------------------------------------------------------------------

    async def send(self, item: Any, *, creds: dict) -> SendResult:
        """Deliver one outbox row to WhatsApp.

        Long bodies are transcoded, split at 4096 characters, and posted as
        separate messages.  A mid-sequence failure reports ``success=True``
        with the last delivered id so a retry does not duplicate the chunks
        that already landed.
        """
        token: str = (creds or {}).get("access_token") or ""
        destination = item.destination or {}
        wa_id: str = destination.get("wa_id") or ""
        phone_number_id: str = destination.get("phone_number_id") or ""
        api_version: str = destination.get("api_version") or DEFAULT_API_VERSION

        if not token or not wa_id or not phone_number_id:
            return SendResult(success=False, error="missing whatsapp credentials or destination")

        if item.payload.get("input_prompt"):
            text = _render_input_prompt(item.payload)
        else:
            text = render_whatsapp(item.payload.get("content", "") or "")

        if not text.strip():
            # Nothing to send.  success=True with no id is the correct
            # terminal state — see the docstring note in the spec §6.1.
            return SendResult(success=True, message_id=None)

        last_id: str | None = None
        for chunk in split_text(text, _MAX_MESSAGE_CHARS) or [text]:
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": wa_id,
                "type": "text",
                "text": {"body": chunk, "preview_url": True},
            }
            wamid, error = await send_message(
                self._http,
                token=token,
                phone_number_id=phone_number_id,
                payload=payload,
                api_version=api_version,
            )
            if wamid is None:
                if last_id is not None:
                    return SendResult(success=True, message_id=last_id)
                return SendResult(success=False, error=error or "send failed")
            last_id = wamid

        if last_id is None:
            return SendResult(success=False, error="send failed")
        return SendResult(success=True, message_id=last_id)
```

Add the module-level renderer above the class:

```python
def _render_input_prompt(payload: dict) -> str:
    """Render an ``ask_user_question`` prompt as numbered plain text.

    WhatsApp has no native buttons in this integration, so the choices are
    a numbered list in the message body and a plain typed reply resolves the
    pending record through the shared ``resolve_text_answer`` path.
    """
    lines: list[str] = []
    context = (payload.get("context") or "").strip()
    if context:
        lines.append(context)

    for question in payload.get("questions") or []:
        if not isinstance(question, dict):
            continue
        prompt = (question.get("question") or "").strip()
        if prompt:
            lines.append(f"*{prompt}*")
        for index, option in enumerate(question.get("options") or [], start=1):
            label = option.get("label") if isinstance(option, dict) else str(option)
            if label:
                lines.append(f"{index}. {label}")

    lines.append("_Reply with your answer._")
    return "\n".join(line for line in lines if line)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /work/surogates && uv run pytest tests/test_whatsapp_platform.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/channels/platforms/whatsapp.py tests/test_whatsapp_platform.py
git commit -m "feat(channels): WhatsApp outbound send with text-mode input prompts"
```

---

## Task 6: Optional members — ack_received, download_file, send_private, post_input_nudge

**Files:**
- Modify: `surogates/channels/platforms/whatsapp.py`
- Test: `tests/test_whatsapp_platform.py` (extend)

**Interfaces:**
- Consumes: `whatsapp_api.send_message`, `whatsapp_api.download_media` (Task 2).
- Produces:
  - `async ack_received(msg, *, creds, config) -> None`
  - `async download_file(*, creds, url, max_bytes) -> bytes | None`
  - `async send_private(creds, *, sender_id, chat_id, is_dm, text) -> bool`
  - `async post_input_nudge(*, creds, channel, thread_ts, text) -> None`

Read §6.2 and §6.5. `post_input_nudge` is **not** interactive-only — it delivers the `/stop` acknowledgement and the allowance-block notice with its buy link. `runner.py:310-312` getattr-guards it, so omitting it fails silently.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_whatsapp_platform.py`:

```python
# ---------------------------------------------------------------------------
# ack_received — read receipt + typing in one call
# ---------------------------------------------------------------------------


class TestAckReceived:
    @pytest.mark.asyncio
    async def test_marks_read_and_sets_typing(self):
        p = WhatsAppPlatform()
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(200, json={"success": True})
            )
            await p.ack_received(msg, creds=_creds(), config={})
        body = _json.loads(route.calls[0].request.content)
        assert body == {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": msg.source["wamid"],
            "typing_indicator": {"type": "text"},
        }

    @pytest.mark.asyncio
    async def test_no_call_without_wamid(self):
        p = WhatsAppPlatform()
        msg = SimpleNamespace(identifier=WA_ID, source={"phone_number_id": PNID})
        with respx.mock(assert_all_called=False) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(200, json={})
            )
            await p.ack_received(msg, creds=_creds(), config={})
        assert len(route.calls) == 0

    @pytest.mark.asyncio
    async def test_never_raises_on_transport_error(self):
        p = WhatsAppPlatform()
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        with respx.mock(assert_all_called=True) as router:
            router.post(MESSAGES_URL).mock(side_effect=httpx.ConnectError("down"))
            await p.ack_received(msg, creds=_creds(), config={})


# ---------------------------------------------------------------------------
# download_file
# ---------------------------------------------------------------------------


class TestDownloadFile:
    @pytest.mark.asyncio
    async def test_two_hop_fetch(self):
        from surogates.channels.platforms.whatsapp_api import GRAPH_API_BASE

        p = WhatsAppPlatform()
        meta_url = f"{GRAPH_API_BASE}/{DEFAULT_API_VERSION}/media_abc"
        blob_url = "https://lookaside.fbsbx.com/whatsapp/m/xyz"
        with respx.mock(assert_all_called=True) as router:
            router.get(meta_url).mock(
                return_value=httpx.Response(
                    200, json={"url": blob_url, "mime_type": "image/jpeg",
                               "file_size": 5},
                )
            )
            router.get(blob_url).mock(
                return_value=httpx.Response(200, content=b"BYTES")
            )
            data = await p.download_file(
                creds=_creds(), url="media_abc", max_bytes=1024,
            )
        assert data == b"BYTES"

    @pytest.mark.asyncio
    async def test_returns_none_without_token(self):
        p = WhatsAppPlatform()
        assert await p.download_file(
            creds={"access_token": ""}, url="media_abc", max_bytes=1024,
        ) is None


# ---------------------------------------------------------------------------
# send_private / post_input_nudge
# ---------------------------------------------------------------------------


class TestSendPrivateAndNudge:
    @pytest.mark.asyncio
    async def test_send_private_delivers_and_returns_true(self):
        # Every WhatsApp conversation is already a DM, so this is a plain send.
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.P1"}]},
                )
            )
            ok = await p.send_private(
                _creds(), sender_id=WA_ID, chat_id=WA_ID, is_dm=True,
                text="Your link code is ABCD-1234",
            )
        assert ok is True
        body = _json.loads(route.calls[0].request.content)
        assert "ABCD-1234" in body["text"]["body"]

    @pytest.mark.asyncio
    async def test_send_private_false_without_phone_number_id(self):
        p = WhatsAppPlatform()
        ok = await p.send_private(
            {"access_token": ACCESS_TOKEN}, sender_id=WA_ID, chat_id=WA_ID,
            is_dm=True, text="hi",
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_post_input_nudge_sends_text(self):
        # Delivers the /stop ack and the allowance-block notice with its buy
        # link; runner.py getattr-guards it, so omitting it fails silently.
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.N1"}]},
                )
            )
            await p.post_input_nudge(
                creds=_creds(), channel=WA_ID, thread_ts=None,
                text="⏹ Stopping the current run…",
            )
        body = _json.loads(route.calls[0].request.content)
        assert body["to"] == WA_ID
        assert "Stopping" in body["text"]["body"]
```

`send_private` and `post_input_nudge` need the tenant's `phone_number_id`, which is not in their signature. Resolve it from `creds` — Task 11's provisioner stores it as a credential alongside the secrets so it is available wherever creds are. So:

1. Add `"phone_number_id": "phone_number_id"` to the descriptor's `vault_refs` in `WhatsAppPlatform`.
2. Update `TestWhatsAppPlatform.test_descriptor_vault_refs` (written in Task 3) to expect four keys:

```python
    def test_descriptor_vault_refs(self):
        refs = WhatsAppPlatform().descriptor.vault_refs(PNID)
        assert refs == {
            "access_token": "access_token",
            "app_secret": "app_secret",
            "verify_token": "verify_token",
            "phone_number_id": "phone_number_id",
        }
```

3. Add `"phone_number_id": PNID` to the `_creds()` helper's base dict.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogates && uv run pytest tests/test_whatsapp_platform.py -k "Ack or Download or Private" -v`
Expected: FAIL — `AttributeError` for each missing method.

- [ ] **Step 3: Implement the optional members**

Add to `WhatsAppPlatform` (import `download_media` from `whatsapp_api`):

```python
    # ------------------------------------------------------------------
    # ack_received — read receipt + typing indicator in one call
    # ------------------------------------------------------------------

    async def ack_received(self, msg: Any, *, creds: dict, config: dict) -> None:
        """Mark the inbound message read and show the typing pip.

        One request sets blue double-checkmarks *and* the typing indicator.
        This is the only progress signal WhatsApp offers: it cannot edit a
        sent message, so the dispatcher swallows every intermediate
        narration event and the agent would otherwise be silent for the
        whole tool-calling phase.

        Called only when the pipeline accepted the message, so filtered
        senders never receive a receipt.  Best-effort; never raises.
        """
        token: str = (creds or {}).get("access_token") or ""
        source = getattr(msg, "source", None) or {}
        wamid = source.get("wamid")
        phone_number_id = source.get("phone_number_id") or (creds or {}).get(
            "phone_number_id"
        )
        if not token or not wamid or not phone_number_id:
            return

        api_version = (config or {}).get("api_version") or DEFAULT_API_VERSION
        _, error = await send_message(
            self._http,
            token=token,
            phone_number_id=phone_number_id,
            payload={
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": wamid,
                "typing_indicator": {"type": "text"},
            },
            api_version=api_version,
        )
        if error and "131009" in error:
            # wamid older than 30 days — common after a long-quiet
            # conversation, not worth a warning.
            logger.info("[whatsapp] read receipt skipped: %s", error)
        elif error:
            logger.warning("[whatsapp] read receipt failed (%s)", error)

    # ------------------------------------------------------------------
    # download_file — two-hop inbound media fetch
    # ------------------------------------------------------------------

    async def download_file(
        self, *, creds: dict, url: str, max_bytes: int,
    ) -> bytes | None:
        """Fetch inbound media.  ``url`` carries the Cloud API media id.

        Mirrors Telegram, which carries its ``file_id`` in the same slot.
        ``max_bytes`` is a hard cap, not a streaming contract — the caller
        buffers the whole body.  Best-effort; never raises.
        """
        token: str = (creds or {}).get("access_token") or ""
        if not token or not url:
            return None
        data, _mime = await download_media(
            self._http, token=token, media_id=url, max_bytes=max_bytes,
        )
        return data

    # ------------------------------------------------------------------
    # send_private / post_input_nudge — plain sends
    # ------------------------------------------------------------------

    async def _send_plain(
        self, *, creds: dict, wa_id: str, text: str,
    ) -> bool:
        """Post one plain text message.  Returns True on success."""
        token: str = (creds or {}).get("access_token") or ""
        phone_number_id: str = (creds or {}).get("phone_number_id") or ""
        if not token or not phone_number_id or not wa_id or not text:
            return False
        wamid, error = await send_message(
            self._http,
            token=token,
            phone_number_id=phone_number_id,
            payload={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": wa_id,
                "type": "text",
                "text": {"body": text, "preview_url": True},
            },
        )
        if error:
            logger.warning("[whatsapp] plain send failed (%s)", error)
        return wamid is not None

    async def send_private(
        self,
        creds: dict,
        *,
        sender_id: str,
        chat_id: str,
        is_dm: bool,
        text: str,
    ) -> bool:
        """Privately deliver *text* to *sender_id*.

        Every WhatsApp conversation is already a DM, so this is a plain
        send.  Used by the ``linked`` identity policy to deliver a pairing
        code without leaking it into a shared surface.
        """
        return await self._send_plain(creds=creds, wa_id=sender_id, text=text)

    async def post_input_nudge(
        self, *, creds: dict, channel: str, thread_ts: Any, text: str,
    ) -> None:
        """Deliver a status line to the conversation.

        Not interactive-only: this carries the ``/stop`` acknowledgement and
        the allowance/subscription block notice with its buy link.  WhatsApp
        has no threads, so ``thread_ts`` is ignored.  Best-effort.
        """
        await self._send_plain(creds=creds, wa_id=channel, text=text)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /work/surogates && uv run pytest tests/test_whatsapp_platform.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/channels/platforms/whatsapp.py tests/test_whatsapp_platform.py
git commit -m "feat(channels): WhatsApp read receipts, media download, private sends"
```

---

## Task 7: Outbound media — send_files + the MEDIA gate

**Files:**
- Modify: `surogates/channels/platforms/whatsapp.py` (add `send_files`)
- Modify: `surogates/channels/dispatcher.py:760`
- Test: `tests/test_whatsapp_platform.py`, `tests/test_channel_dispatcher.py`

**Interfaces:**
- Consumes: `whatsapp_api.upload_media`, `whatsapp_api.send_message` (Task 2); `channel_media.OutboundFile` (`filename: str`, `mime_type: str`, `data: bytes`).
- Produces: `async send_files(item, *, creds, files: list) -> list[str]` returning uploaded media ids.

Read §6.3 and §6.4. The gate change is **mandatory**: `harness/prompts/platforms/whatsapp.md` already promises native attachments, so shipping without it sends literal `MEDIA:` text.

WhatsApp is the first platform with `send_files` but without `supports_edit`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_whatsapp_platform.py`:

```python
# ---------------------------------------------------------------------------
# send_files — two-step Graph upload
# ---------------------------------------------------------------------------

from surogates.channels.channel_media import OutboundFile

MEDIA_URL = graph_url(PNID, "media")


class TestSendFiles:
    @pytest.mark.asyncio
    async def test_uploads_then_sends_and_returns_media_ids(self):
        p = WhatsAppPlatform()
        files = [OutboundFile(filename="chart.png", mime_type="image/png", data=b"PNG")]
        with respx.mock(assert_all_called=True) as router:
            router.post(MEDIA_URL).mock(
                return_value=httpx.Response(200, json={"id": "media_up1"})
            )
            send = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.M1"}]},
                )
            )
            uploaded = await p.send_files(_item(""), creds=_creds(), files=files)
        assert uploaded == ["media_up1"]
        body = _json.loads(send.calls[0].request.content)
        assert body["type"] == "image"
        assert body["image"]["id"] == "media_up1"

    @pytest.mark.asyncio
    async def test_document_carries_filename(self):
        p = WhatsAppPlatform()
        files = [OutboundFile(filename="notes.pdf", mime_type="application/pdf",
                              data=b"PDF")]
        with respx.mock(assert_all_called=True) as router:
            router.post(MEDIA_URL).mock(
                return_value=httpx.Response(200, json={"id": "media_doc"})
            )
            send = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.D1"}]},
                )
            )
            await p.send_files(_item(""), creds=_creds(), files=files)
        body = _json.loads(send.calls[0].request.content)
        assert body["type"] == "document"
        assert body["document"]["filename"] == "notes.pdf"

    @pytest.mark.asyncio
    async def test_oversize_file_is_skipped_not_raised(self):
        p = WhatsAppPlatform()
        files = [
            OutboundFile(filename="big.png", mime_type="image/png",
                         data=b"x" * (6 * 1024 * 1024)),
            OutboundFile(filename="ok.png", mime_type="image/png", data=b"PNG"),
        ]
        with respx.mock(assert_all_called=True) as router:
            router.post(MEDIA_URL).mock(
                return_value=httpx.Response(200, json={"id": "media_ok"})
            )
            router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.OK"}]},
                )
            )
            uploaded = await p.send_files(_item(""), creds=_creds(), files=files)
        assert uploaded == ["media_ok"]

    @pytest.mark.asyncio
    async def test_caption_truncated_to_1024(self):
        p = WhatsAppPlatform()
        item = _item("x" * 2000)
        files = [OutboundFile(filename="a.png", mime_type="image/png", data=b"P")]
        with respx.mock(assert_all_called=True) as router:
            router.post(MEDIA_URL).mock(
                return_value=httpx.Response(200, json={"id": "m1"})
            )
            send = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.C"}]},
                )
            )
            await p.send_files(item, creds=_creds(), files=files)
        body = _json.loads(send.calls[0].request.content)
        assert len(body["image"]["caption"]) <= 1024

    @pytest.mark.asyncio
    async def test_returns_empty_without_token(self):
        p = WhatsAppPlatform()
        files = [OutboundFile(filename="a.png", mime_type="image/png", data=b"P")]
        assert await p.send_files(
            _item(""), creds={"access_token": ""}, files=files,
        ) == []
```

Add to `tests/test_channel_dispatcher.py`:

```python
async def test_media_gate_is_capability_based_not_slack_only():
    """A platform with send_files gets MEDIA: markers resolved; one without
    does not, so a marker is never stripped from a platform that cannot
    upload it."""
    from surogates.channels.dispatcher import ChannelDeliveryDispatcher

    source = inspect.getsource(ChannelDeliveryDispatcher._deliver_item)
    assert 'platform.kind == "slack" and "MEDIA:"' not in source
    assert 'send_files_fn is not None and "MEDIA:"' in source
```

(Import `inspect` at the top of that file if it is not already imported. This is a guard against silent reversion; the behavioural coverage lives in the WhatsApp platform tests.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogates && uv run pytest tests/test_whatsapp_platform.py -k SendFiles tests/test_channel_dispatcher.py -k media_gate -v`
Expected: FAIL — missing `send_files`, and the gate assertion fails.

- [ ] **Step 3: Implement `send_files` and change the gate**

Add to `whatsapp.py` (import `upload_media` from `whatsapp_api`):

```python
#: WhatsApp's caption cap.  Over-long captions are silently rejected by Meta.
_MAX_CAPTION_CHARS = 1024

#: Media kinds that accept a caption.
_CAPTIONABLE = ("image", "video", "document")
```

```python
    # ------------------------------------------------------------------
    # send_files — upload workspace files referenced by MEDIA: markers
    # ------------------------------------------------------------------

    async def send_files(
        self, item: Any, *, creds: dict, files: list,
    ) -> list[str]:
        """Upload *files* and post each as a native WhatsApp attachment.

        Two Graph hops per file: ``POST /media`` yields a media id, then
        ``POST /messages`` sends it.  Returns the uploaded media ids (empty
        when nothing uploaded).  Best-effort per file: any error is logged
        and skipped, never raised — the same contract as ``download_file``.
        """
        token: str = (creds or {}).get("access_token") or ""
        destination = item.destination or {}
        wa_id: str = destination.get("wa_id") or ""
        phone_number_id: str = destination.get("phone_number_id") or ""
        api_version: str = destination.get("api_version") or DEFAULT_API_VERSION
        if not token or not wa_id or not phone_number_id or not files:
            return []

        caption = render_whatsapp(item.payload.get("content", "") or "")[
            :_MAX_CAPTION_CHARS
        ]

        uploaded: list[str] = []
        for index, f in enumerate(files):
            media_id, error = await upload_media(
                self._http,
                token=token,
                phone_number_id=phone_number_id,
                filename=f.filename,
                data=f.data,
                mime_type=f.mime_type,
                api_version=api_version,
            )
            if media_id is None:
                logger.warning(
                    "[whatsapp] media upload failed for %s (%s)", f.filename, error,
                )
                continue

            kind = _media_kind(f.mime_type)
            block: dict[str, Any] = {"id": media_id}
            # Only the first attachment carries the caption; repeating it on
            # every file would spam a linear chat.
            if index == 0 and caption and kind in _CAPTIONABLE:
                block["caption"] = caption
            if kind == "document":
                block["filename"] = f.filename

            _, send_error = await send_message(
                self._http,
                token=token,
                phone_number_id=phone_number_id,
                payload={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": wa_id,
                    "type": kind,
                    kind: block,
                },
                api_version=api_version,
            )
            if send_error:
                logger.warning(
                    "[whatsapp] sending media %s failed (%s)", f.filename, send_error,
                )
                continue
            uploaded.append(media_id)
        return uploaded
```

Add the module-level helper:

```python
def _media_kind(mime_type: str) -> str:
    """Map a MIME type to the WhatsApp message ``type`` for that media."""
    base = (mime_type or "").split("/")[0].strip().lower()
    if base in ("image", "video", "audio"):
        return base
    return "document"
```

In `surogates/channels/dispatcher.py`, change line 760 and its comment:

```python
        # MEDIA: markers — strip from text and resolve workspace files.
        # Gated on the send_files capability: never strip a marker on a
        # platform that cannot upload it.
        media_files: list = []
        media_reply_only = False
        send_files_fn = getattr(platform, "send_files", None)
        content = item.payload.get("content") or ""
        if send_files_fn is not None and "MEDIA:" in content:
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /work/surogates && uv run pytest tests/test_whatsapp_platform.py tests/test_channel_dispatcher.py tests/test_slack_platform.py -v`
Expected: PASS — including every Slack test, which proves the gate change did not alter Slack behaviour. Telegram has no `send_files`, so it is unaffected.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/channels/platforms/whatsapp.py surogates/channels/dispatcher.py \
        tests/test_whatsapp_platform.py tests/test_channel_dispatcher.py
git commit -m "feat(channels): WhatsApp outbound media, MEDIA gate by capability"
```

---

## Task 8: Wire the channel into the platform's enumeration points

**Files:**
- Modify: `surogates/channels/platforms/__init__.py:12-13`
- Modify: `surogates/config.py:593-601`, `:637-639`
- Modify: `surogates/channels/constants.py:35`, `:41`, `:51`
- Modify: `surogates/channels/inbound.py:679`
- Modify: `surogates/channels/memory_boundary.py:15`
- Modify: `surogates/session/store.py:155`, and a branch beside `:1160`
- Test: `tests/test_whatsapp_platform.py`, `tests/test_channel_constants.py` (create if absent)

**Interfaces:**
- Consumes: `WhatsAppPlatform` (Task 3), `send` (Task 5).
- Produces: a fully routable channel — routes mounted, delivery loop running, outbox rows claimed.

Read §11 of the spec. Four of these are silent-failure traps.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_channel_constants.py`:

```python
"""Guards on the per-channel constant lists.

Each of these lists is a hardcoded enumeration that a new platform must
join.  Omission fails silently at runtime, so the guards live here.
"""

from __future__ import annotations

import surogates.channels.platforms  # noqa: F401  (registers every platform)
from surogates.channels.constants import (
    ADAPTER_CHANNELS,
    END_USER_CHANNELS,
    INTERACTIVE_PROMPT_CHANNELS,
)
from surogates.channels.memory_boundary import MANAGED_CHANNELS
from surogates.channels.registry import registry


def test_every_registered_platform_has_a_delivery_adapter():
    # An outbox row for a channel outside ADAPTER_CHANNELS is never claimed
    # and sits pending forever (store.py:1148).
    missing = {p.kind for p in registry.all()} - set(ADAPTER_CHANNELS)
    assert not missing, f"registered but not in ADAPTER_CHANNELS: {sorted(missing)}"


def test_whatsapp_can_render_input_prompts():
    # Outside INTERACTIVE_PROMPT_CHANNELS, _build_channel_payload leaves the
    # payload empty and store.py writes NO outbox row at all — the user sees
    # nothing and the session parks for 30 minutes.
    assert "whatsapp" in INTERACTIVE_PROMPT_CHANNELS


def test_whatsapp_is_an_end_user_channel():
    assert "whatsapp" in END_USER_CHANNELS


def test_whatsapp_has_a_memory_boundary():
    # Fail-open otherwise: session_memory_boundary returns None and WhatsApp
    # conversations share the per-user memory partition with web sessions.
    assert "whatsapp" in MANAGED_CHANNELS
```

If `registry.all()` does not exist, use the registry's actual enumeration accessor — check `surogates/channels/registry.py` for the method name and adapt.

Append to `tests/test_whatsapp_platform.py`:

```python
# ---------------------------------------------------------------------------
# Pending-input tuple + outbox destination
# ---------------------------------------------------------------------------


class TestWhatsAppPipelineWiring:
    def test_pending_input_tuple_includes_whatsapp(self):
        # The non-Slack fallthrough at inbound.py is the plain-text answer
        # path (resolve_text_answer); joining the tuple opts in for free.
        import inspect

        import surogates.channels.inbound as inbound

        source = inspect.getsource(inbound.ChannelInboundPipeline.handle)
        assert "whatsapp" in source, (
            "whatsapp missing from the pending-input platform tuple: a typed "
            "answer would be treated as a new message and never resolve"
        )

    def test_thread_dest_fields_has_whatsapp(self):
        from surogates.session.store import _THREAD_DEST_FIELDS

        assert "whatsapp" in _THREAD_DEST_FIELDS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogates && uv run pytest tests/test_channel_constants.py tests/test_whatsapp_platform.py -k Wiring -v`
Expected: FAIL on every assertion — `whatsapp` is in none of the lists.

- [ ] **Step 3: Make the edits**

`surogates/channels/platforms/__init__.py` — add after the telegram import:

```python
from surogates.channels.platforms import whatsapp as whatsapp  # noqa: F401
```

`surogates/config.py` — add beside `WebsiteChannelSettings` (~line 601):

```python
class WhatsAppChannelSettings(ChannelKindSettings):
    model_config = {"env_prefix": "SUROGATES_CHANNELS_WHATSAPP_"}
```

and on `ChannelsSettings`, beside the `website` field:

```python
    whatsapp: WhatsAppChannelSettings = Field(default_factory=WhatsAppChannelSettings)
```

`surogates/channels/constants.py`:

```python
ADAPTER_CHANNELS = frozenset({"slack", "telegram", "whatsapp"})
```
```python
INTERACTIVE_PROMPT_CHANNELS = frozenset({"slack", "telegram", "whatsapp"})
```
```python
END_USER_CHANNELS = frozenset({"web", "website", "slack", "telegram", "teams", "whatsapp"})
```

Update the `INTERACTIVE_PROMPT_CHANNELS` docstring, which currently misdescribes the failure mode:

```python
#: Channels whose platform can render an ``ask_user_question`` prompt
#: (Slack: Answer button + modal; Telegram: inline keyboard; WhatsApp:
#: numbered plain text).  A channel OUTSIDE this set gets no
#: ``input_prompt`` outbox row at all — ``_build_channel_payload`` returns
#: an empty payload and ``store`` drops it — so the user sees nothing and
#: the session parks waiting for an answer that can never arrive.
```

`surogates/channels/inbound.py:679`:

```python
        if deps.pending_input is not None and routing.platform in (
            "slack", "telegram", "whatsapp",
        ):
```

`surogates/channels/memory_boundary.py:15`:

```python
MANAGED_CHANNELS: frozenset[str] = frozenset({"slack", "telegram", "whatsapp"})
```

`surogates/session/store.py` — add to `_THREAD_DEST_FIELDS` (~line 155). WhatsApp has no threads, so both fields are `None`:

```python
    "whatsapp": (None, None),
```

and add the destination branch beside the telegram one (~line 1160):

```python
            elif channel == "whatsapp":
                destination = {
                    "wa_id": config.get("whatsapp_channel_id", ""),
                    "phone_number_id": config.get("channel_identifier", ""),
                    "api_version": config.get("api_version", ""),
                    "channel_identifier": config.get("channel_identifier", ""),
                }
```

`inbound.py:632-633` already writes `f"{routing.platform}_channel_id"` generically, so `whatsapp_channel_id` is populated with no further change.

If `_THREAD_DEST_FIELDS.get(channel, (None, None))` unpacking makes `thread_dest`/`thread_key` `None` and the telegram branch assigns `thread_dest: ...` as a dict key, confirm the WhatsApp branch does not — it must not include a `None` key. The branch above is already correct.

- [ ] **Step 4: Run the full suite**

Run: `cd /work/surogates && uv run pytest tests/ -x -q`
Expected: PASS. Every previously-passing test must still pass; the new constant guards go green.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/channels/platforms/__init__.py surogates/config.py \
        surogates/channels/constants.py surogates/channels/inbound.py \
        surogates/channels/memory_boundary.py surogates/session/store.py \
        tests/test_channel_constants.py tests/test_whatsapp_platform.py
git commit -m "feat(channels): register whatsapp across the channel enumeration points"
```

---

## Task 9: Permanent-error classification

**Files:**
- Modify: `surogates/channels/delivery.py:58-75`
- Test: `tests/test_channel_delivery.py` (extend, or create if absent)

**Interfaces:**
- Consumes: `whatsapp_api.format_graph_error`'s output shape (Task 2).
- Produces: correct retry-vs-dead classification for Graph errors.

Read §6.7. `is_permanent_delivery_error` is an **unanchored, case-insensitive substring test shared by every platform**, so bare numeric codes are dangerous.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_channel_delivery.py`:

```python
# ---------------------------------------------------------------------------
# WhatsApp Graph error classification
# ---------------------------------------------------------------------------


class TestGraphErrorClassification:
    @pytest.mark.parametrize(
        "error",
        [
            "graph error 190 (HTTP 401): Session has expired",
            "graph error 100 (HTTP 400): Unsupported get request",
            "graph error 131026 (HTTP 400): Message undeliverable",
            "graph error 131047 (HTTP 400): Re-engagement message",
        ],
    )
    def test_permanent_graph_errors(self, error):
        assert is_permanent_delivery_error(error) is True

    @pytest.mark.parametrize(
        "error",
        [
            "graph error 130429 (HTTP 400): Rate limit hit",
            "graph error 4 (HTTP 400): Application request limit reached",
            "HTTP 500: internal error",
            "HTTP 429: too many requests",
        ],
    )
    def test_retryable_graph_errors(self, error):
        assert is_permanent_delivery_error(error) is False

    def test_codeless_error_is_retryable(self):
        # format_graph_error omits the "graph error" prefix when Meta returns
        # no code, so a code-less error can never match a permanent prefix.
        assert is_permanent_delivery_error("HTTP 400: something odd") is False

    def test_unrelated_error_containing_100_stays_retryable(self):
        # The matcher is an unanchored substring test shared by every
        # platform: a bare "100" entry would kill these.
        assert is_permanent_delivery_error(
            "slack rate limited: retry after 1000 seconds",
        ) is False
        assert is_permanent_delivery_error(
            "upload failed: file exceeds 100 MB",
        ) is False
```

Import `is_permanent_delivery_error` from `surogates.channels.delivery` and `pytest` at the top of that file if not already present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogates && uv run pytest tests/test_channel_delivery.py -k Graph -v`
Expected: FAIL on the four `test_permanent_graph_errors` cases — they are currently classified retryable.

- [ ] **Step 3: Add the delimited prefixes**

In `surogates/channels/delivery.py`, add to `_PERMANENT_DELIVERY_ERRORS`:

```python
    # WhatsApp Cloud API (Meta Graph) — delimited prefixes, never bare codes.
    # ``is_permanent_delivery_error`` is an unanchored substring test shared
    # by every platform, so a bare "100" would mark any Slack or Telegram
    # error mentioning "1000 requests" or "100 MB" permanently dead.
    "graph error 190 (",     # token expired (463) or revoked (467)
    "graph error 100 (",     # bad object id — usually a phone number in the
                             # Phone Number ID field
    "graph error 131026 (",  # recipient not on WhatsApp / undeliverable
    "graph error 131047 (",  # 24-hour customer service window closed
```

Retryable codes need no entry — retryable is the default.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /work/surogates && uv run pytest tests/test_channel_delivery.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/channels/delivery.py tests/test_channel_delivery.py
git commit -m "feat(channels): classify permanent WhatsApp Graph delivery errors"
```

---

## Task 10: Operator documentation

**Files:**
- Create: `docs/channels/whatsapp.md`
- Modify: `docs/channels/index.md:7-13`

**Interfaces:**
- Consumes: everything above.
- Produces: operator-facing setup documentation.

Follow the archetype exactly: h1 + eight h2s, no h3, **no tables**, exactly one unlabelled fenced ASCII diagram inside `## How it works`. `telegram.md` is the closest analogue — read it first and match its heading set.

- [ ] **Step 1: Read the template**

Run: `cd /work/surogates && cat docs/channels/telegram.md`

Note its heading set: `# Telegram Channel` / `## How it works` / `## Setup` / `## Identity` / `## Sessions & threading` / `## Media` / `` ## Interactive input (`ask_user_question`) `` / `## Ops notes`.

- [ ] **Step 2: Write `docs/channels/whatsapp.md`**

Use the same eight headings. Content requirements, each of which must appear:

- **How it works** — one ASCII diagram: Meta webhook → `channels.surogate.ai/whatsapp/{phone_number_id}` → tenant resolution → pipeline → session; outbox → Graph `/messages`.
- **Setup** — numbered steps: create the Meta App ("Connect with customers through WhatsApp" use case); generate a **System User** token with `whatsapp_business_messaging`, `whatsapp_business_management`, `business_management` and expiration **Never**; collect phone number id, WABA id, app secret; **save the Studio form first**, then paste the callback URL and verify token into WhatsApp → Configuration → Edit webhook; subscribe the **`messages`** webhook field; note the 5-recipient dev-mode cap.
- State prominently that a dashboard token expires in 24 hours and the channel will silently die on day two.
- State that the form must be saved before pasting into Meta, because the handshake needs the routing row and vault entries to exist.
- **Identity** — `wa_id` is E.164 digits with no `+`; shadow users by default, pairing when `identity_policy=linked`.
- **Sessions & threading** — DM-only; no threads; one session per sender.
- **Media** — inbound images/documents/audio arrive as attachments; voice notes arrive as `.ogg` files, not transcripts; outbound `MEDIA:<path>` becomes a native attachment; per-type caps.
- **Interactive input** — `ask_user_question` renders as a numbered text prompt; a plain reply is the answer.
- **Ops notes** — the standard three bullets: `channels.whatsapp.enabled: true`; outbox retry 30 s / dead-letter 30 min; unknown-identifier fast-ack 200. Add a fourth: the agent never initiates, so a message sent more than 24 hours after the user's last one is rejected by Meta with `131047` and marked dead.

- [ ] **Step 3: Add the index row**

In `docs/channels/index.md`, add a WhatsApp row to the Available Channels list at `:7-13`, matching the existing two-column format.

- [ ] **Step 4: Verify**

Run: `cd /work/surogates && grep -c '^## ' docs/channels/whatsapp.md`
Expected: `8`

Run: `cd /work/surogates && grep -c '^### ' docs/channels/whatsapp.md`
Expected: `0` — the archetype has no h3.

Run: `cd /work/surogates && grep -c '^|' docs/channels/whatsapp.md`
Expected: `0` — the archetype has no tables.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add docs/channels/whatsapp.md docs/channels/index.md
git commit -m "docs(channels): WhatsApp channel operator guide"
```

---

## Task 11: Ops provisioner

**Files:**
- Modify: `/work/surogate-ops/surogate_ops/server/services/channel_provisioning.py` (append after the telegram block, ~line 629)
- Test: `/work/surogate-ops/tests/test_channel_provisioning.py`

**Interfaces:**
- Consumes: `ChannelProvisioner`, `PreparedChannel`, `_is_truthy`, `_identity_policy`, `as_org_uuid` from the same module.
- Produces: `CHANNEL_PROVISIONERS["whatsapp"]`.

**This is the single most load-bearing ops change.** Without it no `channel_routing` row is ever written and every inbound webhook fast-acks 200.

The verify token must be minted in a **`prepare` hook**, not a sync `credentials` callable — a sync callable re-mints on every env save and breaks the already-registered Meta webhook. Telegram's hook is the exact precedent.

The hook also derives the display phone number via Graph and returns it as `extra_env`, because the Studio `runConnect` helper treats an empty derived value as a failed validation and rolls the channel back to disabled (Task 14).

- [ ] **Step 1: Create the branch and write the failing tests**

```bash
cd /work/surogate-ops && git checkout -b feat/whatsapp-cloud-channel
```

Append to `tests/test_channel_provisioning.py`, mirroring the existing telegram section:

```python
# ---------------------------------------------------------------------------
# WhatsApp provisioner
# ---------------------------------------------------------------------------


class TestWhatsAppProvisioner:
    def test_registered(self):
        assert "whatsapp" in CHANNEL_PROVISIONERS

    def test_enabled_env(self):
        assert CHANNEL_PROVISIONERS["whatsapp"].enabled_env == "SUROGATES_WHATSAPP_ENABLED"

    def test_uses_a_prepare_hook(self):
        # The verify token must be minted idempotently; a sync credentials
        # callable would re-mint on every save and break the registered
        # Meta webhook.
        assert CHANNEL_PROVISIONERS["whatsapp"].prepare is not None

    async def test_prepare_returns_none_without_phone_number_id(self):
        prov = CHANNEL_PROVISIONERS["whatsapp"]
        result = await prov.prepare(
            {"SUROGATES_WHATSAPP_ACCESS_TOKEN": "EAAx"},
            surogates_client=_FakeSurogatesClient(),
            project_id="00000000-0000-0000-0000-000000000001",
            http_client=_FakeHttp(),
        )
        assert result is None

    async def test_prepare_returns_none_without_access_token(self):
        prov = CHANNEL_PROVISIONERS["whatsapp"]
        result = await prov.prepare(
            {"SUROGATES_WHATSAPP_PHONE_NUMBER_ID": "779418925277868"},
            surogates_client=_FakeSurogatesClient(),
            project_id="00000000-0000-0000-0000-000000000001",
            http_client=_FakeHttp(),
        )
        assert result is None

    async def test_prepare_builds_channel(self):
        prov = CHANNEL_PROVISIONERS["whatsapp"]
        env = {
            "SUROGATES_WHATSAPP_PHONE_NUMBER_ID": "779418925277868",
            "SUROGATES_WHATSAPP_ACCESS_TOKEN": "EAAtoken",
            "SUROGATES_WHATSAPP_APP_SECRET": "0123456789abcdef0123456789abcdef",
            "SUROGATES_WHATSAPP_WABA_ID": "215589313241560",
        }
        http = _FakeHttp(json_body={"display_phone_number": "+1 555 179 7781"})
        result = await prov.prepare(
            env,
            surogates_client=_FakeSurogatesClient(),
            project_id="00000000-0000-0000-0000-000000000001",
            http_client=http,
        )
        assert result is not None
        assert result.identifier == "779418925277868"
        assert result.credentials["access_token"] == "EAAtoken"
        assert result.credentials["app_secret"] == "0123456789abcdef0123456789abcdef"
        assert result.credentials["phone_number_id"] == "779418925277868"
        assert result.credentials["verify_token"]
        assert result.config["waba_id"] == "215589313241560"
        assert result.extra_env["SUROGATES_WHATSAPP_DISPLAY_PHONE"] == "+1 555 179 7781"

    async def test_prepare_reuses_existing_verify_token(self):
        # Re-minting would break the webhook Meta has already verified.
        prov = CHANNEL_PROVISIONERS["whatsapp"]
        client = _FakeSurogatesClient(credential="already-minted")
        env = {
            "SUROGATES_WHATSAPP_PHONE_NUMBER_ID": "779418925277868",
            "SUROGATES_WHATSAPP_ACCESS_TOKEN": "EAAtoken",
            "SUROGATES_WHATSAPP_APP_SECRET": "0123456789abcdef0123456789abcdef",
        }
        result = await prov.prepare(
            env,
            surogates_client=client,
            project_id="00000000-0000-0000-0000-000000000001",
            http_client=_FakeHttp(json_body={"display_phone_number": "+1"}),
        )
        assert result.credentials["verify_token"] == "already-minted"
        assert client.read_keys == ["whatsapp_verify_token_779418925277868"]

    async def test_prepare_survives_graph_failure(self):
        # A Graph outage must not block provisioning; the display phone is
        # cosmetic, and the identifier comes from env.
        prov = CHANNEL_PROVISIONERS["whatsapp"]
        env = {
            "SUROGATES_WHATSAPP_PHONE_NUMBER_ID": "779418925277868",
            "SUROGATES_WHATSAPP_ACCESS_TOKEN": "EAAtoken",
            "SUROGATES_WHATSAPP_APP_SECRET": "0123456789abcdef0123456789abcdef",
        }
        result = await prov.prepare(
            env,
            surogates_client=_FakeSurogatesClient(),
            project_id="00000000-0000-0000-0000-000000000001",
            http_client=_FakeHttp(status_code=500),
        )
        assert result is not None
        assert result.extra_env["SUROGATES_WHATSAPP_DISPLAY_PHONE"] == "779418925277868"
```

Add the two fakes near the file's other helpers if they do not already exist:

```python
class _FakeSurogatesClient:
    """Records vault reads; returns a fixed credential or None."""

    def __init__(self, credential: str | None = None):
        self.credential = credential
        self.read_keys: list[str] = []

    async def get_credential(self, org_id, key):
        self.read_keys.append(key)
        return self.credential


class _FakeHttp:
    """Minimal async http double exposing .get()."""

    def __init__(self, *, status_code: int = 200, json_body: dict | None = None):
        self.status_code = status_code
        self.json_body = json_body or {}

    async def get(self, url, **kwargs):
        body = self.json_body
        code = self.status_code

        class _Response:
            is_success = 200 <= code < 300
            status_code = code

            @staticmethod
            def json():
                return body

        return _Response()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogate-ops && pytest tests/test_channel_provisioning.py -k WhatsApp -v`

**Do not use `uv run` here** — it reinstalls the pinned surogates wheel and clobbers the local dev install.

Expected: FAIL — `KeyError: 'whatsapp'`

- [ ] **Step 3: Implement the provisioner**

Append to `channel_provisioning.py`, after the telegram block:

```python
# ---------------------------------------------------------------------------
# WhatsApp provisioner
# ---------------------------------------------------------------------------

def _whatsapp_config(env: dict) -> dict[str, Any]:
    """Extract WhatsApp behaviour config from env-vars.

    Key names are the runtime descriptor's ``config_keys`` contract, not the
    env-var names.
    """
    return {
        "require_mention": _is_truthy(
            str(env.get("SUROGATES_WHATSAPP_REQUIRE_MENTION", ""))
        ),
        "allow_bots": _is_truthy(
            str(env.get("SUROGATES_WHATSAPP_ALLOW_BOTS", ""))
        ),
        "identity_policy": _identity_policy(env, "SUROGATES_WHATSAPP_IDENTITY_POLICY"),
        "waba_id": (env.get("SUROGATES_WHATSAPP_WABA_ID") or "").strip(),
        "api_version": (env.get("SUROGATES_WHATSAPP_API_VERSION") or "").strip(),
    }


async def _whatsapp_prepare(
    env: dict,
    *,
    surogates_client: Any,
    project_id: str,
    http_client: Any,
) -> Optional[PreparedChannel]:
    """Async prepare hook for the WhatsApp provisioner.

    1. Reads the phone number id, access token and app secret; returns
       ``None`` if any is absent.
    2. Fetches or mints the webhook verify token from the surogates vault.
       This MUST be idempotent: re-minting would break the callback URL Meta
       has already verified, and the operator would have to re-verify by
       hand with no diagnostic.
    3. Best-effort Graph lookup of the display phone number, returned as
       ``extra_env`` — Studio's connect flow treats an empty derived value
       as a failed validation and rolls the channel back to disabled.
    """
    phone_number_id = (env.get("SUROGATES_WHATSAPP_PHONE_NUMBER_ID") or "").strip()
    access_token = (env.get("SUROGATES_WHATSAPP_ACCESS_TOKEN") or "").strip()
    app_secret = (env.get("SUROGATES_WHATSAPP_APP_SECRET") or "").strip()
    if not phone_number_id or not access_token or not app_secret:
        return None

    # Idempotent verify token.  Read under the SAME org-UUID key that
    # store_credential writes with (``as_org_uuid(project_id)``), and at the
    # same key shape sync_channel_routing_from_env writes:
    # f"{kind}_{cred_name}_{identifier}".
    vault_key = f"whatsapp_verify_token_{phone_number_id}"
    try:
        existing_token = await surogates_client.get_credential(
            as_org_uuid(project_id), vault_key,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to read whatsapp verify token from vault (%s); minting new one",
            exc,
        )
        existing_token = None

    verify_token = existing_token or _mint_webhook_secret()

    # Derive the display phone number for the Studio connect flow.  Cosmetic:
    # a Graph outage must not block provisioning.
    api_version = (env.get("SUROGATES_WHATSAPP_API_VERSION") or "v23.0").strip()
    display_phone = phone_number_id
    try:
        response = await http_client.get(
            f"https://graph.facebook.com/{api_version}/{phone_number_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.is_success:
            display_phone = (
                response.json().get("display_phone_number") or phone_number_id
            )
        else:
            logger.warning(
                "WhatsApp phone-number lookup returned HTTP %s; "
                "falling back to the phone number id",
                response.status_code,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "WhatsApp phone-number lookup failed (%s); "
            "falling back to the phone number id",
            exc,
        )

    return PreparedChannel(
        identifier=phone_number_id,
        credentials={
            "access_token": access_token,
            "app_secret": app_secret,
            "verify_token": verify_token,
            # The platform needs the tenant id wherever creds are available
            # (send_private / post_input_nudge get no routing object).
            "phone_number_id": phone_number_id,
        },
        config=_whatsapp_config(env),
        extra_env={"SUROGATES_WHATSAPP_DISPLAY_PHONE": display_phone},
    )


CHANNEL_PROVISIONERS["whatsapp"] = ChannelProvisioner(
    enabled_env="SUROGATES_WHATSAPP_ENABLED",
    prepare=_whatsapp_prepare,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /work/surogate-ops && pytest tests/test_channel_provisioning.py -v`
Expected: PASS — including every pre-existing slack/telegram test.

- [ ] **Step 5: Commit**

```bash
cd /work/surogate-ops
git add surogate_ops/server/services/channel_provisioning.py tests/test_channel_provisioning.py
git commit -m "feat(channels): WhatsApp channel provisioner"
```

---

## Task 12: Ops commerce vocabulary and routing lists

**Files:**
- Modify: `surogate_ops/core/commerce/features.py:66`, `:93`
- Modify: `surogate_ops/core/surogates_client.py:144-155`
- Modify: `surogate_ops/server/routes/agent_runtime.py:103`, `:256`
- Modify: `surogate_ops/server/routes/commerce_public.py:230`
- Modify: `surogate_ops/core/db/models/operate.py:48-50`
- Test: `tests/test_offer_features.py` (**invert** an existing test)

**Interfaces:**
- Consumes: Task 11's provisioner.
- Produces: WhatsApp recognised as a sellable, meterable, linkable channel.

`CHANNEL_VOCAB` is **security-shaped**: `features_allow_channel` returns `True` for any channel outside the vocab, so a buyer on a Slack-only package would chat over WhatsApp unmetered.

No database migration is needed — `channel_kind` is `String(64)` and `ChannelKind` is a plain class of `str` constants, not an enum. Note that migration `a3f9c1d84e72`'s *downgrade* recreates the old PG enum with only slack/telegram/website and will fail once a `whatsapp` row exists; the schema is forward-only past that point.

- [ ] **Step 1: Update the tests**

In `tests/test_offer_features.py`, the existing test uses `whatsapp` as its example of an unknown channel. **Invert it** — do not merely add a new test:

```python
def test_unknown_channel_rejected():
    with pytest.raises(ValueError, match="unknown channel"):
        validate_offer_features({"channels": ["signal"]})


def test_whatsapp_is_a_known_channel():
    # whatsapp used to be this file's example of an unknown channel.
    validate_offer_features({"channels": ["whatsapp"]})


def test_whatsapp_is_metered():
    # features_allow_channel returns True for anything outside CHANNEL_VOCAB,
    # so a channel missing from the vocab is never gated — a buyer on a
    # slack-only package could chat over it unmetered.
    effective = {"channels": ["slack"]}
    assert features_allow_channel(effective, "whatsapp") is False
    assert features_allow_channel(effective, "slack") is True
```

Import `features_allow_channel` if it is not already imported.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogate-ops && pytest tests/test_offer_features.py -v`
Expected: FAIL — `test_whatsapp_is_a_known_channel` raises `ValueError: unknown channel`.

- [ ] **Step 3: Make the edits**

`core/commerce/features.py`:

```python
CHANNEL_VOCAB = frozenset({"slack", "telegram", "website", "whatsapp"})
```

and in `CHANNEL_LABELS`, add:

```python
    "whatsapp": "WhatsApp",
```

`core/surogates_client.py` — in `_format_channel_name`'s map, add:

```python
    "whatsapp": "WhatsApp",
```

(Telegram is currently missing from this map and renders via the `.title()` fallback. Adding it too is a one-line improvement; do so.)

`server/routes/agent_runtime.py:103`:

```python
_MANAGED_CHANNELS: frozenset[str] = frozenset({"slack", "telegram", "whatsapp"})
```

`server/routes/agent_runtime.py:256`:

```python
_LINKABLE_CHANNEL_KINDS: tuple[str, ...] = ("slack", "telegram", "whatsapp")
```

`server/routes/commerce_public.py:230` — add `"whatsapp"` to the inline `channel_kind.in_([...])` list. This is a duplicate of `_LINKABLE_CHANNEL_KINDS`; leave the duplication in place rather than refactoring in this task.

`core/db/models/operate.py:48-50` — add the constant beside the others:

```python
    whatsapp = "whatsapp"
```

- [ ] **Step 4: Run the ops suite**

Run: `cd /work/surogate-ops && pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /work/surogate-ops
git add surogate_ops/core/commerce/features.py surogate_ops/core/surogates_client.py \
        surogate_ops/server/routes/agent_runtime.py \
        surogate_ops/server/routes/commerce_public.py \
        surogate_ops/core/db/models/operate.py tests/test_offer_features.py
git commit -m "feat(channels): recognise whatsapp in commerce vocab and routing lists"
```

---

## Task 13: Admin channels tab

**Files:**
- Modify: `frontend/src/features/agents/channels-tab.tsx:25-46`, `:219-239`
- Test: `frontend/src/features/agents/__tests__/channels-env.test.ts`

**Interfaces:**
- Consumes: the `SUROGATES_WHATSAPP_*` env-var names from Task 11.
- Produces: `CHANNEL_ENV_KEYS` covering WhatsApp, and an admin form that re-emits every one of them.

**The invariant:** a key belongs in `CHANNEL_ENV_KEYS` only if something re-emits it on save — a form from its own state, or the provisioner's `extra_env`. `preserveExternalEnv` carries forward everything *not* in the set. `SUROGATES_WHATSAPP_DISPLAY_PHONE` is re-minted server-side (Task 11), so it belongs in the set without a form field, exactly like `SUROGATES_TELEGRAM_USERNAME`.

**Two forms share this one set** (Task 14 covers the other). Both must be updated, or the one you skip deletes the credentials on save.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/features/agents/__tests__/channels-env.test.ts`:

```typescript
describe("WhatsApp env keys", () => {
  const WHATSAPP_KEYS = [
    "SUROGATES_WHATSAPP_ENABLED",
    "SUROGATES_WHATSAPP_PHONE_NUMBER_ID",
    "SUROGATES_WHATSAPP_ACCESS_TOKEN",
    "SUROGATES_WHATSAPP_APP_SECRET",
    "SUROGATES_WHATSAPP_WABA_ID",
    "SUROGATES_WHATSAPP_API_VERSION",
    "SUROGATES_WHATSAPP_DISPLAY_PHONE",
    "SUROGATES_WHATSAPP_IDENTITY_POLICY",
    "SUROGATES_WHATSAPP_ALLOW_BOTS",
  ];

  it.each(WHATSAPP_KEYS)("%s is a channel-owned key", (key) => {
    expect(CHANNEL_ENV_KEYS.has(key)).toBe(true);
  });

  it("preserveExternalEnv drops channel-owned whatsapp keys", () => {
    // They are dropped because the form re-emits them (or the provisioner
    // re-mints them). A key in the set that nothing re-emits is deleted.
    const preserved = preserveExternalEnv([
      { key: "SUROGATES_WHATSAPP_ACCESS_TOKEN", value: "EAAx" },
      { key: "MY_OWN_VAR", value: "keep" },
    ]);
    expect(preserved).toEqual({ MY_OWN_VAR: "keep" });
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogate-ops/frontend && npm test -- channels-env`
Expected: FAIL — every key assertion.

- [ ] **Step 3: Implement**

In `channels-tab.tsx`, add all nine keys to `CHANNEL_ENV_KEYS` (`:25-46`).

Add WhatsApp form state beside the Telegram state, and WhatsApp fields to the `env_vars` literal at `:219-239`:

```typescript
      SUROGATES_WHATSAPP_ENABLED: String(whatsappEnabled),
      SUROGATES_WHATSAPP_PHONE_NUMBER_ID: whatsappPhoneNumberId,
      SUROGATES_WHATSAPP_ACCESS_TOKEN: whatsappAccessToken,
      SUROGATES_WHATSAPP_APP_SECRET: whatsappAppSecret,
      SUROGATES_WHATSAPP_WABA_ID: whatsappWabaId,
      SUROGATES_WHATSAPP_API_VERSION: whatsappApiVersion,
      SUROGATES_WHATSAPP_IDENTITY_POLICY: whatsappIdentityPolicy,
      SUROGATES_WHATSAPP_ALLOW_BOTS: whatsappAllowBots,
```

Note `SUROGATES_WHATSAPP_DISPLAY_PHONE` is deliberately **absent** from the literal — the provisioner re-mints it, exactly as `SUROGATES_TELEGRAM_USERNAME` is absent today.

Add the JSX section mirroring the Telegram block, with these fields: Phone Number ID, Access Token (password), App Secret (password), WABA ID, plus the identity-policy and allow-bots toggles the other channels have.

- [ ] **Step 4: Run tests and typecheck**

Run: `cd /work/surogate-ops/frontend && npm test -- channels-env && npm run typecheck`
Expected: PASS both.

- [ ] **Step 5: Commit**

```bash
cd /work/surogate-ops
git add frontend/src/features/agents/channels-tab.tsx \
        frontend/src/features/agents/__tests__/channels-env.test.ts
git commit -m "feat(studio): WhatsApp fields in the admin channels tab"
```

---

## Task 14: Studio connect and manage views

**Files:**
- Modify: `frontend/src/features/work/work-agent-channels-tab.tsx` — `ChannelView` union `:44-53`; new views modelled on `TelegramConnectView:1137` / `TelegramManageView:1220`; state + `env_vars` literal `:1456-1477`; list-view cards `:1996-2011`; view-routing chain `:1797`, `:1814`, `:1884`, `:1900`
- Test: manual verification against a real Meta app, plus `npm run typecheck`

**Interfaces:**
- Consumes: `CHANNEL_ENV_KEYS` (Task 13); `SUROGATES_WHATSAPP_DISPLAY_PHONE` from the provisioner (Task 11).
- Produces: the operator-facing connect flow.

**Without the list-view cards and the routing chain, the new views are unreachable** — they are not optional polish.

`runConnect` (`:1500-1541`) is generic over key *names* but hard-requires a server-derived env value: it treats an empty `derivedKey` as a failed token validation and rolls the channel back to disabled (`:1522-1536`). Pass `derivedKey: "SUROGATES_WHATSAPP_DISPLAY_PHONE"`, which Task 11's provisioner always populates (falling back to the phone number id on a Graph outage, so it is never empty).

- [ ] **Step 1: Extend the ChannelView union**

At `:44-53`, add:

```typescript
  | "whatsapp"
  | "whatsapp-connecting"
  | "whatsapp-reconnect"
```

- [ ] **Step 2: Add the env_vars entries**

In the save handler's `env_vars` literal (`:1456-1477`), add the same eight keys as Task 13 (again omitting `SUROGATES_WHATSAPP_DISPLAY_PHONE`). The two forms must stay in lockstep — a key emitted by one and not the other is deleted whenever the other saves.

- [ ] **Step 3: Write `WhatsAppConnectView`**

Model it on `TelegramConnectView` (`:1137`). It must collect: Phone Number ID, Access Token, App Secret, WABA ID.

Paste-time validation, each with a message that names what was pasted, what the field wants, and where to find it:

- **Phone Number ID** — numeric only. If 10–12 digits, reject with: *"That looks like a phone number — but this field needs the Phone Number ID (Meta's internal ID, 15–17 digits). Look just below the 'From' dropdown in API Setup."* This is the single highest-value validation in the whole flow.
- **Access Token** — must start with `EAA`, length ≥ 100. Diagnose known wrong prefixes: `sk-` → "that's an OpenAI key", `xoxb-`/`xoxp-` → "that's a Slack token", `ghp_`/`gho_` → "that's a GitHub token".
- **App Secret** — exactly 32 hex characters, **rejecting uppercase** with *"Meta app secrets are lowercase hex — check your paste."* Do not lowercase before matching: that would let an uppercase paste pass validation and then fail HMAC at runtime.
- **WABA ID** — numeric, 10–25 characters.

Call `runConnect` with:

```typescript
{
  enabledKey: "SUROGATES_WHATSAPP_ENABLED",
  derivedKey: "SUROGATES_WHATSAPP_DISPLAY_PHONE",
  platformLabel: "WhatsApp",
  channelView: "whatsapp",
  reconnectView: "whatsapp-reconnect",
  setEnabled: setWhatsappEnabled,
}
```

- [ ] **Step 4: Write `WhatsAppManageView`**

Model it on `TelegramManageView` (`:1220`). It must render, with copy buttons:

- The **callback URL**, built from the phone number id: `https://channels.surogate.ai/whatsapp/{phone_number_id}`. Keep this in one place so it cannot drift from the mounted route.
- The **verify token**, shown once, with a copy button. Rotation must default to **No** — rotating breaks the webhook Meta has already verified.
- A prominent instruction that **the form must be saved before pasting the URL into Meta**. If reversed, the handshake fast-acks 200 with an empty body and Meta rejects the URL with no diagnostic. This is the most likely setup failure.
- A reminder to subscribe the **`messages`** webhook field — omitting it is the classic "verified but nothing arrives".
- A note that dev-mode numbers can only message 5 whitelisted recipients, and that this is Meta's *recipient whitelist* (who you can send to) — distinct from the agent's own allow-list (who may talk to it). Operators conflate these constantly.
- A warning that a dashboard token expires in 24 hours and only a System User token with expiration **Never** survives.

- [ ] **Step 5: Wire the views into the routing chain and list**

Add WhatsApp cases at `:1797`, `:1814`, `:1884`, `:1900` alongside the Telegram cases, and a WhatsApp card in the list view at `:1996-2011`. **The views are unreachable without these.**

- [ ] **Step 6: Verify**

Run: `cd /work/surogate-ops/frontend && npm run typecheck && npm run lint && npm test`
Expected: PASS all three. The `ChannelView` union makes the compiler flag any routing branch you missed.

- [ ] **Step 7: Commit**

```bash
cd /work/surogate-ops
git add frontend/src/features/work/work-agent-channels-tab.tsx
git commit -m "feat(studio): WhatsApp connect and manage views"
```

---

## Task 15: Remaining frontend enumeration lists

**Files:**
- Modify: `frontend/src/features/work/work-home-page.tsx:40`, `:46-54`, `:58-64`
- Modify: `frontend/src/features/work/work-agent-overview-page.tsx:67-82`
- Modify: `frontend/src/features/work/work-agent-overview-state.ts:11-18`, `:24-46`
- Modify: `frontend/src/features/work/work-agent-settings-page.tsx:1676`
- Modify: `frontend/src/features/agents/agent-commerce-panel.tsx:169-173`
- Modify: `frontend/src/features/onboarding/use-onboarding-progress.ts:19-24`
- Modify: `frontend/src/features/public-agent/buy-page.tsx:344-345`
- Test: `frontend/src/features/work/__tests__/work-agent-overview-state.test.ts`, `frontend/src/features/agents/__tests__/agent-commerce-panel.test.tsx`

**Interfaces:**
- Consumes: everything above.
- Produces: WhatsApp visible in every Studio surface.

- [ ] **Step 1: Write the failing tests**

Append to `work-agent-overview-state.test.ts`:

```typescript
describe("WhatsApp in the overview", () => {
  it("is a visible overview channel", () => {
    // A channel outside this set is filtered out of the chart entirely.
    expect(VISIBLE_OVERVIEW_CHANNELS).toContain("whatsapp");
  });

  it("has a display label", () => {
    expect(channelLabel("whatsapp")).toBe("WhatsApp");
  });
});
```

Append to `agent-commerce-panel.test.tsx`:

```typescript
it("offers whatsapp as a sellable channel", () => {
  expect(SELLABLE_CHANNELS.map((c) => c.id)).toContain("whatsapp");
});
```

Add a buy-page test:

```typescript
it("renders a third linkable channel by its own name", () => {
  // Pre-existing bug: anything that wasn't "slack" rendered as "Telegram".
  expect(formatLinkableChannel("whatsapp")).toBe("WhatsApp");
  expect(formatLinkableChannel("telegram")).toBe("Telegram");
  expect(formatLinkableChannel("slack")).toBe("Slack");
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogate-ops/frontend && npm test -- overview-state commerce-panel buy-page`
Expected: FAIL on each new assertion.

- [ ] **Step 3: Make the edits**

- `work-home-page.tsx` — add `"whatsapp"` to the `ChannelKey` union, an env probe for `SUROGATES_WHATSAPP_ENABLED`, and `whatsapp: "WhatsApp"` to the label map.
- `work-agent-overview-page.tsx` — add WhatsApp to `activeChannelNames`.
- `work-agent-overview-state.ts` — add `"whatsapp"` to `VISIBLE_OVERVIEW_CHANNELS` and a `WhatsApp` branch to the label if-chain.
- `work-agent-settings-page.tsx:1676` — `(["slack","telegram","website","whatsapp"] as const)`. The `SUROGATES_${c.toUpperCase()}_ENABLED` derivation is already generic.
- `agent-commerce-panel.tsx:169-173` — add WhatsApp to `SELLABLE_CHANNELS`. **Its id must match the settings-page list above**, or the channel is silently unsellable.
- `use-onboarding-progress.ts:19-24` — add WhatsApp to the channel list.
- `buy-page.tsx:344-345` — replace the two-way ternary with a lookup that handles any kind. Extract it as `formatLinkableChannel` so the test above can import it:

```typescript
const LINKABLE_CHANNEL_LABELS: Record<string, string> = {
  slack: "Slack",
  telegram: "Telegram",
  whatsapp: "WhatsApp",
};

export function formatLinkableChannel(kind: string): string {
  return LINKABLE_CHANNEL_LABELS[kind] ?? kind;
}
```

- [ ] **Step 4: Verify**

Run: `cd /work/surogate-ops/frontend && npm test && npm run typecheck && npm run lint`
Expected: PASS all three.

- [ ] **Step 5: Commit**

```bash
cd /work/surogate-ops
git add frontend/src/features/work/work-home-page.tsx \
        frontend/src/features/work/work-agent-overview-page.tsx \
        frontend/src/features/work/work-agent-overview-state.ts \
        frontend/src/features/work/work-agent-settings-page.tsx \
        frontend/src/features/agents/agent-commerce-panel.tsx \
        frontend/src/features/onboarding/use-onboarding-progress.ts \
        frontend/src/features/public-agent/buy-page.tsx \
        frontend/src/features/work/__tests__ frontend/src/features/agents/__tests__
git commit -m "feat(studio): surface whatsapp across the remaining channel lists"
```

---

## Deployment notes

Not tasks — do these when shipping, and read the spec's §14 risk table first.

1. **Enable the channel in the runtime config.** PROD reads the hand-applied ConfigMap `surogates-runtime-config` (`k8s/surogates-runtime/production/30-runtime-configmap.yaml:189-195`), **not** a chart template. Add `whatsapp: { enabled: true }` under `channels:` and restart the channels Deployment. The per-kind `channels.*.enabled` keys in `values.yaml:187-190` are inert — no template consumes them.
2. **`templates/channels-deployment.yaml` needs no change** — one Deployment runs `surogates channels` for every platform.
3. **PROD `runtime-channels` was deployed with `kubectl`, not helm**, and the `runtime` release has diverged from the on-disk chart. Dump the live state before any deploy; do not assume a helm upgrade is safe.
4. **The migration is forward-only.** `a3f9c1d84e72`'s downgrade recreates the old PG enum with only slack/telegram/website and will fail once a `whatsapp` row exists.

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: §3.4 Graph version → Global Constraints + T2 · §4 GET handshake → T4 · §5.1–5.2 → T3 · §5.3 parse → T3 · §5.4 group refusal → T3 · §5.5 dedup → T3 (`ts = wamid`) · §6.1 send → T5 · §6.2 ack_received → T6 · §6.3 send_files → T7 · §6.4 MEDIA gate → T7 · §6.5 download_file → T6 · §6.6 transcoder → T1 · §6.7 error classification → T9 · §7 credentials → T11 · §8 no migration → T12 + Deployment notes · §3.3 ask_user_question → T5 (renderer) + T8 (constants) · §11 surogates → T8 · §11 ops backend → T11, T12 · §11 frontend → T13, T14, T15 · §12 Studio flow → T13, T14 · §13 testing → distributed across every task.

**Type consistency.** `render_whatsapp` (T1) is called in T5 and T7. `send_message`/`upload_media`/`download_media`/`graph_url`/`format_graph_error`/`ext_for_mime` (T2) are used with identical signatures in T3, T5, T6, T7. `_media_kind` (T7) and `_kind_for_mime` (T2) are deliberately separate: the former returns a WhatsApp message `type`, the latter a cap-table key. `msg.source["phone_number_id"]`/`["wamid"]` set in T3 are read in T6. The destination keys `wa_id`/`phone_number_id`/`api_version` written in T8 are read in T5 and T7. `SUROGATES_WHATSAPP_DISPLAY_PHONE` produced in T11 is consumed in T13 and T14.

**Known follow-ups deliberately deferred**, all recorded in spec §10: no templates or 24-hour-window machinery; no native buttons; no voice transcription; no reply quoting; no `statuses[]` → outbox reconciliation; no Studio credential alarm; no durable cross-replica dedup; no client-side send throttling; no live Graph probes in Studio; no Embedded Signup; no groups.
