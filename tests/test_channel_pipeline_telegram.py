"""Pipeline behavior added for Telegram: mention patterns, free-text answers
to pending input, reply-to threading state, and platform-agnostic
attachment ingest."""

import dataclasses
from unittest.mock import AsyncMock

from surogates.channels.inbound import (
    ChannelInboundPipeline,
    InboundFileRef,
    InboundOutcome,
)
from surogates.session.events import EventType

from tests.test_channel_pipeline import (
    SESSION_ID,
    _make_config,
    _make_deps,
    _make_msg,
    _make_routing,
    _Routing,
)


def _telegram_routing() -> _Routing:
    return _Routing(platform="telegram", identifier="@bot")


class _ConfigStore:
    """Session-store fake that records config-key writes."""

    def __init__(self, existing_config: dict | None = None) -> None:
        self.events = []
        self.config_writes: list[tuple[str, object]] = []
        self._config = dict(existing_config or {})

    async def emit_event(self, session_id, event_type, data) -> None:
        self.events.append((session_id, event_type, data))

    async def update_session_config_key(self, session_id, key, value) -> None:
        self.config_writes.append((key, value))
        self._config[key] = value

    async def get_session(self, session_id):
        return dataclasses.make_dataclass("S", ["config"])(config=dict(self._config))


async def test_mention_pattern_grants_processing():
    deps = _make_deps()
    msg = _make_msg(text="hey libra, run the report", is_mention=False)
    result = await ChannelInboundPipeline().handle(
        msg,
        routing=_make_routing(),
        config={**_make_config(require_mention=True), "mention_patterns": "libra, other"},
        deps=deps,
    )
    assert result == InboundOutcome.PROCESSED


async def test_mention_pattern_with_punctuation_matches():
    """Patterns starting with non-word chars ("@bot") must still match —
    a plain \\b boundary would silently never fire for them."""
    deps = _make_deps()
    msg = _make_msg(text="hey @libra run the report", is_mention=False)
    result = await ChannelInboundPipeline().handle(
        msg,
        routing=_make_routing(),
        config={**_make_config(require_mention=True), "mention_patterns": "@libra"},
        deps=deps,
    )
    assert result == InboundOutcome.PROCESSED


async def test_mention_pattern_miss_still_gated():
    deps = _make_deps()
    msg = _make_msg(text="unrelated chatter", is_mention=False)
    result = await ChannelInboundPipeline().handle(
        msg,
        routing=_make_routing(),
        config={**_make_config(require_mention=True), "mention_patterns": "libra"},
        deps=deps,
    )
    assert result == InboundOutcome.DROPPED


async def test_telegram_plain_reply_resolves_pending_input(monkeypatch):
    deps = _make_deps()
    nudges = []

    async def pending_input(session_id):
        return {
            "tool_call_id": "tc9",
            "questions": [{"prompt": "Deploy?", "choices": [{"label": "Yes"}, {"label": "No"}]}],
            "context": "",
        }

    async def input_nudge(session_id, msg, text):
        nudges.append(text)

    deps.pending_input = pending_input
    deps.input_nudge = input_nudge

    resolve = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "surogates.session.interactive_input.resolve_input_response", resolve,
    )

    msg = _make_msg(is_dm=True, text="yes", ts="900.0")
    result = await ChannelInboundPipeline().handle(
        msg, routing=_telegram_routing(), config=_make_config(), deps=deps,
    )

    assert result == InboundOutcome.DROPPED
    resolve.assert_awaited_once()
    kwargs = resolve.await_args.kwargs
    assert kwargs["session_id"] == SESSION_ID
    assert kwargs["tool_call_id"] == "tc9"
    assert kwargs["responses"][0]["answer"] == "Yes"
    assert nudges and "Got it" in nudges[0]
    assert not any(t == EventType.USER_MESSAGE for _, t, _ in deps.session_store.events)


