# fetch_channel_messages Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Slack-channel agents an on-demand `fetch_channel_messages` tool to read recent channel history (with `limit`/`since`/`user` filters), and reframe the one-time backfill snapshot so the model uses it.

**Architecture:** A thin builtin tool (`toolset="channels"`) delegates over the session-scoped harness API client to a new server route, which resolves the bot token from the vault and calls the existing `SlackPlatform.fetch_channel_context` primitive, then a pure filter/format core. Mirrors the existing `fetch_channel_file` path end-to-end. The bot token never leaves the server.

**Tech Stack:** Python 3.12, FastAPI, pytest, Slack `AsyncWebClient` (already wired), frozen dataclasses.

## Global Constraints

- Base branch: `master` (surogates repo). Feature branch already created: `feat/fetch-channel-messages-tool`.
- surogates-only change; no ops-server change.
- Conventional Commits (`type(scope): subject`). No Plan/Task/Phase numbers in code or commit messages.
- Channel-only (DMs/MPDMs already excluded by `fetch_channel_context`). No thread replies, no cursor pagination exposed to the model, no server-side search.
- `limit` default 50, clamped to `1..200`. `since` accepts ISO date (`2026-07-01`) or relative (`24h`, `7d`); invalid `since` → HTTP 400. `user` accepts bare id (`U063…`) or mention (`<@U063…>`), normalized to bare id.
- Do NOT run `uv run` in this repo; run pytest via the ambient venv (`pytest ...`).

## Progress

- [x] Task 1: Add `author_id` to `RawMessage` and populate it
- [x] Task 2: Pure filter/format core + parameterized header
- [x] Task 3: `message_fetch.py` orchestrator
- [x] Task 4: Server route + `_resolve_session` split
- [x] Task 5: Harness API client method
- [x] Task 6: Builtin tool + runtime/router registration
- [ ] Task 7: Full-suite regression + branch wrap-up (in progress)

---

### Task 1: Add `author_id` to `RawMessage` and populate it

`RawMessage` currently carries only a resolved display-name `author`. The `user` filter needs the raw Slack user id, so add an `author_id` field (defaulted, so existing positional/keyword construction keeps working) and populate it in the platform.

**Files:**
- Modify: `surogates/channels/channel_backfill.py` (the `RawMessage` dataclass, ~line 41)
- Modify: `surogates/channels/platforms/slack.py:1300-1305` (the `RawMessage(...)` construction)
- Test: `tests/test_slack_fetch_channel_context.py`

**Interfaces:**
- Produces: `RawMessage(ts: float, author: str, text: str, files: tuple[tuple[str,str],...] = (), author_id: str = "")`

- [ ] **Step 1: Write the failing test** — add to `tests/test_slack_fetch_channel_context.py`:

```python
async def test_fetch_channel_context_populates_author_id(monkeypatch):
    """author_id carries the raw Slack user id; author stays the display name."""
    from surogates.channels.channel_backfill import BackfillLimits
    from surogates.channels.platforms.slack import SlackPlatform

    platform = SlackPlatform()

    class _FakeClient:
        async def conversations_info(self, channel):
            return {"channel": {"name": "surogate", "topic": {}, "purpose": {}}}

        async def conversations_history(self, **kwargs):
            return {"messages": [
                {"ts": "1720000000.0", "user": "U063C2DB7GW", "text": "hi"},
            ], "has_more": False, "response_metadata": {}}

    monkeypatch.setattr(platform, "_get_client", lambda token: _FakeClient())
    monkeypatch.setattr(platform, "_resolve_bot_user_id", _amock(return_value="UBOT"))
    monkeypatch.setattr(platform, "_resolve_user_name", _amock(return_value="Flavius"))

    result = await platform.fetch_channel_context(
        creds={"bot_token": "xoxb-x"}, channel_id="C1",
        limits=BackfillLimits(),
    )
    assert result is not None
    _meta, msgs = result
    assert msgs[0].author == "Flavius"
    assert msgs[0].author_id == "U063C2DB7GW"
```

If `_amock` does not already exist in this test module, add near the top:

```python
def _amock(*, return_value):
    async def _f(*a, **k):
        return return_value
    return _f
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_slack_fetch_channel_context.py::test_fetch_channel_context_populates_author_id -v`
Expected: FAIL with `AttributeError: 'RawMessage' object has no attribute 'author_id'`

- [ ] **Step 3: Add the field** — in `surogates/channels/channel_backfill.py`, the `RawMessage` dataclass:

```python
@dataclass(frozen=True)
class RawMessage:
    ts: float
    author: str
    text: str
    files: tuple[tuple[str, str], ...] = ()
    author_id: str = ""
```

- [ ] **Step 4: Populate it** — in `surogates/channels/platforms/slack.py`, the `raw.append(RawMessage(...))` block (~line 1300):

```python
                    raw.append(RawMessage(
                        ts=ts,
                        author=author,
                        text=(m.get("text") or "").strip(),
                        files=files,
                        author_id=(m.get("user") or ""),
                    ))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_slack_fetch_channel_context.py -v`
Expected: PASS (new test + existing tests)

- [ ] **Step 6: Commit**

```bash
git add surogates/channels/channel_backfill.py surogates/channels/platforms/slack.py tests/test_slack_fetch_channel_context.py
git commit -m "feat(channels): carry raw author_id on RawMessage for user filtering"
```

---

### Task 2: Pure filter/format core + parameterized header

Add the pure, I/O-free query core to `channel_backfill.py`: `parse_since`, `normalize_user`, `filter_messages_for_query`, and a `header:` parameter on `format_context_block`. Reframe the backfill default header away from "before the agent joined".

**Files:**
- Modify: `surogates/channels/channel_backfill.py` (`format_context_block` ~line 101; add new helpers)
- Test: `tests/test_channel_messages_core.py` (new)
- Test: existing backfill tests that assert the header string (find and update)

**Interfaces:**
- Produces:
  - `BACKFILL_HEADER: str` and `MESSAGES_HEADER: str` module constants
  - `def parse_since(value: str | None, *, now: float) -> float | None` — epoch cutoff, or `None` when `value` is falsy; raises `ValueError` on an unparseable value
  - `def normalize_user(value: str | None) -> str` — strips `<@…>` / leading `@`, returns bare id (or `""`)
  - `def filter_messages_for_query(messages: list[RawMessage], *, since_cutoff: float | None, user_id: str, limit: int) -> list[RawMessage]` — newest-first in, drops older than cutoff, keeps only matching `author_id` when `user_id` is set, takes newest `limit`, returns **oldest-first**
  - `def format_context_block(meta: ChannelMeta, messages: list[RawMessage], *, now: float, header: str = BACKFILL_HEADER) -> str | None`

- [ ] **Step 1: Write the failing tests** — create `tests/test_channel_messages_core.py`:

```python
import pytest

from surogates.channels.channel_backfill import (
    BACKFILL_HEADER,
    ChannelMeta,
    RawMessage,
    filter_messages_for_query,
    format_context_block,
    normalize_user,
    parse_since,
)

NOW = 1_720_000_000.0  # fixed reference epoch


def _msg(ts, uid, text="x"):
    return RawMessage(ts=ts, author=f"name-{uid}", text=text, author_id=uid)


def test_normalize_user_strips_mention_and_at():
    assert normalize_user("<@U063>") == "U063"
    assert normalize_user("@U063") == "U063"
    assert normalize_user("U063") == "U063"
    assert normalize_user(None) == ""


def test_parse_since_relative_and_iso_and_none():
    assert parse_since(None, now=NOW) is None
    assert parse_since("24h", now=NOW) == NOW - 24 * 3600
    assert parse_since("7d", now=NOW) == NOW - 7 * 86400
    # ISO date -> midnight UTC epoch of that date
    assert parse_since("2024-07-03", now=NOW) == 1_719_964_800.0


def test_parse_since_invalid_raises():
    with pytest.raises(ValueError):
        parse_since("banana", now=NOW)


def test_filter_by_user_and_limit_returns_oldest_first():
    msgs = [  # newest-first, as fetch_channel_context returns
        _msg(NOW - 10, "U1"), _msg(NOW - 20, "U2"),
        _msg(NOW - 30, "U1"), _msg(NOW - 40, "U1"),
    ]
    out = filter_messages_for_query(msgs, since_cutoff=None, user_id="U1", limit=2)
    assert [m.ts for m in out] == [NOW - 30, NOW - 10]  # newest 2 of U1, oldest-first


def test_filter_by_since_drops_older():
    msgs = [_msg(NOW - 10, "U1"), _msg(NOW - 100, "U1")]
    out = filter_messages_for_query(
        msgs, since_cutoff=NOW - 50, user_id="", limit=50)
    assert [m.ts for m in out] == [NOW - 10]


def test_format_block_uses_custom_header():
    meta = ChannelMeta(name="surogate", topic="", purpose="")
    block = format_context_block(
        meta, [_msg(NOW - 10, "U1", "hello")], now=NOW, header="[channel messages]")
    assert block.startswith("[channel messages]")
    assert "hello" in block


def test_format_block_default_header_is_not_pre_join():
    meta = ChannelMeta(name="surogate", topic="", purpose="")
    block = format_context_block(meta, [_msg(NOW - 10, "U1", "hi")], now=NOW)
    assert block.startswith(BACKFILL_HEADER)
    assert "before the agent joined" not in block
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_channel_messages_core.py -v`
Expected: FAIL with `ImportError` (helpers not defined)

