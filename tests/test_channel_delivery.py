"""Outbound delivery, retries, media uploads and progress-message workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4


from surogates.channels.base import SendResult
from surogates.channels.dispatcher import ChannelDeliveryDispatcher


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
        self.failed: list[tuple[int, str]] = []  # (id, error) — will retry
        self.dead: list[tuple[int, str]] = []  # (id, error) — terminal, no retry

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

    async def mark_dead(self, outbox_id: int, error: str) -> None:
        self.dead.append((outbox_id, error))


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
    redis: Any = None,
) -> "ChannelDeliveryDispatcher":
    return ChannelDeliveryDispatcher(
        cache=cache or _FakeCache(),
        vault=vault or _FakeVault(),
        delivery_service=delivery or _FakeDeliveryService(),
        redis=redis,
    )


APP_ID = "A01234567"
ORG_ID = "org-aaaaaa"

_KNOWN_CACHE = {
    f"slack:{APP_ID}": {"org_id": ORG_ID, "agent_id": "agent-x", "config": {}},
}


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

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.kv.pop(key, None)


def _make_dispatcher_with_redis(
    platform: _FakePlatform | None = None,
    delivery: _FakeDeliveryService | None = None,
    cache: _FakeCache | None = None,
    vault: _FakeVault | None = None,
    redis: Any = None,
) -> "ChannelDeliveryDispatcher":
    return ChannelDeliveryDispatcher(
        cache=cache or _FakeCache(),
        vault=vault or _FakeVault(),
        delivery_service=delivery or _FakeDeliveryService(),
        redis=redis or _FakeRedis(),
    )


class TestMarkBotMessageAfterDelivery:
    """FIX 2: After a successful send, mark_bot_message is called in ChannelAdapterState."""

    async def test_successful_send_marks_bot_message_for_message_id(self):
        """After successful delivery, state.is_bot_message(result.message_id) is True."""
        from surogates.channels.channel_state import ChannelAdapterState

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
        state = ChannelAdapterState(fake_redis, agent_id=agent_id, platform="slack")
        assert await state.is_bot_message("sent-msg-ts-001"), (
            "After successful delivery, is_bot_message(result.message_id) must be True"
        )

    async def test_successful_send_marks_bot_message_for_thread_ts(self):
        """If destination has thread_ts, that thread_ts is also marked as a bot message."""
        from surogates.channels.channel_state import ChannelAdapterState

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

        state = ChannelAdapterState(fake_redis, agent_id=agent_id, platform="slack")
        assert await state.is_bot_message("reply-msg-001"), (
            "reply message_id must be marked as bot message"
        )
        assert await state.is_bot_message("original-thread-001"), (
            "thread_ts from destination must also be marked as bot message"
        )

    async def test_failed_send_does_not_mark_bot_message(self):
        """A failed send must NOT mark any bot message."""

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
        from uuid import UUID

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

        async def resolve_id(sf, platform, uid, *, org_id=None, display_name=""):
            class _Id:
                user_id = UUID("bbbbbbbb-0000-0000-0000-000000000001")
            return _Id()

        async def firehose(*a, **kw):
            pass

        state = _FakeStateFix2()
        deps = PipelineDeps(
            session_store=_FakeSS(),
            redis=_FakeRedisInPipeline(),
            state=state,
            firehose_append=firehose,
            get_or_create_session=get_or_create,
            enqueue_session=enqueue,
            resolve_identity=resolve_id,
            session_factory=None,
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

    async def test_mark_bot_message_error_does_not_cause_mark_failed(self):
        """A Redis error during mark_bot_message must NOT propagate or trigger mark_failed."""

        class _ErrorRedis:
            """Redis fake whose set always raises."""
            async def set(self, key: str, value: Any, ex: int | None = None) -> None:
                raise RuntimeError("redis down")

            async def get(self, key: str) -> Any:
                return None

            async def exists(self, key: str) -> int:
                return 0

        agent_id = "agent-x"
        item = _FakeOutboxItem(
            id=200,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
        )
        delivery = _FakeDeliveryService(items=[item])
        send_result = SendResult(success=True, message_id="msg-redis-down")
        platform = _FakePlatform(result=send_result)
        cache = _FakeCache(_KNOWN_CACHE)

        dispatcher = _make_dispatcher_with_redis(
            platform=platform, delivery=delivery, cache=cache, redis=_ErrorRedis()
        )
        # Must not raise
        await dispatcher.deliver_batch(platform)

        # Delivery must still be recorded
        assert any(did == 200 for did, _ in delivery.delivered), (
            "mark_delivered must have been called even when mark_bot_message raises"
        )
        # No mark_failed must have been called
        assert not any(fid == 200 for fid, _ in delivery.failed), (
            "mark_failed must NOT be called when only mark_bot_message raises"
        )

    async def test_telegram_message_thread_id_is_marked_as_bot_message(self):
        """Telegram destination with message_thread_id → thread is marked as bot message."""
        from surogates.channels.channel_state import ChannelAdapterState

        fake_redis = _FakeRedis()
        agent_id = "agent-x"

        item = _FakeOutboxItem(
            id=201,
            destination={
                "channel_identifier": APP_ID,
                "chat_id": "12345",
                "message_thread_id": "99",
            },
        )
        delivery = _FakeDeliveryService(items=[item])
        send_result = SendResult(success=True, message_id="tg-msg-001")
        platform = _FakePlatform(result=send_result)
        cache = _FakeCache(_KNOWN_CACHE)

        dispatcher = _make_dispatcher_with_redis(
            platform=platform, delivery=delivery, cache=cache, redis=fake_redis
        )
        await dispatcher.deliver_batch(platform)

        state = ChannelAdapterState(fake_redis, agent_id=agent_id, platform="slack")
        assert await state.is_bot_message("99"), (
            "message_thread_id from Telegram destination must be marked as bot message"
        )

    async def test_slack_thread_ts_still_marked_as_bot_message(self):
        """Regression: Slack thread_ts in destination must still be marked as bot message."""
        from surogates.channels.channel_state import ChannelAdapterState

        fake_redis = _FakeRedis()
        agent_id = "agent-x"

        item = _FakeOutboxItem(
            id=202,
            destination={
                "channel_identifier": APP_ID,
                "channel_id": "C001",
                "thread_ts": "slack-thread-001",
            },
        )
        delivery = _FakeDeliveryService(items=[item])
        send_result = SendResult(success=True, message_id="slack-msg-001")
        platform = _FakePlatform(result=send_result)
        cache = _FakeCache(_KNOWN_CACHE)

        dispatcher = _make_dispatcher_with_redis(
            platform=platform, delivery=delivery, cache=cache, redis=fake_redis
        )
        await dispatcher.deliver_batch(platform)

        state = ChannelAdapterState(fake_redis, agent_id=agent_id, platform="slack")
        assert await state.is_bot_message("slack-thread-001"), (
            "thread_ts from Slack destination must still be marked as bot message"
        )


class TestThinkingPlaceholderDelivery:
    async def test_matching_placeholder_injects_update_ts_and_clears_on_success(self):
        """A same-channel placeholder → update_ts injected into destination; key cleared after successful send."""
        from surogates.channels.channel_progress import (
            read_placeholder,
            set_placeholder,
        )

        redis = _FakeRedis()
        item = _FakeOutboxItem(
            id=101,
            destination={
                "channel_identifier": APP_ID,
                "channel_id": "C001",
                "thread_ts": "10.0",
            },
        )
        await set_placeholder(
            redis,
            "slack",
            item.session_id,
            channel="C001",
            ts="20.0",
            thread_ts="10.0",
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform(result=SendResult(success=True, message_id="20.0"))
        dispatcher = _make_dispatcher(
            platform=platform,
            delivery=delivery,
            cache=_FakeCache(_KNOWN_CACHE),
            redis=redis,
        )

        await dispatcher._deliver_item(platform, item)

        # update_ts must be injected into the destination before send
        assert platform.send_calls[0][0].destination["update_ts"] == "20.0"
        # Redis key must be cleared after successful send
        assert await read_placeholder(redis, "slack", item.session_id) is None
        # Item must be marked delivered (not failed)
        assert delivery.delivered == [(101, "20.0")]
        assert delivery.failed == []

    async def test_no_placeholder_posts_fresh_without_update_ts(self):
        """No placeholder seeded → destination has no update_ts; send still succeeds."""
        item = _FakeOutboxItem(
            id=102,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
        )
        redis = _FakeRedis()
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform(result=SendResult(success=True, message_id="new-1"))
        dispatcher = _make_dispatcher(
            platform=platform,
            delivery=delivery,
            cache=_FakeCache(_KNOWN_CACHE),
            redis=redis,
        )

        await dispatcher._deliver_item(platform, item)

        # No update_ts injected
        assert "update_ts" not in platform.send_calls[0][0].destination
        assert delivery.delivered == [(102, "new-1")]
        assert delivery.failed == []

    async def test_channel_mismatch_clears_stale_placeholder_and_posts_fresh(self):
        """Placeholder for a different channel → stale entry cleared; send posts fresh."""
        from surogates.channels.channel_progress import (
            read_placeholder,
            set_placeholder,
        )

        redis = _FakeRedis()
        item = _FakeOutboxItem(
            id=103,
            destination={"channel_identifier": APP_ID, "channel_id": "C_NEW"},
        )
        await set_placeholder(
            redis,
            "slack",
            item.session_id,
            channel="C_OLD",
            ts="old-1",
            thread_ts=None,
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform(result=SendResult(success=True, message_id="new-2"))
        dispatcher = _make_dispatcher(
            platform=platform,
            delivery=delivery,
            cache=_FakeCache(_KNOWN_CACHE),
            redis=redis,
        )

        await dispatcher._deliver_item(platform, item)

        # Stale placeholder was cleared; no update_ts injected
        assert "update_ts" not in platform.send_calls[0][0].destination
        assert await read_placeholder(redis, "slack", item.session_id) is None
        assert delivery.delivered == [(103, "new-2")]

    async def test_send_failure_retains_matching_placeholder_for_retry(self):
        """A failed send must NOT clear the placeholder so the next retry can still edit it."""
        from surogates.channels.channel_progress import (
            read_placeholder,
            set_placeholder,
        )

        redis = _FakeRedis()
        item = _FakeOutboxItem(
            id=104,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
        )
        await set_placeholder(
            redis,
            "slack",
            item.session_id,
            channel="C001",
            ts="20.0",
            thread_ts=None,
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform(result=SendResult(success=False, error="rate_limited"))
        dispatcher = _make_dispatcher(
            platform=platform,
            delivery=delivery,
            cache=_FakeCache(_KNOWN_CACHE),
            redis=redis,
        )

        await dispatcher._deliver_item(platform, item)

        # update_ts was injected even on failure
        assert platform.send_calls[0][0].destination["update_ts"] == "20.0"
        # Placeholder must NOT be cleared on failure
        assert await read_placeholder(redis, "slack", item.session_id) == {
            "channel": "C001",
            "ts": "20.0",
            "thread_ts": None,
        }
        assert delivery.delivered == []
        assert delivery.failed == [(104, "rate_limited")]


@dataclass
class _MediaSession:
    id: str = "22222222-2222-2222-2222-222222222222"
    config: dict = field(default_factory=lambda: {"storage_bucket": "wsbucket"})


class _FakeSessionStore:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def get_session(self, session_id: Any) -> Any:
        return self._session


class _MediaStorage:
    """Storage that returns bytes for any key whose basename is in `present`."""

    def __init__(self, present: dict[str, bytes]) -> None:
        self._present = present  # basename -> bytes

    def _name(self, key: str) -> str:
        return key.rsplit("/", 1)[-1]

    async def stat(self, bucket: str, key: str):
        name = self._name(key)
        if name not in self._present:
            raise KeyError(key)
        return {"size": len(self._present[name])}

    async def read(self, bucket: str, key: str) -> bytes:
        name = self._name(key)
        if name not in self._present:
            raise KeyError(key)
        return self._present[name]


class _ProgressRedis:
    """Minimal async Redis: get/delete over an in-memory dict, recording deletes."""

    def __init__(self, store: dict | None = None) -> None:
        self._store = store or {}
        self.deleted: list[str] = []

    async def get(self, key: str):
        return self._store.get(key)

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self._store.pop(key, None)


class _FakeMediaPlatform(_FakePlatform):
    """_FakePlatform plus outbound file upload + message delete recording."""

    def __init__(self, uploaded_ids: list[str] | None = None, **kw) -> None:
        super().__init__(**kw)
        self._uploaded_ids = uploaded_ids if uploaded_ids is not None else ["F999"]
        self.send_files_calls: list[tuple[Any, list]] = []
        self.deleted: list[dict] = []

    async def send_files(self, item: Any, *, creds: dict, files: list) -> list[str]:
        self.send_files_calls.append((item, files))
        return list(self._uploaded_ids) if files else []

    async def delete_message(self, *, creds: dict, channel: str, ts: str) -> None:
        self.deleted.append({"channel": channel, "ts": ts})


def _media_dispatcher(platform, delivery, storage, session_store):
    from surogates.channels.dispatcher import ChannelDeliveryDispatcher
    return ChannelDeliveryDispatcher(
        cache=_FakeCache(_KNOWN_CACHE),
        vault=_FakeVault(),
        delivery_service=delivery,
        redis=None,
        storage=storage,
        session_store=session_store,
    )


class TestSlackMediaDelivery:
    async def test_text_with_marker_posts_text_and_uploads(self):
        item = _FakeOutboxItem(
            id=1,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
            payload={"content": "Here it is. MEDIA:/workspace/report.pdf"},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakeMediaPlatform()
        storage = _MediaStorage({"report.pdf": b"%PDF data"})
        store = _FakeSessionStore(_MediaSession())

        dispatcher = _media_dispatcher(platform, delivery, storage, store)
        await dispatcher.deliver_batch(platform)

        # Text was sent with the marker stripped.
        sent_item, _ = platform.send_calls[0]
        assert sent_item.payload["content"] == "Here it is."
        # The workspace file was uploaded.
        assert len(platform.send_files_calls) == 1
        _, files = platform.send_files_calls[0]
        assert [f.filename for f in files] == ["report.pdf"]
        assert delivery.delivered == [(1, "msg-001")]

    async def test_marker_only_uploads_without_text_and_marks_delivered(self):
        item = _FakeOutboxItem(
            id=2,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
            payload={"content": "MEDIA:/workspace/only.pdf"},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakeMediaPlatform(uploaded_ids=["FA1"])
        storage = _MediaStorage({"only.pdf": b"data"})
        store = _FakeSessionStore(_MediaSession())

        dispatcher = _media_dispatcher(platform, delivery, storage, store)
        await dispatcher.deliver_batch(platform)

        # No text send; file uploaded; item delivered with the file id.
        assert platform.send_calls == []
        assert len(platform.send_files_calls) == 1
        assert delivery.delivered == [(2, "FA1")]

    async def test_marker_only_upload_failure_posts_generic_message(self):
        item = _FakeOutboxItem(
            id=3,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
            payload={"content": "MEDIA:/workspace/missing.pdf"},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakeMediaPlatform()  # send_files returns [] for no files
        storage = _MediaStorage({})  # missing.pdf not present → no files resolved
        store = _FakeSessionStore(_MediaSession())

        dispatcher = _media_dispatcher(platform, delivery, storage, store)
        await dispatcher.deliver_batch(platform)

        # Falls back to the generic non-leaking failure message via send.
        assert len(platform.send_calls) == 1
        sent_item, _ = platform.send_calls[0]
        assert sent_item.payload["content"] == "I couldn't upload the referenced file."
        assert "MEDIA:" not in sent_item.payload["content"]
        assert platform.send_files_calls == []  # nothing to re-upload
        assert delivery.delivered == [(3, "msg-001")]

    async def test_no_marker_text_is_unchanged(self):
        item = _FakeOutboxItem(
            id=4,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
            payload={"content": "just a normal reply"},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakeMediaPlatform()
        storage = _MediaStorage({})
        store = _FakeSessionStore(_MediaSession())

        dispatcher = _media_dispatcher(platform, delivery, storage, store)
        await dispatcher.deliver_batch(platform)

        sent_item, _ = platform.send_calls[0]
        assert sent_item.payload["content"] == "just a normal reply"
        assert platform.send_files_calls == []

    async def test_pathless_marker_only_posts_generic_message(self):
        item = _FakeOutboxItem(
            id=6,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
            payload={"content": "MEDIA:"},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakeMediaPlatform()
        storage = _MediaStorage({})
        store = _FakeSessionStore(_MediaSession())

        dispatcher = _media_dispatcher(platform, delivery, storage, store)
        await dispatcher.deliver_batch(platform)

        # A bare 'MEDIA:' must never produce an empty send; the user gets the
        # generic non-leaking message instead.
        assert len(platform.send_calls) == 1
        sent_item, _ = platform.send_calls[0]
        assert sent_item.payload["content"] == "I couldn't upload the referenced file."
        assert "MEDIA:" not in sent_item.payload["content"]
        assert platform.send_files_calls == []
        assert delivery.delivered == [(6, "msg-001")]

    async def test_marker_only_with_placeholder_deletes_and_clears(self):
        import json
        from surogates.channels.channel_progress import progress_key
        from surogates.channels.dispatcher import ChannelDeliveryDispatcher

        session_id = uuid4()
        item = _FakeOutboxItem(
            id=5,
            session_id=session_id,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
            payload={"content": "MEDIA:/workspace/only.pdf"},
        )
        redis = _ProgressRedis({
            progress_key("slack", session_id): json.dumps(
                {"channel": "C001", "ts": "1700.5", "thread_ts": None}
            ),
        })
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakeMediaPlatform(uploaded_ids=["FA9"])
        storage = _MediaStorage({"only.pdf": b"data"})
        store = _FakeSessionStore(_MediaSession())
        dispatcher = ChannelDeliveryDispatcher(
            cache=_FakeCache(_KNOWN_CACHE), vault=_FakeVault(),
            delivery_service=delivery, redis=redis, storage=storage, session_store=store,
        )

        await dispatcher.deliver_batch(platform)

        # No text send; placeholder message deleted on Slack; Redis key cleared.
        assert platform.send_calls == []
        assert platform.deleted == [{"channel": "C001", "ts": "1700.5"}]
        assert progress_key("slack", session_id) in redis.deleted
        assert delivery.delivered == [(5, "FA9")]


class _FullRedis:
    """Fake Redis supporting get/set/delete and set(..., nx=True) for throttle tests."""

    def __init__(self, store: dict | None = None) -> None:
        self.kv: dict[str, Any] = dict(store or {})
        self.deleted: list[str] = []

    async def get(self, key: str) -> Any:
        return self.kv.get(key)

    async def set(
        self,
        key: str,
        value: Any,
        ex: int | None = None,
        nx: bool = False,
    ) -> Any:
        if nx:
            if key in self.kv:
                return None  # already set — NX fails
            self.kv[key] = value
            return True
        self.kv[key] = value
        return True

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.deleted.append(key)
            self.kv.pop(key, None)

    async def exists(self, key: str) -> int:
        return 1 if key in self.kv else 0


class _FakeEditablePlatform(_FakePlatform):
    """_FakePlatform that declares supports_edit = True."""
    supports_edit = True


def _make_live_dispatcher(
    platform=None,
    delivery=None,
    redis=None,
):
    from surogates.channels.dispatcher import ChannelDeliveryDispatcher

    return ChannelDeliveryDispatcher(
        cache=_FakeCache(_KNOWN_CACHE),
        vault=_FakeVault(),
        delivery_service=delivery or _FakeDeliveryService(),
        redis=redis or _FullRedis(),
    )


class TestLiveProgressDispatcher:
    """State-machine tests for live-update / intermediate-suppression behaviour."""

    async def test_slack_intermediate_with_placeholder_edits_and_sets_new_placeholder(self):
        """Intermediate + edit-capable + existing placeholder → send with update_ts;
        clear_placeholder NOT called; set_placeholder called with result ts."""
        import json
        from surogates.channels.channel_progress import progress_key, read_placeholder

        session_id = uuid4()
        placeholder_ts = "111.0"
        redis = _FullRedis({
            progress_key("slack", session_id): json.dumps(
                {"channel": "C001", "ts": placeholder_ts, "thread_ts": "100.0"}
            ),
        })

        item = _FakeOutboxItem(
            id=1,
            session_id=session_id,
            destination={
                "channel_identifier": APP_ID,
                "channel_id": "C001",
                "thread_ts": "100.0",
            },
            payload={"content": "step", "intermediate": True},
        )
        delivery = _FakeDeliveryService(items=[item])
        result_ts = "222.0"
        platform = _FakeEditablePlatform(result=SendResult(success=True, message_id=result_ts))
        dispatcher = _make_live_dispatcher(platform=platform, delivery=delivery, redis=redis)

        await dispatcher._deliver_item(platform, item)

        # update_ts injected from the placeholder
        assert item.destination["update_ts"] == placeholder_ts
        # send was called
        assert len(platform.send_calls) == 1
        # clear_placeholder must NOT have been called (placeholder key still present or
        # replaced with the new ts — not deleted)
        ph = await read_placeholder(redis, "slack", session_id)
        assert ph is not None, "placeholder must still exist after an intermediate send"
        # set_placeholder updated it to the new ts
        assert ph["ts"] == result_ts
        # mark_delivered called
        assert delivery.delivered == [(1, result_ts)]
        assert delivery.failed == []

    async def test_slack_intermediate_no_placeholder_posts_fresh_and_sets_placeholder(self):
        """Intermediate + no placeholder → send without update_ts; set_placeholder called."""
        from surogates.channels.channel_progress import read_placeholder

        session_id = uuid4()
        redis = _FullRedis()  # empty — no placeholder

        item = _FakeOutboxItem(
            id=2,
            session_id=session_id,
            destination={
                "channel_identifier": APP_ID,
                "channel_id": "C001",
                "thread_ts": "100.0",
            },
            payload={"content": "step", "intermediate": True},
        )
        delivery = _FakeDeliveryService(items=[item])
        result_ts = "333.0"
        platform = _FakeEditablePlatform(result=SendResult(success=True, message_id=result_ts))
        dispatcher = _make_live_dispatcher(platform=platform, delivery=delivery, redis=redis)

        await dispatcher._deliver_item(platform, item)

        # no update_ts injected (no placeholder existed)
        assert "update_ts" not in item.destination
        # send was called
        assert len(platform.send_calls) == 1
        # set_placeholder called with the new ts
        ph = await read_placeholder(redis, "slack", session_id)
        assert ph is not None, "set_placeholder must have been called after intermediate send"
        assert ph["ts"] == result_ts
        assert delivery.delivered == [(2, result_ts)]

    async def test_slack_final_with_placeholder_sends_and_clears(self):
        """Final message + placeholder → send with update_ts; clear_placeholder IS called."""
        import json
        from surogates.channels.channel_progress import progress_key, read_placeholder

        session_id = uuid4()
        placeholder_ts = "444.0"
        redis = _FullRedis({
            progress_key("slack", session_id): json.dumps(
                {"channel": "C001", "ts": placeholder_ts, "thread_ts": "100.0"}
            ),
        })

        item = _FakeOutboxItem(
            id=3,
            session_id=session_id,
            destination={
                "channel_identifier": APP_ID,
                "channel_id": "C001",
                "thread_ts": "100.0",
            },
            payload={"content": "Here is the answer", "intermediate": False},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakeEditablePlatform(result=SendResult(success=True, message_id="555.0"))
        dispatcher = _make_live_dispatcher(platform=platform, delivery=delivery, redis=redis)

        await dispatcher._deliver_item(platform, item)

        assert item.destination["update_ts"] == placeholder_ts
        assert len(platform.send_calls) == 1
        # clear_placeholder must have been called on final message
        ph = await read_placeholder(redis, "slack", session_id)
        assert ph is None, "clear_placeholder must be called on the final message"
        assert delivery.delivered == [(3, "555.0")]

    async def test_non_edit_platform_intermediate_suppressed(self):
        """Intermediate on a platform without supports_edit → mark_delivered, no send."""
        session_id = uuid4()
        item = _FakeOutboxItem(
            id=4,
            session_id=session_id,
            destination={
                "channel_identifier": APP_ID,
                "channel_id": "C001",
            },
            payload={"content": "narration step", "intermediate": True},
        )
        delivery = _FakeDeliveryService(items=[item])
        # _FakePlatform has no supports_edit attribute
        platform = _FakePlatform(result=SendResult(success=True, message_id="x"))
        dispatcher = _make_live_dispatcher(platform=platform, delivery=delivery)

        await dispatcher._deliver_item(platform, item)

        # send must NOT be called for suppressed intermediates
        assert platform.send_calls == [], "send must not be called for suppressed intermediate"
        # mark_delivered must still be called (item consumed from outbox)
        assert len(delivery.delivered) == 1
        assert delivery.delivered[0][0] == 4
        assert delivery.failed == []

    async def test_slack_intermediate_throttle_coalesces_second_edit(self):
        """Two intermediate items back-to-back: the redis nx key is already set on the
        second → the second is mark_delivered without send."""
        session_id = uuid4()
        throttle_key = f"channel-progress-edit:slack:{session_id}"

        # Pre-seed the throttle key to simulate a recent-in-flight edit.
        redis = _FullRedis({throttle_key: "1"})

        item = _FakeOutboxItem(
            id=5,
            session_id=session_id,
            destination={
                "channel_identifier": APP_ID,
                "channel_id": "C001",
            },
            payload={"content": "another step", "intermediate": True},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakeEditablePlatform(result=SendResult(success=True, message_id="y"))
        dispatcher = _make_live_dispatcher(platform=platform, delivery=delivery, redis=redis)

        await dispatcher._deliver_item(platform, item)

        # Throttled: send must NOT be called
        assert platform.send_calls == [], "throttled intermediate must not call send"
        # mark_delivered still called (coalesced away)
        assert len(delivery.delivered) == 1
        assert delivery.delivered[0][0] == 5
        assert delivery.failed == []


class TestPermanentDeliveryFailures:

    async def test_permanent_result_error_marks_dead_not_failed(self):
        item = _FakeOutboxItem(id=71, destination={"channel_identifier": APP_ID})
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform(result=SendResult(
            success=False,
            error="The server responded with: {'ok': False, "
                  "'error': 'restricted_action_read_only_channel'}",
        ))
        dispatcher = _make_dispatcher(
            platform=platform, delivery=delivery, cache=_FakeCache(_KNOWN_CACHE),
        )
        await dispatcher.deliver_batch(platform)

        assert delivery.dead and delivery.dead[0][0] == 71
        assert delivery.failed == []  # not requeued

    async def test_permanent_raised_error_marks_dead(self):
        item = _FakeOutboxItem(id=72, destination={"channel_identifier": APP_ID})
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform(raises=RuntimeError(
            "chat_postMessage failed: ... 'error': 'restricted_action_read_only_channel'",
        ))
        dispatcher = _make_dispatcher(
            platform=platform, delivery=delivery, cache=_FakeCache(_KNOWN_CACHE),
        )
        await dispatcher.deliver_batch(platform)

        assert delivery.dead and delivery.dead[0][0] == 72
        assert delivery.failed == []

    async def test_transient_error_still_retries(self):
        item = _FakeOutboxItem(id=73, destination={"channel_identifier": APP_ID})
        delivery = _FakeDeliveryService(items=[item])
        platform = _FakePlatform(raises=RuntimeError("ratelimited"))
        dispatcher = _make_dispatcher(
            platform=platform, delivery=delivery, cache=_FakeCache(_KNOWN_CACHE),
        )
        await dispatcher.deliver_batch(platform)

        assert delivery.failed and delivery.failed[0][0] == 73
        assert delivery.dead == []

    async def test_exhausted_old_item_stops_retrying(self):
        # A transient error on an item older than the max delivery age is given
        # up on (marked dead) instead of retried forever.
        old = _FakeOutboxItem(
            id=74, destination={"channel_identifier": APP_ID},
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        delivery = _FakeDeliveryService(items=[old])
        platform = _FakePlatform(raises=RuntimeError("ratelimited"))
        dispatcher = _make_dispatcher(
            platform=platform, delivery=delivery, cache=_FakeCache(_KNOWN_CACHE),
        )
        await dispatcher.deliver_batch(platform)

        assert delivery.dead and delivery.dead[0][0] == 74
        assert delivery.failed == []


class _CodeRunRedis:
    """A get/set/delete-only Redis fake for the code-run ts handshake.

    (Distinct from the fuller ``_FakeRedis`` above so it doesn't shadow it —
    the bot-message tests need ``exists``/``sadd`` which this does not provide;
    the code-run path best-effort-swallows those missing methods.)
    """

    def __init__(self) -> None:
        self.kv: dict[str, Any] = {}

    async def get(self, k: str) -> Any:
        return self.kv.get(k)

    async def set(self, k: str, v: Any, ex: int | None = None) -> None:
        self.kv[k] = v

    async def delete(self, *ks: str) -> None:
        for k in ks:
            self.kv.pop(k, None)


class _EditPlatform(_FakePlatform):
    """Slack-like: edits when destination carries update_ts, else posts fresh."""

    supports_edit = True

    def __init__(self) -> None:
        super().__init__()
        self.edits: list[str | None] = []

    async def send(self, item: Any, *, creds: dict) -> SendResult:
        seen = item.destination.get("update_ts")
        self.edits.append(seen)
        # A fresh post returns a new ts; an edit keeps the same message.
        return SendResult(success=True, message_id=seen or "posted-ts-1")


class TestCodeRunEditInPlace:
    async def test_first_heartbeat_posts_then_subsequent_edit(self):
        session_id = uuid4()
        dest = {"channel_identifier": APP_ID, "channel_id": "C001"}
        payload = {"content": "🛠️ Still working…", "code_run": "run7"}
        items = [
            _FakeOutboxItem(id=1, session_id=session_id, dedupe_key="slack:1",
                            destination=dict(dest), payload=dict(payload)),
            _FakeOutboxItem(id=2, session_id=session_id, dedupe_key="slack:2",
                            destination=dict(dest), payload=dict(payload)),
        ]
        delivery = _FakeDeliveryService(items=items)
        platform = _EditPlatform()
        redis = _CodeRunRedis()
        dispatcher = _make_dispatcher(
            platform=platform, delivery=delivery,
            cache=_FakeCache(_KNOWN_CACHE), redis=redis,
        )

        await dispatcher.deliver_batch(platform)

        # First posts (no update_ts), second edits the recorded message.
        assert platform.edits == [None, "posted-ts-1"]
        # The run's message ts is recorded in Redis for the next heartbeat.
        from surogates.channels.channel_progress import code_run_key
        assert code_run_key("slack", session_id, "run7") in redis.kv

    async def test_second_run_gets_its_own_message(self):
        session_id = uuid4()
        dest = {"channel_identifier": APP_ID, "channel_id": "C001"}
        items = [
            _FakeOutboxItem(id=1, session_id=session_id, dedupe_key="slack:1",
                            destination=dict(dest),
                            payload={"content": "a", "code_run": "runA"}),
            _FakeOutboxItem(id=2, session_id=session_id, dedupe_key="slack:2",
                            destination=dict(dest),
                            payload={"content": "b", "code_run": "runB"}),
        ]
        platform = _EditPlatform()
        dispatcher = _make_dispatcher(
            platform=platform, delivery=_FakeDeliveryService(items=items),
            cache=_FakeCache(_KNOWN_CACHE), redis=_CodeRunRedis(),
        )
        await dispatcher.deliver_batch(platform)
        # Neither edits — each run posts its own fresh main message.
        assert platform.edits == [None, None]


class _NoSendFilesPlatform(_FakePlatform):
    """A platform that cannot upload — the negative side of the gate.

    Telegram is this shape today: no ``send_files``, no ``supports_edit``.
    """


class _NoDeletePlatform(_FakeMediaPlatform):
    """``send_files`` but no ``supports_edit`` and no ``delete_message``.

    WhatsApp's exact combination, and one no platform had before it. The
    marker-only branch reaches for ``delete_message`` to clear a thinking
    placeholder; with none posted there is nothing to clear, and the leg
    must stay inert rather than raising.
    """

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        # Undo the base class's delete_message so getattr() finds nothing.
        self.delete_message = None


class TestMediaGateIsCapabilityBased:
    async def test_platform_without_send_files_keeps_the_raw_marker(self):
        """A marker must never be stripped from a platform that cannot upload
        it — the user would lose the file reference entirely."""
        item = _FakeOutboxItem(
            id=10,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
            payload={"content": "Here it is. MEDIA:/workspace/report.pdf"},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _NoSendFilesPlatform()
        storage = _MediaStorage({"report.pdf": b"%PDF data"})
        store = _FakeSessionStore(_MediaSession())

        dispatcher = _media_dispatcher(platform, delivery, storage, store)
        await dispatcher.deliver_batch(platform)

        sent_item, _ = platform.send_calls[0]
        assert sent_item.payload["content"] == (
            "Here it is. MEDIA:/workspace/report.pdf"
        )

    async def test_send_files_without_delete_message_is_inert_not_an_error(self):
        """The marker-only branch must not require delete_message."""
        item = _FakeOutboxItem(
            id=11,
            destination={"channel_identifier": APP_ID, "channel_id": "C001"},
            payload={"content": "MEDIA:/workspace/only.pdf"},
        )
        delivery = _FakeDeliveryService(items=[item])
        platform = _NoDeletePlatform(uploaded_ids=["FA2"])
        storage = _MediaStorage({"only.pdf": b"data"})
        store = _FakeSessionStore(_MediaSession())

        dispatcher = _media_dispatcher(platform, delivery, storage, store)
        await dispatcher.deliver_batch(platform)

        assert platform.send_calls == []
        assert len(platform.send_files_calls) == 1
        assert delivery.delivered == [(11, "FA2")]