async def test_telegram_reply_to_mode_all_tracks_latest():
    deps = _make_deps()
    store = _ConfigStore()
    deps.session_store = store

    base = _make_msg(text="first", ts="901.0")
    msg = dataclasses.replace(base, source={"message_id": 11})
    await ChannelInboundPipeline().handle(
        msg,
        routing=_telegram_routing(),
        config={**_make_config(require_mention=False), "reply_to_mode": "all"},
        deps=deps,
    )
    msg2 = dataclasses.replace(
        _make_msg(text="second", ts="902.0"), source={"message_id": 12},
    )
    await ChannelInboundPipeline().handle(
        msg2,
        routing=_telegram_routing(),
        config={**_make_config(require_mention=False), "reply_to_mode": "all"},
        deps=deps,
    )
    assert store.config_writes == [
        ("telegram_reply_to_message_id", 11),
        ("telegram_reply_to_message_id", 12),
    ]


async def test_telegram_reply_to_mode_first_pins_opening_message():
    deps = _make_deps()
    store = _ConfigStore()
    deps.session_store = store

    for ts, mid in (("903.0", 21), ("904.0", 22)):
        msg = dataclasses.replace(
            _make_msg(text="x", ts=ts), source={"message_id": mid},
        )
        await ChannelInboundPipeline().handle(
            msg,
            routing=_telegram_routing(),
            config={**_make_config(require_mention=False), "reply_to_mode": "first"},
            deps=deps,
        )
    assert store.config_writes == [("telegram_reply_to_message_id", 21)]


async def test_telegram_dm_never_records_reply_to():
    deps = _make_deps()
    store = _ConfigStore()
    deps.session_store = store
    msg = dataclasses.replace(
        _make_msg(is_dm=True, text="x", ts="905.0"), source={"message_id": 31},
    )
    await ChannelInboundPipeline().handle(
        msg,
        routing=_telegram_routing(),
        config={**_make_config(), "reply_to_mode": "all"},
        deps=deps,
    )
    assert store.config_writes == []


async def test_telegram_plain_reply_falls_through_when_resolution_races(monkeypatch):
    """A reply that loses the answer race (button tapped first) must become a
    normal turn, not vanish."""
    deps = _make_deps()

    async def pending_input(session_id):
        return {"tool_call_id": "tc9", "questions": [{"prompt": "Deploy?"}], "context": ""}

    deps.pending_input = pending_input
    monkeypatch.setattr(
        "surogates.session.interactive_input.resolve_input_response",
        AsyncMock(return_value=False),
    )

    msg = _make_msg(is_dm=True, text="also check the invoice", ts="910.0")
    result = await ChannelInboundPipeline().handle(
        msg, routing=_telegram_routing(), config=_make_config(), deps=deps,
    )

    assert result == InboundOutcome.PROCESSED
    assert deps._enqueued
    assert any(t == EventType.USER_MESSAGE for _, t, _ in deps.session_store.events)


async def test_caption_less_media_passes_body_gate():
    """Telegram attachments arrive with empty text and empty media_urls —
    the files alone must count as a message body."""
    deps = _make_deps()
    msg = dataclasses.replace(
        _make_msg(is_dm=True, text="", ts="911.0"),
        files=[InboundFileRef(url="fid", filename="a.jpg", mime_type="image/jpeg", size=1, file_id="fid")],
    )
    result = await ChannelInboundPipeline().handle(
        msg, routing=_telegram_routing(), config=_make_config(), deps=deps,
    )
    assert result == InboundOutcome.PROCESSED


async def test_telegram_files_reach_attachment_ingest():
    deps = _make_deps()
    ingested = []

    async def attachments(session_id, msg):
        ingested.append([f.file_id for f in msg.files])
        return {"images": [], "attachments": [], "note": ""}

    deps.attachments = attachments
    msg = dataclasses.replace(
        _make_msg(is_dm=True, text="see file", ts="906.0"),
        files=[InboundFileRef(url="fid", filename="a.pdf", mime_type="application/pdf", size=1, file_id="fid")],
    )
    result = await ChannelInboundPipeline().handle(
        msg, routing=_telegram_routing(), config=_make_config(), deps=deps,
    )
    assert result == InboundOutcome.PROCESSED
    assert ingested == [["fid"]]