- [ ] **Step 3: Implement the helpers** — in `surogates/channels/channel_backfill.py`. Add near the top after imports (ensure `from datetime import datetime, timezone` and `import re` are present):

```python
BACKFILL_HEADER = (
    "[recent channel history — snapshot; use fetch_channel_messages "
    "for more or newer messages]"
)
MESSAGES_HEADER = "[channel messages]"

_REL_SINCE = re.compile(r"^\s*(\d+)\s*([hd])\s*$", re.IGNORECASE)


def normalize_user(value: str | None) -> str:
    """Strip a Slack mention wrapper to a bare user id ('<@U063>' -> 'U063')."""
    v = (value or "").strip()
    if v.startswith("<@") and v.endswith(">"):
        v = v[2:-1].split("|", 1)[0]
    elif v.startswith("@"):
        v = v[1:]
    return v.strip()


def parse_since(value: str | None, *, now: float) -> float | None:
    """Return an epoch cutoff for *value*, or None when empty.

    Accepts relative windows ('24h', '7d') evaluated against *now*, or an ISO
    date ('2026-07-01') meaning midnight UTC on that date. Raises ValueError on
    anything else so the caller can surface a 400 rather than broaden silently.
    """
    v = (value or "").strip()
    if not v:
        return None
    m = _REL_SINCE.match(v)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        return now - n * (3600 if unit == "h" else 86400)
    try:
        d = datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"Unparseable 'since' value: {value!r}") from exc
    return d.timestamp()


def filter_messages_for_query(
    messages: list["RawMessage"], *, since_cutoff: float | None,
    user_id: str, limit: int,
) -> list["RawMessage"]:
    """Filter newest-first *messages* by since/user, take newest *limit*,
    return oldest-first for natural reading order."""
    out = []
    for m in messages:  # newest-first
        if since_cutoff is not None and m.ts < since_cutoff:
            continue
        if user_id and m.author_id != user_id:
            continue
        out.append(m)
        if len(out) >= max(1, limit):
            break
    return list(reversed(out))
```

- [ ] **Step 4: Parameterize the header** — change `format_context_block`'s signature and first line:

```python
def format_context_block(
    meta: ChannelMeta, messages: list[RawMessage], *, now: float,
    header: str = BACKFILL_HEADER,
) -> str | None:
    if not messages:
        return None
    lines = [header]
```

- [ ] **Step 5: Update existing backfill header assertions**

Run: `grep -rn "before the agent joined" tests/`
For each hit, update the expected string to `BACKFILL_HEADER` (import it) or to a substring check like `assert block.startswith("[recent channel history")`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_channel_messages_core.py tests/test_channel_backfill_core.py tests/test_channel_memory_flush.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add surogates/channels/channel_backfill.py tests/test_channel_messages_core.py tests/
git commit -m "feat(channels): channel-message query core + reframe backfill header"
```

---

### Task 3: `message_fetch.py` orchestrator

Mirror `file_fetch.py`: resolve creds from the vault, call `fetch_channel_context`, run the pure core, return a JSON-able dict. Missing config/token/None → empty result with a `note` (not an exception); invalid `since` → `ValueError` (route maps to 400).

**Files:**
- Create: `surogates/channels/message_fetch.py`
- Test: `tests/test_channel_message_fetch.py` (new)

**Interfaces:**
- Consumes: `parse_since`, `normalize_user`, `filter_messages_for_query`, `format_context_block`, `MESSAGES_HEADER`, `BackfillLimits`, `ChannelMeta` (Task 2); `resolve_channel_credentials`; `platform.descriptor.vault_refs`, `platform.fetch_channel_context`
- Produces: `async def fetch_channel_messages(*, platform, vault, session, limit: int, since: str | None, user: str | None, now: float) -> dict` returning `{"messages_block": str | None, "count": int, "channel": str, "note": str | None}`. Raises `ValueError` on bad `since`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_channel_message_fetch.py`:

