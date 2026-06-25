"""Tests for the per-platform outbound delivery loop.

Covers:
  1. deliver_batch happy path: claimed item → creds resolved by identifier →
     platform.send called → mark_delivered with the message_id.
  2. send returning failure → mark_failed (not mark_delivered).
  3. send RAISING an exception → mark_failed, batch continues to the next item.
  4. item missing channel_identifier in destination → mark_failed, no send.
  5. item whose identifier no longer resolves (tenant deprovisioned) → mark_failed.
  6. _enqueue_channel_delivery puts channel_identifier into the slack destination.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from surogates.channels.base import SendResult


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeOutboxItem:
    id: int
    session_id: UUID = field(default_factory=uuid4)
    event_id: int = 1
    channel: str = "slack"
    destination: dict = field(default_factory=dict)
    payload: dict = field(default_factory=lambda: {"content": "hello"})
    dedupe_key: str = "slack:1"
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class _FakeDeliveryService:
    """Records claim/mark calls; returns a configurable batch."""

    def __init__(self, items: list[_FakeOutboxItem] | None = None) -> None:
        self._items = items or []
        self.claimed: list[tuple[str, str]] = []  # (channel, worker_id)
        self.delivered: list[tuple[int, str | None]] = []  # (id, message_id)
        self.failed: list[tuple[int, str]] = []  # (id, error)

    async def claim_batch(
        self, channel: str, worker_id: str, *, limit: int = 50
    ) -> list[_FakeOutboxItem]:
        self.claimed.append((channel, worker_id))
        return list(self._items)

    async def mark_delivered(
        self, outbox_id: int, *, provider_message_id: str | None = None
    ) -> None:
        self.delivered.append((outbox_id, provider_message_id))

    async def mark_failed(self, outbox_id: int, error: str) -> None:
        self.failed.append((outbox_id, error))


class _FakePlatform:
    """Minimal ChannelPlatform fake for delivery tests."""

    kind = "slack"

    def __init__(self, result: SendResult | None = None, raises: Exception | None = None) -> None:
        self._result = result or SendResult(success=True, message_id="msg-001")
        self._raises = raises
        self.send_calls: list[tuple[Any, dict]] = []

        from surogates.channels.registry import ChannelDescriptor
        self.descriptor = ChannelDescriptor(
            vault_refs=lambda ident: {"bot_token": "bot_token"},
            config_keys=("bot_token",),
            webhook_registration="manual",
        )

    async def send(self, item: Any, *, creds: dict) -> SendResult:
        self.send_calls.append((item, creds))
        if self._raises:
            raise self._raises
        return self._result


class _FakeCache:
    """Returns a tenant for known identifiers, None for unknown."""

    def __init__(self, data: dict[str, dict] | None = None) -> None:
        self._data = data or {}

    async def get(self, key: str) -> dict | None:
        return self._data.get(key)


class _FakeVault:
    """Returns a fixed cred value for all refs."""

    def __init__(self, value: str = "xoxb-fake-token") -> None:
        self._value = value
        self.calls: list[tuple[str, str]] = []

    async def resolve_ref(self, ref: str, *, org_id: str) -> str | None:
        self.calls.append((ref, org_id))
        return self._value


def _make_dispatcher(
    platform: _FakePlatform | None = None,
    delivery: _FakeDeliveryService | None = None,
    cache: _FakeCache | None = None,
    vault: _FakeVault | None = None,
) -> "ChannelDeliveryDispatcher":
    from surogates.channels.dispatcher import ChannelDeliveryDispatcher

    return ChannelDeliveryDispatcher(
        cache=cache or _FakeCache(),
        vault=vault or _FakeVault(),
        delivery_service=delivery or _FakeDeliveryService(),
    )


# ---------------------------------------------------------------------------
# Helper: build a cache with a known tenant for identifier APP_ID
# ---------------------------------------------------------------------------

APP_ID = "A01234567"
ORG_ID = "org-aaaaaa"

_KNOWN_CACHE = {
    f"slack:{APP_ID}": {"org_id": ORG_ID, "agent_id": "agent-x", "config": {}},
}


# ---------------------------------------------------------------------------
# Tests: deliver_batch happy path
# ---------------------------------------------------------------------------


class TestDeliverBatchHappyPath:
    async def test_claimed_item_with_identifier_calls_send(self):
        """An item with a valid channel_identifier → platform.send is called."""
        item = _FakeOutboxItem(
            id=1,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform()
        cache = _FakeCache(_KNOWN_CACHE)
        vault = _FakeVault()

        dispatcher = _make_dispatcher(platform=platform, delivery=delivery, cache=cache, vault=vault)
        n = await dispatcher.deliver_batch(platform)

        assert len(platform.send_calls) == 1
        assert platform.send_calls[0][0] is item

    async def test_send_receives_creds_resolved_by_identifier(self):
        """Creds passed to send are resolved using the item's channel_identifier."""
        item = _FakeOutboxItem(
            id=1,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform()
        cache = _FakeCache(_KNOWN_CACHE)
        vault = _FakeVault(value="xoxb-resolved-token")

        dispatcher = _make_dispatcher(platform=platform, delivery=delivery, cache=cache, vault=vault)
        await dispatcher.deliver_batch(platform)

        # The creds dict passed to send should have the resolved token.
        _, creds = platform.send_calls[0]
        assert "bot_token" in creds
        assert creds["bot_token"] == "xoxb-resolved-token"

    async def test_mark_delivered_called_with_message_id(self):
        """Successful send → mark_delivered called with provider_message_id."""
        item = _FakeOutboxItem(
            id=42,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform(result=SendResult(success=True, message_id="slack-msg-999"))
        cache = _FakeCache(_KNOWN_CACHE)

        dispatcher = _make_dispatcher(platform=platform, delivery=delivery, cache=cache)
        await dispatcher.deliver_batch(platform)

        assert delivery.delivered == [(42, "slack-msg-999")]
        assert delivery.failed == []

    async def test_deliver_batch_returns_count_of_items(self):
        """deliver_batch returns the number of items processed."""
        items = [
            _FakeOutboxItem(id=i, destination={"channel_identifier": APP_ID}) for i in range(3)
        ]
        delivery = _FakeDeliveryService(items=items)
        platform = _FakePlatform()
        cache = _FakeCache(_KNOWN_CACHE)

        dispatcher = _make_dispatcher(platform=platform, delivery=delivery, cache=cache)
        n = await dispatcher.deliver_batch(platform)
        assert n == 3


# ---------------------------------------------------------------------------
# Tests: send returning failure
# ---------------------------------------------------------------------------


class TestDeliverBatchSendFailure:
    async def test_send_failure_calls_mark_failed(self):
        """send returning success=False → mark_failed, not mark_delivered."""
        item = _FakeOutboxItem(
            id=7,
            destination={"channel_identifier": APP_ID, "channel_id": "C002"},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform(result=SendResult(success=False, error="channel_not_found"))
        cache = _FakeCache(_KNOWN_CACHE)

        dispatcher = _make_dispatcher(platform=platform, delivery=delivery, cache=cache)
        await dispatcher.deliver_batch(platform)

        assert delivery.failed == [(7, "channel_not_found")]
        assert delivery.delivered == []

    async def test_send_failure_error_fallback_when_no_error_message(self):
        """SendResult(success=False, error=None) → mark_failed with a non-empty error."""
        item = _FakeOutboxItem(
            id=8,
            destination={"channel_identifier": APP_ID},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform(result=SendResult(success=False, error=None))
        cache = _FakeCache(_KNOWN_CACHE)

        dispatcher = _make_dispatcher(platform=platform, delivery=delivery, cache=cache)
        await dispatcher.deliver_batch(platform)

        # Should still be marked failed with SOME error string.
        assert len(delivery.failed) == 1
        failed_id, error_msg = delivery.failed[0]
        assert failed_id == 8
        assert error_msg  # non-empty


# ---------------------------------------------------------------------------
# Tests: send RAISES — per-item isolation
# ---------------------------------------------------------------------------


class TestDeliverBatchSendRaises:
    async def test_send_raising_calls_mark_failed(self):
        """A send that raises → mark_failed for that item."""
        item = _FakeOutboxItem(
            id=99,
            destination={"channel_identifier": APP_ID},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform(raises=RuntimeError("network timeout"))
        cache = _FakeCache(_KNOWN_CACHE)

        dispatcher = _make_dispatcher(platform=platform, delivery=delivery, cache=cache)
        await dispatcher.deliver_batch(platform)

        assert len(delivery.failed) == 1
        assert delivery.failed[0][0] == 99
        assert "network timeout" in delivery.failed[0][1]
        assert delivery.delivered == []

    async def test_send_raises_on_first_item_second_still_delivered(self):
        """A raising send for item-1 must NOT abort the batch; item-2 still delivered."""
        item1 = _FakeOutboxItem(
            id=10,
            destination={"channel_identifier": APP_ID, "channel_id": "C003"},
        )
        item2 = _FakeOutboxItem(
            id=11,
            destination={"channel_identifier": APP_ID, "channel_id": "C004"},
        )

        call_count = 0
        original_result = SendResult(success=True, message_id="m-11")

        class _PartialFail(_FakePlatform):
            async def send(self, item: Any, *, creds: dict) -> SendResult:
                nonlocal call_count
                call_count += 1
                self.send_calls.append((item, creds))
                if item.id == 10:
                    raise RuntimeError("first fails")
                return original_result

        platform = _PartialFail()
        delivery = _FakeDeliveryService(items=[item1, item2])
        cache = _FakeCache(_KNOWN_CACHE)

        dispatcher = _make_dispatcher(platform=platform, delivery=delivery, cache=cache)
        await dispatcher.deliver_batch(platform)

        assert call_count == 2
        assert (10, "first fails") in [(fid, err) for fid, err in delivery.failed]
        assert (11, "m-11") in delivery.delivered


# ---------------------------------------------------------------------------
# Tests: missing channel_identifier in destination
# ---------------------------------------------------------------------------


class TestMissingChannelIdentifier:
    async def test_missing_identifier_calls_mark_failed(self):
        """An item with no channel_identifier in destination → mark_failed, no send."""
        item = _FakeOutboxItem(
            id=5,
            destination={"channel_id": "C999"},  # no channel_identifier key
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform()
        cache = _FakeCache(_KNOWN_CACHE)

        dispatcher = _make_dispatcher(platform=platform, delivery=delivery, cache=cache)
        await dispatcher.deliver_batch(platform)

        assert len(delivery.failed) == 1
        assert delivery.failed[0][0] == 5
        assert platform.send_calls == []

    async def test_empty_identifier_calls_mark_failed(self):
        """An item with channel_identifier="" → mark_failed, no send."""
        item = _FakeOutboxItem(
            id=6,
            destination={"channel_identifier": ""},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform()
        cache = _FakeCache(_KNOWN_CACHE)

        dispatcher = _make_dispatcher(platform=platform, delivery=delivery, cache=cache)
        await dispatcher.deliver_batch(platform)

        assert len(delivery.failed) == 1
        assert delivery.failed[0][0] == 6
        assert platform.send_calls == []


# ---------------------------------------------------------------------------
# Tests: deprovisioned / unknown identifier
# ---------------------------------------------------------------------------


class TestUnknownIdentifier:
    async def test_unknown_identifier_calls_mark_failed(self):
        """An item whose identifier has no routing entry → mark_failed, no send."""
        item = _FakeOutboxItem(
            id=33,
            destination={"channel_identifier": "UNKNOWN_APP"},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform()
        cache = _FakeCache({})  # empty → every identifier unknown

        dispatcher = _make_dispatcher(platform=platform, delivery=delivery, cache=cache)
        await dispatcher.deliver_batch(platform)

        assert len(delivery.failed) == 1
        assert delivery.failed[0][0] == 33
        assert platform.send_calls == []

    async def test_unknown_identifier_does_not_block_known_item(self):
        """Unknown identifier for item-1 must not skip the valid item-2."""
        item1 = _FakeOutboxItem(
            id=20,
            destination={"channel_identifier": "GONE_APP"},
        )
        item2 = _FakeOutboxItem(
            id=21,
            destination={"channel_identifier": APP_ID},
        )
        delivery = _FakeDeliveryService(items=[item1, item2])
        platform = _FakePlatform()
        cache = _FakeCache(_KNOWN_CACHE)

        dispatcher = _make_dispatcher(platform=platform, delivery=delivery, cache=cache)
        await dispatcher.deliver_batch(platform)

        # item1 failed (unknown app), item2 delivered
        assert any(fid == 20 for fid, _ in delivery.failed)
        assert any(did == 21 for did, _ in delivery.delivered)


# ---------------------------------------------------------------------------
# Tests: _enqueue_channel_delivery puts channel_identifier in destination
# ---------------------------------------------------------------------------


class TestEnqueueChannelDeliveryIncludesIdentifier:
    """Verify that _enqueue_channel_delivery copies channel_identifier into destination."""

    async def test_slack_destination_includes_channel_identifier(self):
        """Slack destination dict must contain channel_identifier from session config."""
        from surogates.session.store import SessionStore

        # Build a minimal fake SessionStore with the parts _enqueue_channel_delivery uses.
        captured: list[dict] = []

        class _FakeSF:
            """Async context manager yielding a fake DB session."""
            def __call__(self):
                return _FakeSF()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def get(self, model, pk):
                # Return a fake session row with channel='slack' and the config.
                # Keys match what inbound.py writes: {platform}_channel_id,
                # {platform}_thread_key, channel_identifier.
                from types import SimpleNamespace
                return SimpleNamespace(
                    channel="slack",
                    config={
                        "slack_channel_id": "C123",
                        "slack_thread_key": "1234.5678",
                        "channel_identifier": "A_SLACK_APP",
                    },
                )
            async def add(self, obj):
                pass
            async def commit(self):
                pass

        class _FakeOutboxCapture:
            def __init__(self, **kw):
                captured.append(dict(kw))
            id = None

        # Patch DeliveryOutbox at its definition site (surogates.db.models) so
        # the inline `from surogates.db.models import DeliveryOutbox` inside
        # _enqueue_channel_delivery picks up the fake.
        import unittest.mock as mock
        from surogates.session import store as store_mod
        from surogates.session.events import EventType

        with mock.patch("surogates.db.models.DeliveryOutbox", _FakeOutboxCapture):
            sf = _FakeSF()
            # Instantiate a bare SessionStore (bypassing __init__ side effects).
            ss = object.__new__(store_mod.SessionStore)
            ss._sf = sf
            ss._channel_cache = {}

            await ss._enqueue_channel_delivery(
                session_id=uuid4(),
                event_id=1,
                event_type=EventType.LLM_RESPONSE,
                data={"message": {"content": "hello world"}},
            )

        assert len(captured) == 1, "Expected exactly one outbox row to be enqueued"
        dest = captured[0]["destination"]
        assert "channel_identifier" in dest, (
            f"channel_identifier missing from slack destination: {dest}"
        )
        assert dest["channel_identifier"] == "A_SLACK_APP"


# ---------------------------------------------------------------------------
# Seam tests: pipeline-written keys → store reads → send-consumable destination
# ---------------------------------------------------------------------------


def _make_fake_store(channel: str, config: dict) -> "object":
    """Return a bare SessionStore wired with a fake session row."""
    import unittest.mock as mock
    from surogates.session import store as store_mod

    class _FakeSF:
        def __call__(self):
            return _FakeSF()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, model, pk):
            from types import SimpleNamespace
            return SimpleNamespace(channel=channel, config=config)

        async def add(self, obj):
            pass

        async def commit(self):
            pass

    ss = object.__new__(store_mod.SessionStore)
    ss._sf = _FakeSF()
    ss._channel_cache = {}
    return ss


async def _capture_destination(channel: str, config: dict) -> dict:
    """Run _enqueue_channel_delivery with the given config and return destination."""
    import unittest.mock as mock
    from surogates.session.events import EventType

    captured: list[dict] = []

    class _FakeOutboxCapture:
        def __init__(self, **kw):
            captured.append(dict(kw))

        id = None

    ss = _make_fake_store(channel, config)
    with mock.patch("surogates.db.models.DeliveryOutbox", _FakeOutboxCapture):
        await ss._enqueue_channel_delivery(
            session_id=uuid4(),
            event_id=1,
            event_type=EventType.LLM_RESPONSE,
            data={"message": {"content": "seam test"}},
        )

    assert len(captured) == 1, f"Expected one outbox row, got {len(captured)}"
    return captured[0]["destination"]


class TestDeliverySeamPipelineKeys:
    """Seam tests: config keys written by inbound.py → destination consumed by send().

    The pipeline (inbound.py ~line 347) writes:
        {platform}_channel_id, {platform}_thread_key, channel_identifier

    SlackPlatform.send reads: destination["channel_id"], destination.get("thread_ts")
    TelegramPlatform.send reads: destination["chat_id"], destination.get("message_thread_id")

    store.py _enqueue_channel_delivery must translate between the two.
    """

    async def test_slack_pipeline_keys_produce_nonempty_channel_id(self):
        """Pipeline writes slack_channel_id → destination["channel_id"] is non-empty."""
        config = {
            "slack_channel_id": "C1SEAM",
            "slack_thread_key": "123.45",
            "channel_identifier": "A0X_SEAM",
        }
        dest = await _capture_destination("slack", config)
        assert dest.get("channel_id"), (
            f"destination['channel_id'] is empty or missing; got: {dest!r}\n"
            "This is the seam bug: store reads 'slack_channel_id' but must map it to 'channel_id'."
        )
        assert dest["channel_id"] == "C1SEAM"

    async def test_slack_pipeline_keys_produce_thread_ts(self):
        """Pipeline writes slack_thread_key → destination["thread_ts"] is set."""
        config = {
            "slack_channel_id": "C1SEAM",
            "slack_thread_key": "123.45",
            "channel_identifier": "A0X_SEAM",
        }
        dest = await _capture_destination("slack", config)
        assert dest.get("thread_ts") == "123.45", (
            f"destination['thread_ts'] should be '123.45'; got: {dest.get('thread_ts')!r}\n"
            "store must read 'slack_thread_key' (not 'slack_thread_ts')."
        )

    async def test_slack_no_stale_team_id_key(self):
        """Pipeline never writes slack_team_id; destination must not include it."""
        config = {
            "slack_channel_id": "C1SEAM",
            "slack_thread_key": "123.45",
            "channel_identifier": "A0X_SEAM",
        }
        dest = await _capture_destination("slack", config)
        # team_id is not consumed by SlackPlatform.send and was never written
        # by the pipeline; it should not appear in the destination.
        assert "team_id" not in dest, (
            f"destination must not contain 'team_id' (never written by pipeline): {dest!r}"
        )

    async def test_telegram_pipeline_keys_produce_nonempty_chat_id(self):
        """Pipeline writes telegram_channel_id → destination["chat_id"] is non-empty.

        This is the CRITICAL bug: store was reading 'telegram_chat_id' but the
        pipeline writes 'telegram_channel_id', so chat_id was always ''.
        """
        config = {
            "telegram_channel_id": "-100123456789",
            "telegram_thread_key": "42",
            "channel_identifier": "@my_bot",
        }
        dest = await _capture_destination("telegram", config)
        assert dest.get("chat_id"), (
            f"destination['chat_id'] is empty or missing; got: {dest!r}\n"
            "CRITICAL seam bug: store reads 'telegram_chat_id' but pipeline writes "
            "'telegram_channel_id'."
        )
        assert dest["chat_id"] == "-100123456789"

    async def test_telegram_pipeline_keys_produce_message_thread_id(self):
        """Pipeline writes telegram_thread_key → destination["message_thread_id"] is set."""
        config = {
            "telegram_channel_id": "-100123456789",
            "telegram_thread_key": "42",
            "channel_identifier": "@my_bot",
        }
        dest = await _capture_destination("telegram", config)
        assert dest.get("message_thread_id") == "42", (
            f"destination['message_thread_id'] should be '42'; got: {dest.get('message_thread_id')!r}\n"
            "store must read 'telegram_thread_key' and emit 'message_thread_id'."
        )


# ---------------------------------------------------------------------------
# FIX 2: mark_bot_message called after successful delivery
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal Redis fake tracking set calls."""

    def __init__(self) -> None:
        self.kv: dict[str, Any] = {}

    async def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self.kv[key] = value

    async def get(self, key: str) -> Any:
        return self.kv.get(key)

    async def exists(self, key: str) -> int:
        return 1 if key in self.kv else 0


def _make_dispatcher_with_redis(
    platform: _FakePlatform | None = None,
    delivery: _FakeDeliveryService | None = None,
    cache: _FakeCache | None = None,
    vault: _FakeVault | None = None,
    redis: Any = None,
) -> "ChannelDeliveryDispatcher":
    from surogates.channels.dispatcher import ChannelDeliveryDispatcher

    return ChannelDeliveryDispatcher(
        cache=cache or _FakeCache(),
        vault=vault or _FakeVault(),
        delivery_service=delivery or _FakeDeliveryService(),
        redis=redis or _FakeRedis(),
    )


class TestMarkBotMessageAfterDelivery:
    """FIX 2: After a successful send, mark_bot_message is called in SlackAdapterState."""

    async def test_successful_send_marks_bot_message_for_message_id(self):
        """After successful delivery, state.is_bot_message(result.message_id) is True."""
        from surogates.channels.slack_state import SlackAdapterState

        fake_redis = _FakeRedis()
        agent_id = "agent-x"

        item = _FakeOutboxItem(
            id=100,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
        )
        delivery = _FakeDeliveryService(items=[item])
        send_result = SendResult(success=True, message_id="sent-msg-ts-001")
        platform = _FakePlatform(result=send_result)
        cache = _FakeCache({
            f"slack:{APP_ID}": {"org_id": ORG_ID, "agent_id": agent_id, "config": {}},
        })

        dispatcher = _make_dispatcher_with_redis(
            platform=platform, delivery=delivery, cache=cache, redis=fake_redis
        )
        await dispatcher.deliver_batch(platform)

        # Check that the bot message was marked in state for that agent
        state = SlackAdapterState(fake_redis, agent_id=agent_id)
        assert await state.is_bot_message("sent-msg-ts-001"), (
            "After successful delivery, is_bot_message(result.message_id) must be True"
        )

    async def test_successful_send_marks_bot_message_for_thread_ts(self):
        """If destination has thread_ts, that thread_ts is also marked as a bot message."""
        from surogates.channels.slack_state import SlackAdapterState

        fake_redis = _FakeRedis()
        agent_id = "agent-y"

        item = _FakeOutboxItem(
            id=101,
            destination={
                "channel_identifier": APP_ID,
                "channel_id": "C001",
                "thread_ts": "original-thread-001",
            },
        )
        delivery = _FakeDeliveryService(items=[item])
        send_result = SendResult(success=True, message_id="reply-msg-001")
        platform = _FakePlatform(result=send_result)
        cache = _FakeCache({
            f"slack:{APP_ID}": {"org_id": ORG_ID, "agent_id": agent_id, "config": {}},
        })

        dispatcher = _make_dispatcher_with_redis(
            platform=platform, delivery=delivery, cache=cache, redis=fake_redis
        )
        await dispatcher.deliver_batch(platform)

        state = SlackAdapterState(fake_redis, agent_id=agent_id)
        assert await state.is_bot_message("reply-msg-001"), (
            "reply message_id must be marked as bot message"
        )
        assert await state.is_bot_message("original-thread-001"), (
            "thread_ts from destination must also be marked as bot message"
        )

    async def test_failed_send_does_not_mark_bot_message(self):
        """A failed send must NOT mark any bot message."""
        from surogates.channels.slack_state import SlackAdapterState

        fake_redis = _FakeRedis()
        agent_id = "agent-z"

        item = _FakeOutboxItem(
            id=102,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform(result=SendResult(success=False, error="channel_not_found"))
        cache = _FakeCache({
            f"slack:{APP_ID}": {"org_id": ORG_ID, "agent_id": agent_id, "config": {}},
        })

        dispatcher = _make_dispatcher_with_redis(
            platform=platform, delivery=delivery, cache=cache, redis=fake_redis
        )
        await dispatcher.deliver_batch(platform)

        # No bot messages should have been marked
        assert not fake_redis.kv, (
            f"Failed send must not mark any bot messages; redis had: {fake_redis.kv!r}"
        )

    async def test_bot_thread_reply_pipeline_processes_non_mention(self):
        """After marking a bot message, a pipeline run of a non-mention thread reply is PROCESSED."""
        import sys
        sys.path.insert(0, "/work/surogates")
        from surogates.channels.inbound import (
            ChannelInboundPipeline,
            InboundMessage,
            InboundOutcome,
            PipelineDeps,
        )

        # Setup: fake state that simulates the bot having sent message_id="bot-reply-ts"
        class _FakeStateFix2:
            def __init__(self) -> None:
                self._botmsg: set[str] = {"bot-reply-ts"}
                self._mentioned: set[str] = set()
                self._sessions: dict[str, str] = {}

            async def remember_session(self, k, v):
                self._sessions[k] = v

            async def get_session(self, k):
                return self._sessions.get(k)

            async def mark_mentioned_thread(self, t):
                self._mentioned.add(t)

            async def is_mentioned_thread(self, t):
                return t in self._mentioned

            async def mark_bot_message(self, ts):
                self._botmsg.add(ts)

            async def is_bot_message(self, ts):
                return ts in self._botmsg

        # Fake deps
        from uuid import uuid4, UUID
        from surogates.channels.source import SessionSource, build_session_key

        store_events: list = []

        class _FakeSS:
            async def emit_event(self, sid, et, data):
                store_events.append((sid, et, data))

        class _FakeRedisInPipeline:
            def __init__(self):
                self.kv: dict = {}

            async def set(self, k, v, ex=None):
                self.kv[k] = v

            async def get(self, k):
                return self.kv.get(k)

            async def exists(self, k):
                return 1 if k in self.kv else 0

            async def rpush(self, k, v):
                pass

            async def ltrim(self, k, s, e):
                pass

        sessions_created: list = []

        async def get_or_create(ss, redis, *, session_key, user_id, org_id, agent_id, channel, config, session_factory, model=""):
            sessions_created.append(session_key)
            return UUID("aaaaaaaa-0000-0000-0000-000000000002")

        enqueued: list = []

        async def enqueue(redis, *, org_id, agent_id, session_id):
            enqueued.append(session_id)

        async def resolve_id(sf, platform, uid):
            class _Id:
                user_id = UUID("bbbbbbbb-0000-0000-0000-000000000001")
            return _Id()

        async def firehose(*a, **kw):
            pass

        class _Pairing:
            async def create(self, *a, **kw):
                return None

        state = _FakeStateFix2()
        deps = PipelineDeps(
            session_store=_FakeSS(),
            redis=_FakeRedisInPipeline(),
            state=state,
            pairing=_Pairing(),
            firehose_append=firehose,
            get_or_create_session=get_or_create,
            enqueue_session=enqueue,
            resolve_identity=resolve_id,
            session_factory=None,
            pairing_sender=lambda *a: None,
        )

        class _Routing:
            org_id = "org-fix2"
            agent_id = "agent-fix2"
            platform = "slack"
            identifier = "APP_FIX2"

        # thread_key == "bot-reply-ts" which is already in _botmsg
        msg = InboundMessage(
            kind="text",
            identifier="C_FIX2",
            thread_key="bot-reply-ts",
            platform_user_id="USER1",
            user_name="User1",
            text="reply without mention",
            media_urls=[],
            media_types=[],
            is_dm=False,
            is_mention=False,
            ts="fix2-unique-ts",
            source={},
        )

        pipeline = ChannelInboundPipeline()
        result = await pipeline.handle(
            msg,
            routing=_Routing(),
            config={"require_mention": True},
            deps=deps,
        )

        assert result == InboundOutcome.PROCESSED, (
            f"Non-mention thread reply whose thread_key == a bot message ts → PROCESSED; got {result}"
        )
