"""Tests for the shared inbound message pipeline (surogates/channels/inbound.py).

Covers the 7 gating scenarios extracted from SlackAdapter._handle_slack_message:
  a. DM → session created, USER_MESSAGE emitted, session enqueued.
  b. Channel non-mention + require_mention + follow on → firehose append, no session/enqueue.
  c. Channel mention → processed (session + USER_MESSAGE + enqueue).
  d. Free-response channel → processed without mention.
  e. Unpaired user (identity=None) → pairing prompt, no session.
  f. Dedup by ts → second identical message dropped.
  g. Replay-stable → same inputs produce the same session_key call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from surogates.channels.inbound import (
    ChannelInboundPipeline,
    InboundMessage,
    InboundOutcome,
    PipelineDeps,
)
from surogates.channels.source import SessionSource, build_session_key
from surogates.session.events import EventType


# ---------------------------------------------------------------------------
# Fake helpers
# ---------------------------------------------------------------------------

# The messaging user's home org. Deliberately DIFFERENT from the agent's org
# (AGENT_ORG_ID) so tests can assert the session is owned by the agent's org
# (routing.org_id), not the messaging user's org (identity.org_id).
ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
AGENT_ORG_ID = UUID("00000000-0000-0000-0000-0000000000aa")
AGENT_ID = "agent-test"


@dataclass
class _FakeIdentity:
    user_id: UUID = USER_ID
    org_id: UUID = ORG_ID


class _FakeSessionStore:
    def __init__(self) -> None:
        self.events: list[tuple[UUID, EventType, dict]] = []
        self._next_session_id: UUID = uuid4()

    async def emit_event(
        self, session_id: UUID, event_type: EventType, data: dict,
    ) -> None:
        self.events.append((session_id, event_type, data))


class _FakeRedis:
    """Minimal Redis stub: supports zadd, set, get, exists, rpush, ltrim."""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.kv: dict[str, Any] = {}
        self.lists: dict[str, list[str]] = {}

    async def zadd(self, key: str, mapping: dict) -> None:
        if key not in self.zsets:
            self.zsets[key] = {}
        self.zsets[key].update(mapping)

    async def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self.kv[key] = value

    async def get(self, key: str) -> Any:
        return self.kv.get(key)

    async def exists(self, key: str) -> int:
        return 1 if key in self.kv else 0

    async def rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    async def ltrim(self, key: str, start: int, stop: int) -> None:
        lst = self.lists.get(key, [])
        self.lists[key] = lst[start : stop + 1 if stop >= 0 else None]


class _FakeState:
    """SlackAdapterState-compatible fake (Redis-less for tests)."""

    def __init__(self) -> None:
        self._sessions: dict[str, str] = {}
        self._mentioned: set[str] = set()
        self._botmsg: set[str] = set()

    async def remember_session(self, session_key: str, session_id: str) -> None:
        self._sessions[session_key] = session_id

    async def get_session(self, session_key: str) -> str | None:
        return self._sessions.get(session_key)

    async def mark_mentioned_thread(self, thread_ts: str) -> None:
        self._mentioned.add(thread_ts)

    async def is_mentioned_thread(self, thread_ts: str) -> bool:
        return thread_ts in self._mentioned

    async def mark_bot_message(self, ts: str) -> None:
        self._botmsg.add(ts)

    async def is_bot_message(self, ts: str) -> bool:
        return ts in self._botmsg


class _FakePairing:
    def __init__(self, code: str = "AAAA-BBBB") -> None:
        self.code = code
        self.calls: list[tuple[str, str]] = []

    async def create(
        self, platform: str, platform_user_id: str, platform_meta: dict | None = None,
    ) -> str | None:
        self.calls.append((platform, platform_user_id))
        return self.code


# ---------------------------------------------------------------------------
# Fixture factories
# ---------------------------------------------------------------------------

def _make_msg(
    *,
    is_dm: bool = False,
    is_mention: bool = False,
    text: str = "hello",
    platform_user_id: str = "U1",
    identifier: str = "C1",
    thread_key: str | None = None,
    ts: str = "100.0",
    media_urls: list[str] | None = None,
    media_types: list[str] | None = None,
    kind: str = "text",
) -> InboundMessage:
    return InboundMessage(
        kind=kind,
        identifier=identifier,
        thread_key=thread_key,
        platform_user_id=platform_user_id,
        user_name="Alice",
        text=text,
        media_urls=media_urls or [],
        media_types=media_types or [],
        is_dm=is_dm,
        is_mention=is_mention,
        ts=ts,
        source={},
    )


def _make_routing() -> Any:
    """Minimal routing object with org_id + agent_id.

    ``org_id`` is the *agent's* org (``AGENT_ORG_ID``), distinct from the
    messaging user's org (``identity.org_id`` == ``ORG_ID``).
    """

    @dataclass
    class _Routing:
        org_id: str = str(AGENT_ORG_ID)
        agent_id: str = AGENT_ID
        platform: str = "slack"

    return _Routing()


def _make_config(
    *,
    require_mention: bool = True,
    free_response_channels: set[str] | None = None,
    allow_bots: str = "none",
    follow_enabled: bool = False,
) -> dict:
    return {
        "require_mention": require_mention,
        "free_response_channels": free_response_channels or set(),
        "allow_bots": allow_bots,
        "follow_enabled": follow_enabled,
    }


SESSION_ID = UUID("aaaaaaaa-0000-0000-0000-000000000001")


def _make_deps(
    *,
    identity: _FakeIdentity | None = _FakeIdentity(),
    session_id: UUID = SESSION_ID,
    pairing_sender: Any = None,
) -> PipelineDeps:
    store = _FakeSessionStore()
    store._next_session_id = session_id
    redis = _FakeRedis()
    state = _FakeState()
    pairing = _FakePairing()

    firehose_calls: list[dict] = []

    async def firehose_append(
        redis_: Any,
        *,
        agent_id: str,
        channel_id: str,
        observation: dict,
        maxlen: int = 1000,
    ) -> None:
        firehose_calls.append(
            {"agent_id": agent_id, "channel_id": channel_id, "observation": observation},
        )

    sessions_created: list[dict] = []

    async def get_or_create(
        session_store: Any,
        redis_: Any,
        *,
        session_key: str,
        user_id: UUID,
        org_id: UUID,
        agent_id: str,
        channel: str,
        config: dict,
        session_factory: Any,
        model: str = "",
    ) -> UUID:
        sessions_created.append(
            {"session_key": session_key, "user_id": user_id, "org_id": org_id},
        )
        return session_id

    enqueued: list[dict] = []

    async def enqueue(redis_: Any, *, org_id: str, agent_id: str, session_id: Any) -> None:
        enqueued.append({"org_id": org_id, "agent_id": agent_id, "session_id": session_id})

    async def resolve_id(sf: Any, platform: str, platform_user_id: str):
        return identity

    sender_calls: list[tuple] = []

    async def _default_sender(platform_user_id: str, user_name: str, code: str) -> None:
        sender_calls.append((platform_user_id, user_name, code))

    deps = PipelineDeps(
        session_store=store,
        redis=redis,
        state=state,
        pairing=pairing,
        firehose_append=firehose_append,
        get_or_create_session=get_or_create,
        enqueue_session=enqueue,
        resolve_identity=resolve_id,
        session_factory=None,
        pairing_sender=pairing_sender or _default_sender,
    )
    # Expose internals for assertions.
    deps._firehose_calls = firehose_calls  # type: ignore[attr-defined]
    deps._sessions_created = sessions_created  # type: ignore[attr-defined]
    deps._enqueued = enqueued  # type: ignore[attr-defined]
    deps._sender_calls = sender_calls  # type: ignore[attr-defined]
    return deps


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dm_is_processed():
    """(a) DM → session + USER_MESSAGE + enqueue.

    Also pins multi-tenancy: the session is owned by the agent's org
    (routing.org_id == AGENT_ORG_ID), NOT the messaging user's org
    (identity.org_id == ORG_ID). The user is still identity.user_id.
    """
    msg = _make_msg(is_dm=True, ts="1.0")
    routing = _make_routing()
    config = _make_config()
    deps = _make_deps()

    pipeline = ChannelInboundPipeline()
    result = await pipeline.handle(msg, routing=routing, config=config, deps=deps)

    assert result == InboundOutcome.PROCESSED
    assert deps._sessions_created  # session was created/looked up
    assert deps._enqueued
    assert any(e[1] == EventType.USER_MESSAGE for e in deps.session_store.events)

    # Session ownership: agent's org, not the messaging user's org.
    assert AGENT_ORG_ID != ORG_ID  # guard: the divergence is real
    assert deps._sessions_created[0]["org_id"] == str(AGENT_ORG_ID)
    assert deps._sessions_created[0]["user_id"] == USER_ID
    assert deps._enqueued[0]["org_id"] == str(AGENT_ORG_ID)


@pytest.mark.asyncio
async def test_b_channel_non_mention_require_mention_follow_on_firehoses():
    """(b) channel non-mention + require_mention + follow → firehose, no session/enqueue."""
    msg = _make_msg(is_dm=False, is_mention=False, ts="2.0", identifier="C99")
    routing = _make_routing()
    config = _make_config(require_mention=True, follow_enabled=True)
    deps = _make_deps()

    pipeline = ChannelInboundPipeline()
    result = await pipeline.handle(msg, routing=routing, config=config, deps=deps)

    assert result == InboundOutcome.FIREHOSED
    assert deps._firehose_calls, "should have appended a firehose observation"
    assert not deps._sessions_created, "must NOT create a session"
    assert not deps._enqueued, "must NOT enqueue"
    assert not deps.session_store.events, "must NOT emit USER_MESSAGE"


@pytest.mark.asyncio
async def test_b_channel_non_mention_require_mention_follow_off_drops():
    """Non-mention + require_mention + follow off → DROPPED, no firehose."""
    msg = _make_msg(is_dm=False, is_mention=False, ts="3.0", identifier="C99")
    routing = _make_routing()
    config = _make_config(require_mention=True, follow_enabled=False)
    deps = _make_deps()

    pipeline = ChannelInboundPipeline()
    result = await pipeline.handle(msg, routing=routing, config=config, deps=deps)

    assert result == InboundOutcome.DROPPED
    assert not deps._firehose_calls
    assert not deps._sessions_created
    assert not deps._enqueued


@pytest.mark.asyncio
async def test_empty_non_mention_channel_drops_not_firehosed():
    """Empty non-mention channel msg → DROPPED, never a firehose observation.

    Even with follow on, an empty body (no text, no media) must drop before
    the firehose branch.
    """
    msg = _make_msg(
        is_dm=False, is_mention=False, ts="3.5", identifier="C99",
        text="", media_urls=[],
    )
    routing = _make_routing()
    config = _make_config(require_mention=True, follow_enabled=True)
    deps = _make_deps()

    pipeline = ChannelInboundPipeline()
    result = await pipeline.handle(msg, routing=routing, config=config, deps=deps)

    assert result == InboundOutcome.DROPPED
    assert not deps._firehose_calls, "empty message must NOT be firehosed"
    assert not deps._sessions_created
    assert not deps._enqueued


@pytest.mark.asyncio
async def test_c_channel_mention_is_processed():
    """(c) Channel mention → processed."""
    msg = _make_msg(is_dm=False, is_mention=True, ts="4.0")
    routing = _make_routing()
    config = _make_config(require_mention=True)
    deps = _make_deps()

    pipeline = ChannelInboundPipeline()
    result = await pipeline.handle(msg, routing=routing, config=config, deps=deps)

    assert result == InboundOutcome.PROCESSED
    assert deps._sessions_created
    assert deps._enqueued
    assert any(e[1] == EventType.USER_MESSAGE for e in deps.session_store.events)


@pytest.mark.asyncio
async def test_d_free_response_channel_skips_mention_gate():
    """(d) Free-response channel → processed even without mention."""
    msg = _make_msg(is_dm=False, is_mention=False, ts="5.0", identifier="C-FREE")
    routing = _make_routing()
    config = _make_config(require_mention=True, free_response_channels={"C-FREE"})
    deps = _make_deps()

    pipeline = ChannelInboundPipeline()
    result = await pipeline.handle(msg, routing=routing, config=config, deps=deps)

    assert result == InboundOutcome.PROCESSED
    assert deps._sessions_created
    assert deps._enqueued


@pytest.mark.asyncio
async def test_e_unpaired_user_sends_pairing_prompt_no_session():
    """(e) Identity not found → pairing prompt, no session."""
    msg = _make_msg(is_dm=True, ts="6.0", platform_user_id="U_UNKNOWN")
    routing = _make_routing()
    config = _make_config()
    deps = _make_deps(identity=None)  # <-- no identity

    pipeline = ChannelInboundPipeline()
    result = await pipeline.handle(msg, routing=routing, config=config, deps=deps)

    assert result == InboundOutcome.PAIRING_PROMPTED
    assert deps._sender_calls, "pairing prompt should have been sent"
    assert not deps._sessions_created, "no session for unpaired user"
    assert not deps._enqueued


@pytest.mark.asyncio
async def test_f_dedup_drops_duplicate_ts():
    """(f) Identical ts → second call dropped immediately."""
    msg = _make_msg(is_dm=True, ts="7.0")
    routing = _make_routing()
    config = _make_config()
    deps = _make_deps()

    pipeline = ChannelInboundPipeline()

    result1 = await pipeline.handle(msg, routing=routing, config=config, deps=deps)
    result2 = await pipeline.handle(msg, routing=routing, config=config, deps=deps)

    assert result1 == InboundOutcome.PROCESSED
    assert result2 == InboundOutcome.DROPPED
    # session should only have been created once
    assert len(deps._sessions_created) == 1
    assert len(deps._enqueued) == 1


@pytest.mark.asyncio
async def test_g_replay_stable_same_session_key():
    """(g) Replay: identical inputs produce the same session_key call."""
    msg = _make_msg(is_dm=True, ts="8.0", platform_user_id="U_REPLAY")
    routing = _make_routing()
    config = _make_config()
    deps = _make_deps()

    # Use separate pipelines to avoid dedup filter.
    pipeline_a = ChannelInboundPipeline()
    pipeline_b = ChannelInboundPipeline()

    await pipeline_a.handle(msg, routing=routing, config=config, deps=deps)
    key_first = deps._sessions_created[0]["session_key"]

    msg2 = _make_msg(is_dm=True, ts="9.0", platform_user_id="U_REPLAY")  # same user, different ts
    await pipeline_b.handle(msg2, routing=routing, config=config, deps=deps)
    key_second = deps._sessions_created[1]["session_key"]

    # Same user+channel → same routing key (replay-stable / idempotent key derivation).
    assert key_first == key_second


@pytest.mark.asyncio
async def test_require_mention_false_processes_without_mention():
    """require_mention=False in channel chat → processed without @mention."""
    msg = _make_msg(is_dm=False, is_mention=False, ts="10.0")
    routing = _make_routing()
    config = _make_config(require_mention=False)
    deps = _make_deps()

    pipeline = ChannelInboundPipeline()
    result = await pipeline.handle(msg, routing=routing, config=config, deps=deps)

    assert result == InboundOutcome.PROCESSED


@pytest.mark.asyncio
async def test_active_thread_session_bypasses_mention_gate():
    """Thread reply with existing session → processed even without mention."""
    THREAD_KEY = "200.0"
    msg = _make_msg(
        is_dm=False, is_mention=False, ts="11.0", thread_key=THREAD_KEY,
    )
    routing = _make_routing()
    config = _make_config(require_mention=True)
    deps = _make_deps()

    # Pre-seed the state so the thread has a known session. Derive the key the
    # same way the pipeline does, so the seed and lookup can't drift apart.
    seed_key = build_session_key(
        SessionSource(
            platform=routing.platform,
            chat_id=msg.identifier,
            chat_type="group",
            user_id=msg.platform_user_id,
            thread_id=THREAD_KEY,
        ),
    )
    await deps.state.remember_session(seed_key, "sess-existing")

    pipeline = ChannelInboundPipeline()
    result = await pipeline.handle(msg, routing=routing, config=config, deps=deps)

    assert result == InboundOutcome.PROCESSED


@pytest.mark.asyncio
async def test_bot_thread_reply_bypasses_mention_gate():
    """Non-mention thread reply whose root the bot authored → processed."""
    THREAD_KEY = "250.0"
    msg = _make_msg(
        is_dm=False, is_mention=False, ts="11.5", thread_key=THREAD_KEY,
    )
    routing = _make_routing()
    config = _make_config(require_mention=True)
    deps = _make_deps()

    # The bot authored the thread root.
    await deps.state.mark_bot_message(THREAD_KEY)

    pipeline = ChannelInboundPipeline()
    result = await pipeline.handle(msg, routing=routing, config=config, deps=deps)

    assert result == InboundOutcome.PROCESSED
    assert deps._sessions_created
    assert deps._enqueued


@pytest.mark.asyncio
async def test_mentioned_thread_bypasses_mention_gate():
    """Thread reply in a previously-mentioned thread → processed."""
    THREAD_KEY = "300.0"
    msg = _make_msg(
        is_dm=False, is_mention=False, ts="12.0", thread_key=THREAD_KEY,
    )
    routing = _make_routing()
    config = _make_config(require_mention=True)
    deps = _make_deps()

    await deps.state.mark_mentioned_thread(THREAD_KEY)

    pipeline = ChannelInboundPipeline()
    result = await pipeline.handle(msg, routing=routing, config=config, deps=deps)

    assert result == InboundOutcome.PROCESSED


@pytest.mark.asyncio
async def test_mention_marks_thread():
    """A mention in a thread → marks the thread for future replies."""
    THREAD_KEY = "400.0"
    msg = _make_msg(is_dm=False, is_mention=True, ts="13.0", thread_key=THREAD_KEY)
    routing = _make_routing()
    config = _make_config(require_mention=True)
    deps = _make_deps()

    pipeline = ChannelInboundPipeline()
    await pipeline.handle(msg, routing=routing, config=config, deps=deps)

    assert await deps.state.is_mentioned_thread(THREAD_KEY)


@pytest.mark.asyncio
async def test_user_message_event_content_matches_text():
    """USER_MESSAGE event data carries the normalized text."""
    msg = _make_msg(is_dm=True, ts="14.0", text="What is the plan?")
    routing = _make_routing()
    config = _make_config()
    deps = _make_deps()

    pipeline = ChannelInboundPipeline()
    await pipeline.handle(msg, routing=routing, config=config, deps=deps)

    ev = next(e for e in deps.session_store.events if e[1] == EventType.USER_MESSAGE)
    assert ev[2]["content"] == "What is the plan?"