```python
import types

import pytest

from surogates.channels.channel_backfill import ChannelMeta, RawMessage
from surogates.channels.message_fetch import fetch_channel_messages

NOW = 1_720_000_000.0


def _session(cfg):
    return types.SimpleNamespace(config=cfg, org_id="org-1")


def _platform(context_result):
    descriptor = types.SimpleNamespace(vault_refs=lambda identifier: {"bot_token": "bot_token"})

    async def _fetch_channel_context(*, creds, channel_id, limits):
        return context_result

    return types.SimpleNamespace(descriptor=descriptor, fetch_channel_context=_fetch_channel_context)


class _Vault:
    async def resolve_ref(self, ref, *, org_id):
        return "xoxb-token"


async def test_happy_path_filters_and_formats():
    meta = ChannelMeta(name="surogate", topic="", purpose="")
    msgs = [RawMessage(ts=NOW - 10, author="Flavius", text="hi", author_id="U1")]
    platform = _platform((meta, msgs))
    out = await fetch_channel_messages(
        platform=platform, vault=_Vault(),
        session=_session({"channel_identifier": "A1", "slack_channel_id": "C1"}),
        limit=50, since=None, user="<@U1>", now=NOW)
    assert out["count"] == 1
    assert out["channel"] == "surogate"
    assert "hi" in out["messages_block"]
    assert out["note"] is None


async def test_missing_channel_config_returns_note():
    platform = _platform((ChannelMeta("x", "", ""), []))
    out = await fetch_channel_messages(
        platform=platform, vault=_Vault(),
        session=_session({}), limit=50, since=None, user=None, now=NOW)
    assert out["count"] == 0
    assert out["messages_block"] is None
    assert "channel" in out["note"].lower()


async def test_no_bot_token_returns_note():
    class _EmptyVault:
        async def resolve_ref(self, ref, *, org_id):
            return None
    out = await fetch_channel_messages(
        platform=_platform((ChannelMeta("x", "", ""), [])), vault=_EmptyVault(),
        session=_session({"channel_identifier": "A1", "slack_channel_id": "C1"}),
        limit=50, since=None, user=None, now=NOW)
    assert out["count"] == 0
    assert "token" in out["note"].lower()


async def test_context_none_returns_note():
    out = await fetch_channel_messages(
        platform=_platform(None), vault=_Vault(),
        session=_session({"channel_identifier": "A1", "slack_channel_id": "C1"}),
        limit=50, since=None, user=None, now=NOW)
    assert out["count"] == 0
    assert out["note"]


async def test_invalid_since_raises():
    with pytest.raises(ValueError):
        await fetch_channel_messages(
            platform=_platform((ChannelMeta("x", "", ""), [])), vault=_Vault(),
            session=_session({"channel_identifier": "A1", "slack_channel_id": "C1"}),
            limit=50, since="banana", user=None, now=NOW)
```

Ensure `tests/` runs async tests (repo already uses `pytest-asyncio`; if this file needs it, add `pytestmark = pytest.mark.asyncio` at top, matching sibling test files).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_channel_message_fetch.py -v`
Expected: FAIL with `ModuleNotFoundError: surogates.channels.message_fetch`

- [ ] **Step 3: Implement the orchestrator** — create `surogates/channels/message_fetch.py`:

```python
"""On-demand Slack channel message fetch — resolve creds, pull history, format.

The privileged half of the ``fetch_channel_messages`` tool: given a channel
session it resolves the bot token from the vault, calls the Slack
``fetch_channel_context`` primitive, and runs the pure query core. The bot token
never leaves the server. Missing configuration or credentials yield an empty
result with a human-readable ``note`` rather than an error; an unparseable
``since`` raises ``ValueError`` so the route can return a 400.
"""

from __future__ import annotations

from typing import Any

from surogates.channels.channel_backfill import (
    MESSAGES_HEADER,
    BackfillLimits,
    filter_messages_for_query,
    format_context_block,
    normalize_user,
    parse_since,
)
from surogates.channels.credentials import resolve_channel_credentials

_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50


def _clamp_limit(limit: Any) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(_MAX_LIMIT, n))


def _empty(note: str, channel: str = "") -> dict:
    return {"messages_block": None, "count": 0, "channel": channel, "note": note}


async def fetch_channel_messages(
    *, platform: Any, vault: Any, session: Any,
    limit: Any, since: str | None, user: str | None, now: float,
) -> dict:
    cfg = getattr(session, "config", None) or {}
    identifier = cfg.get("channel_identifier") or ""
    channel_id = cfg.get("slack_channel_id") or ""
    if not identifier or not channel_id:
        return _empty("This session is not bound to a Slack channel.")

    since_cutoff = parse_since(since, now=now)  # may raise ValueError
    user_id = normalize_user(user)
    n = _clamp_limit(limit)

    refs = platform.descriptor.vault_refs(identifier)
    creds = await resolve_channel_credentials(
        vault=vault, kind="slack", identifier=identifier,
        org_id=str(session.org_id), refs=refs,
    )
    if not (creds or {}).get("bot_token"):
        return _empty("No Slack bot token is configured for this channel.")

    limits = BackfillLimits(max_messages=n, max_pages=1)
    result = await platform.fetch_channel_context(
        creds=creds, channel_id=channel_id, limits=limits,
    )
    if result is None:
        return _empty(
            "Could not read this channel's history (the bot may not be a "
            "member, or Slack returned an error).")

    meta, messages = result
    picked = filter_messages_for_query(
        messages, since_cutoff=since_cutoff, user_id=user_id, limit=n)
    if not picked:
        scope = f" from that user" if user_id else ""
        return _empty(f"No messages found{scope} in the requested window.",
                      channel=meta.name)
    block = format_context_block(meta, picked, now=now, header=MESSAGES_HEADER)
    return {
        "messages_block": block, "count": len(picked),
        "channel": meta.name, "note": None,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_channel_message_fetch.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add surogates/channels/message_fetch.py tests/test_channel_message_fetch.py
git commit -m "feat(channels): message_fetch orchestrator over fetch_channel_context"
```

---

### Task 4: Server route + `_resolve_session` split

Split the file route's bucket resolver so the message route can resolve a session without needing a workspace bucket, then add `POST /sessions/{session_id}/channel-messages`.

**Files:**
- Modify: `surogates/api/routes/channel_files.py`
- Test: `tests/test_channel_messages_route.py` (new)

**Interfaces:**
- Consumes: `fetch_channel_messages` (Task 3), `effective_channel_platform`, `registry`, `get_current_tenant`
- Produces: route `POST /sessions/{session_id}/channel-messages` accepting body `{"limit": int?, "since": str?, "user": str?}`, returning the Task 3 dict; refactored `async def _resolve_session(store, session_id, tenant) -> Any`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_channel_messages_route.py`. Like `tests/test_channel_files_route.py`, call the route function **directly** (no TestClient); monkeypatch `channel_files.registry.get` and `channel_files.fetch_channel_messages`, and pass a fake `request` built from `SimpleNamespace`. The repo runs pytest in `asyncio_mode=auto`, so no marker is needed.

```python
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from surogates.api.routes import channel_files
from surogates.api.routes.channel_files import _ChannelMessagesBody


class _Store:
    def __init__(self, session):
        self._session = session

    async def get_session(self, session_id):
        return self._session


def _session(*, channel="slack", config=None):
    return SimpleNamespace(
        id=uuid4(), org_id=uuid4(), channel=channel,
        config=config if config is not None
        else {"channel_identifier": "T1", "slack_channel_id": "C1"},
    )


class _Tenant:
    def __init__(self, owns=True):
        self._owns = owns

    def owns_session(self, org_id, session_id):
        return self._owns


def _request(session):
    state = SimpleNamespace(session_store=_Store(session), credential_vault=object())
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def test_messages_route_happy_path(monkeypatch):
    session = _session(channel="slack")
    monkeypatch.setattr(channel_files.registry, "get", lambda kind: object())

    async def _fake(**kwargs):
        return {"messages_block": "…hi…", "count": 1, "channel": "surogate", "note": None}

    monkeypatch.setattr(channel_files, "fetch_channel_messages", _fake)
    out = await channel_files.fetch_channel_messages_route(
        session_id=session.id, body=_ChannelMessagesBody(limit=10),
        request=_request(session), tenant=_Tenant())
    assert out["count"] == 1
    assert out["messages_block"]


async def test_messages_route_non_slack_400():
    session = _session(channel="web")
    with pytest.raises(HTTPException) as ei:
        await channel_files.fetch_channel_messages_route(
            session_id=session.id, body=_ChannelMessagesBody(),
            request=_request(session), tenant=_Tenant())
    assert ei.value.status_code == 400


async def test_messages_route_foreign_session_404():
    session = _session(channel="slack")
    with pytest.raises(HTTPException) as ei:
        await channel_files.fetch_channel_messages_route(
            session_id=session.id, body=_ChannelMessagesBody(),
            request=_request(session), tenant=_Tenant(owns=False))
    assert ei.value.status_code == 404


async def test_messages_route_invalid_since_400(monkeypatch):
    session = _session(channel="slack")
    monkeypatch.setattr(channel_files.registry, "get", lambda kind: object())

    async def _raise(**kwargs):
        raise ValueError("bad since")

    monkeypatch.setattr(channel_files, "fetch_channel_messages", _raise)
    with pytest.raises(HTTPException) as ei:
        await channel_files.fetch_channel_messages_route(
            session_id=session.id, body=_ChannelMessagesBody(since="banana"),
            request=_request(session), tenant=_Tenant())
    assert ei.value.status_code == 400


async def test_messages_route_missing_config_empty(monkeypatch):
    session = _session(channel="slack", config={})
    monkeypatch.setattr(channel_files.registry, "get", lambda kind: object())

    async def _fake(**kwargs):
        return {"messages_block": None, "count": 0, "channel": "",
                "note": "This session is not bound to a Slack channel."}

    monkeypatch.setattr(channel_files, "fetch_channel_messages", _fake)
    out = await channel_files.fetch_channel_messages_route(
        session_id=session.id, body=_ChannelMessagesBody(),
        request=_request(session), tenant=_Tenant())
    assert out["count"] == 0
    assert out["note"]
```

Confirm `effective_channel_platform(_session(channel="web"))` returns a non-`"slack"` value (it reads `session.channel` for non-ambient sessions); if the helper needs other fields, add them to `_session`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_channel_messages_route.py -v`
Expected: FAIL (route returns 404 / not found)

- [ ] **Step 3: Refactor the resolver** — in `surogates/api/routes/channel_files.py`, replace `_resolve_session_bucket` with a split:

```python
async def _resolve_session(
    store: SessionStore, session_id: UUID, tenant: TenantContext,
) -> Any:
    try:
        session = await store.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    if not tenant.owns_session(session.org_id, session_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    return session


async def _resolve_session_bucket(
    store: SessionStore, session_id: UUID, tenant: TenantContext,
) -> tuple[Any, str]:
    session = await _resolve_session(store, session_id, tenant)
    bucket = session.config.get("storage_bucket")
    if not bucket:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Session {session_id} has no agent bucket.",
        )
    return session, bucket
```

- [ ] **Step 4: Add the route** — in the same file. Add imports at top:

```python
import time
from pydantic import BaseModel
from surogates.channels.message_fetch import fetch_channel_messages
```

Add the request model and route:

```python
class _ChannelMessagesBody(BaseModel):
    limit: int | None = None
    since: str | None = None
    user: str | None = None


@router.post("/sessions/{session_id}/channel-messages")
async def fetch_channel_messages_route(
    session_id: UUID,
    body: _ChannelMessagesBody,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict:
    """Read recent messages from this session's Slack channel."""
    store = _get_session_store(request)
    session = await _resolve_session(store, session_id, tenant)

    channel = effective_channel_platform(session)
    if channel != "slack":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Channel-message fetch is only supported for Slack sessions.",
        )

    platform = registry.get("slack")
    if platform is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Slack platform is not available.",
        )

    try:
        return await fetch_channel_messages(
            platform=platform,
            vault=request.app.state.credential_vault,
            session=session,
            limit=body.limit if body.limit is not None else 50,
            since=body.since,
            user=body.user,
            now=time.time(),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_channel_messages_route.py tests/test_channel_files_route.py -v`
Expected: PASS (new route + file route unaffected by the split)

- [ ] **Step 6: Commit**

```bash
git add surogates/api/routes/channel_files.py tests/test_channel_messages_route.py
git commit -m "feat(channels): add channel-messages read route"
```

---

### Task 5: Harness API client method

Add `fetch_channel_messages` to the session-scoped API client, mirroring `fetch_channel_file`.

**Files:**
- Modify: `surogates/harness/api_client.py` (after `fetch_channel_file`, ~line 355)
- Test: `tests/test_api_client_channel_message.py` (new)

**Interfaces:**
- Consumes: `self._session_id`; `self._post(path, body=None)` — `_post`'s body is a **positional** dict (it sends `json=body` internally). Ctor is `HarnessAPIClient(base_url=..., token=..., session_id=...)`.
- Produces: `async def fetch_channel_messages(self, *, limit: int | None = None, since: str | None = None, user: str | None = None) -> str` returning a JSON string `{"success": True, ...}` or the standard error envelope.

- [ ] **Step 1: Write the failing tests** — create `tests/test_api_client_channel_message.py`, mirroring `tests/test_api_client_channel_file.py`:

```python
import json

from surogates.harness.api_client import HarnessAPIClient


def _client(session_id="11111111-1111-1111-1111-111111111111"):
    return HarnessAPIClient(base_url="http://api", token="t", session_id=session_id)


async def test_requires_session_id():
    c = HarnessAPIClient(base_url="http://api", token="t", session_id=None)
    out = json.loads(await c.fetch_channel_messages(limit=10))
    assert out["success"] is False


async def test_posts_to_channel_messages_path(monkeypatch):
    c = _client()
    captured = {}

    async def _fake_post(path, body=None):
        captured["path"] = path
        captured["body"] = body
        return {"count": 2, "messages_block": "…", "channel": "surogate", "note": None}

    monkeypatch.setattr(c, "_post", _fake_post)
    out = json.loads(await c.fetch_channel_messages(limit=10, since="7d", user="<@U1>"))
    assert out["success"] is True
    assert captured["path"] == (
        "/v1/sessions/11111111-1111-1111-1111-111111111111/channel-messages")
    assert captured["body"] == {"limit": 10, "since": "7d", "user": "<@U1>"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api_client_channel_message.py -v`
Expected: FAIL with `AttributeError: ... has no attribute 'fetch_channel_messages'`

- [ ] **Step 3: Implement the method** — in `surogates/harness/api_client.py`, right after `fetch_channel_file`:

```python
    async def fetch_channel_messages(
        self, *, limit: int | None = None, since: str | None = None,
        user: str | None = None,
    ) -> str:
        """Read recent messages from this session's Slack channel.

        Requires a session-scoped client. Returns a JSON string with the
        formatted ``messages_block`` (and ``count``/``channel``/``note``), or
        the standard error envelope.
        """
        if self._session_id is None:
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        "Channel-message fetch requires a session-scoped API "
                        "client; session_id is not set."
                    ),
                },
                ensure_ascii=False,
            )
        try:
            data = await self._post(
                f"/v1/sessions/{self._session_id}/channel-messages",
                {"limit": limit, "since": since, "user": user},
            )
            return json.dumps({"success": True, **data}, ensure_ascii=False)
        except httpx.HTTPStatusError as exc:
            return _error_response(exc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_api_client_channel_message.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/api_client.py tests/test_api_client_channel_message.py
git commit -m "feat(harness): api_client.fetch_channel_messages"
```

---

### Task 6: Builtin tool + runtime/router registration

Create the tool, register its module in the runtime, and route it to the harness (matching `fetch_channel_file`). Unlisted tools default to the sandbox executor and fail as "Unknown tool", so the `TOOL_LOCATIONS` entry is required.

**Files:**
- Create: `surogates/tools/builtin/channel_messages.py`
- Modify: `surogates/tools/runtime.py` (import list ~line 50-107)
- Modify: `surogates/tools/router.py:121` (`TOOL_LOCATIONS`)
- Test: `tests/test_fetch_channel_messages_tool.py` (new)
- Test: check/adjust any native-channel-tool enumeration (`tests/harness/test_channel_composio_filter.py`)

**Interfaces:**
- Consumes: `ToolRegistry`, `ToolSchema`, `api_client.fetch_channel_messages` (Task 5)
- Produces: tool name `fetch_channel_messages`, `toolset="channels"`; `register(registry)`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_fetch_channel_messages_tool.py`, mirroring `tests/test_fetch_channel_file_tool.py`:

```python
import json

from surogates.tools.builtin.channel_messages import _fetch_channel_messages_handler
from surogates.tools.router import TOOL_LOCATIONS, ToolLocation


async def test_missing_api_client_returns_error():
    out = json.loads(await _fetch_channel_messages_handler({"limit": 10}))
    assert out["success"] is False


async def test_delegates_with_parsed_args():
    calls = {}

    class _Client:
        async def fetch_channel_messages(self, *, limit, since, user):
            calls.update(limit=limit, since=since, user=user)
            return json.dumps({"success": True, "count": 0})

    out = await _fetch_channel_messages_handler(
        {"limit": "25", "since": "7d", "user": "<@U1>"}, api_client=_Client())
    assert json.loads(out)["success"] is True
    assert calls == {"limit": 25, "since": "7d", "user": "<@U1>"}


def test_tool_routed_to_harness():
    assert TOOL_LOCATIONS["fetch_channel_messages"] == ToolLocation.HARNESS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fetch_channel_messages_tool.py -v`
Expected: FAIL with `ModuleNotFoundError` / `KeyError`

- [ ] **Step 3: Create the tool** — `surogates/tools/builtin/channel_messages.py`:

```python
"""The ``fetch_channel_messages`` builtin tool.

Lets a Slack-channel agent read recent messages posted in its own channel,
optionally narrowed by a time window or a specific user. Thin delegate to the
session-scoped harness API client; the privileged history fetch runs
server-side with the channel's bot token.
"""

from __future__ import annotations

import json
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema

FETCH_CHANNEL_MESSAGES_SCHEMA = ToolSchema(
    name="fetch_channel_messages",
    description=(
        "Read recent messages posted in this Slack channel (including messages "
        "from other users). Use this to catch up on the conversation or to see "
        "what a specific person said. Optionally narrow by 'since' (e.g. '24h', "
        "'7d', or a date like '2026-07-01') and by 'user' (a Slack user id such "
        "as 'U063C2DB7GW' or a mention like '<@U063C2DB7GW>'). Returns messages "
        "oldest-to-newest. Only messages in this channel are accessible."
    ),
    parameters={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "How many recent messages to return (default 50, max 200).",
            },
            "since": {
                "type": "string",
                "description": "Only messages newer than this: '24h', '7d', or a date '2026-07-01'.",
            },
            "user": {
                "type": "string",
                "description": "Only messages from this Slack user id or mention (e.g. '<@U063C2DB7GW>').",
            },
        },
        "required": [],
    },
)


def _coerce_limit(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _fetch_channel_messages_handler(
    arguments: dict[str, Any], **kwargs: Any,
) -> str:
    api_client = kwargs.get("api_client")
    if api_client is None:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "Channel-message fetch requires a session-scoped API client."
                ),
            },
            ensure_ascii=False,
        )
    return await api_client.fetch_channel_messages(
        limit=_coerce_limit(arguments.get("limit")),
        since=(arguments.get("since") or None),
        user=(arguments.get("user") or None),
    )


def register(registry: ToolRegistry) -> None:
    """Register the fetch_channel_messages tool."""
    registry.register(
        name="fetch_channel_messages",
        schema=FETCH_CHANNEL_MESSAGES_SCHEMA,
        handler=_fetch_channel_messages_handler,
        toolset="channels",
    )
```

- [ ] **Step 4: Register in the runtime** — in `surogates/tools/runtime.py`, add `channel_messages` to the `from surogates.tools.builtin import (...)` block (near `channel_files`) and to the module tuple that gets iterated (near line 107):

```python
            channel_files,     # fetch_channel_file (pull a shared channel file)
            channel_messages,  # fetch_channel_messages (read recent channel messages)
```

- [ ] **Step 5: Route to the harness** — in `surogates/tools/router.py`, right after the `"fetch_channel_file"` entry (line 121):

```python
    "fetch_channel_file": ToolLocation.HARNESS,
    # Channel message read — same rationale: needs the session-scoped API
    # client to call the ops server; no sandbox isolation.
    "fetch_channel_messages": ToolLocation.HARNESS,
```

- [ ] **Step 6: Check the Composio native-tool filter** — run `pytest tests/harness/test_channel_composio_filter.py -v`. If it enumerates the exact set of native channel tools and now fails because `fetch_channel_messages` is present, add the new tool name to that expected set. If it passes, leave it.

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_fetch_channel_messages_tool.py tests/harness/test_channel_composio_filter.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add surogates/tools/builtin/channel_messages.py surogates/tools/runtime.py surogates/tools/router.py tests/test_fetch_channel_messages_tool.py
git commit -m "feat(tools): fetch_channel_messages builtin + harness routing"
```

---

### Task 7: Full-suite regression + branch wrap-up

**Files:** none (verification only)

- [ ] **Step 1: Run the channel + tool test subset**

Run: `pytest tests/ -k "channel or api_client or router or backfill or slack" -q`
Expected: PASS (no regressions)

- [ ] **Step 2: Run the full suite**

Run: `pytest tests/ -q`
Expected: PASS (or only pre-existing unrelated failures — confirm by comparing against `master` if anything fails)

- [ ] **Step 3: Push and open a PR** (only if the user asks to push)

```bash
git push -u origin feat/fetch-channel-messages-tool
gh pr create --base master --title "feat(channels): fetch_channel_messages live tool" \
  --body "Adds an on-demand tool for Slack-channel agents to read recent channel messages (limit/since/user), backed by the existing fetch_channel_context primitive, and reframes the backfill snapshot header. See docs/superpowers/specs/2026-07-03-fetch-channel-messages-tool-design.md."
```
