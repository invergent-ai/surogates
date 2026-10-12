"""The listeners a process keeps for its whole life outlive a Redis restart.

A Valkey restart resets every connection: a listener that ends on the
error leaves its process deaf to that channel until the pod restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from surogates.channels.dispatcher import ChannelWebhookReconciler
from surogates.channels.registry import ChannelRegistry
from surogates.orchestrator.dispatcher import Orchestrator
from surogates.runtime.invalidator import _CHANNEL_ROUTING, run_invalidator


class _FakePubSub:
    """One pub/sub connection: ``listen`` yields *messages*, then raises *error*."""

    def __init__(self, messages: list[dict], error: Exception | None) -> None:
        self._messages = messages
        self._error = error
        self.closed = False

    async def psubscribe(self, *_patterns: str) -> None:
        pass

    async def aclose(self) -> None:
        self.closed = True

    async def listen(self):
        for message in self._messages:
            yield message
        if self._error is not None:
            raise self._error
        await asyncio.Event().wait()


def _message(channel: str, data: bytes = b"") -> dict:
    return {"type": "pmessage", "pattern": None, "channel": channel.encode(), "data": data}


def _redis_reset_once(channel: str, data: bytes = b"") -> SimpleNamespace:
    """A Redis whose first subscription is reset, and whose second hears *channel*."""
    pubsubs = [
        _FakePubSub([], RedisConnectionError("Connection reset by peer")),
        _FakePubSub([_message(channel, data)], None),
    ]
    redis = SimpleNamespace(opened=[])

    def pubsub() -> _FakePubSub:
        redis.opened.append(pubsubs.pop(0))
        return redis.opened[-1]

    redis.pubsub = pubsub
    return redis


async def _until(heard: asyncio.Event, task: asyncio.Task) -> None:
    try:
        await asyncio.wait_for(heard.wait(), timeout=5)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, RedisConnectionError):
            await task


async def test_the_interrupt_listener_hears_a_stop_after_a_redis_restart() -> None:
    session_id = uuid4()
    heard = asyncio.Event()
    received: list[tuple] = []
    redis = _redis_reset_once(f"surogates:interrupt:{session_id}", b'{"reason": "session deleted"}')
    orchestrator = Orchestrator(
        redis_client=redis, session_store=object(), harness_factory=lambda _sid: None,
        agent_id="support-bot", queue_key="surogates:work_queue:support-bot", max_concurrent=1,
    )

    async def handle(sid, reason) -> None:
        received.append((sid, reason))
        heard.set()

    orchestrator._handle_interrupt_signal = handle
    await _until(heard, asyncio.create_task(orchestrator._listen_for_interrupts()))

    assert received == [(session_id, "session deleted")]
    assert [pubsub.closed for pubsub in redis.opened] == [True, True]


async def test_the_cache_invalidator_hears_a_change_after_a_redis_restart() -> None:
    heard = asyncio.Event()
    invalidated: list[str] = []

    def invalidate(identifier: str) -> None:
        invalidated.append(identifier)
        heard.set()

    redis = _redis_reset_once("agent.runtime_config_changed:agent-1")
    task = asyncio.create_task(run_invalidator(redis, runtime_config_cache=SimpleNamespace(invalidate=invalidate)))
    await _until(heard, task)

    assert invalidated == ["agent-1"]


@pytest.mark.parametrize(("prefix", "cache_kwarg"), _CHANNEL_ROUTING)
async def test_the_cache_invalidator_hands_every_channel_to_its_cache(prefix: str, cache_kwarg: str) -> None:
    heard = asyncio.Event()
    invalidated: list[str] = []

    def invalidate(identifier: str) -> None:
        invalidated.append(identifier)
        heard.set()

    redis = SimpleNamespace(pubsub=lambda: _FakePubSub([_message(f"{prefix}id-1")], None))
    task = asyncio.create_task(run_invalidator(redis, **{cache_kwarg: SimpleNamespace(invalidate=invalidate)}))
    await _until(heard, task)

    assert invalidated == ["id-1"]


async def test_the_webhook_reconciler_hears_a_routing_change_after_a_redis_restart() -> None:
    heard = asyncio.Event()
    changed: list[tuple[str, str]] = []
    reconciler = ChannelWebhookReconciler(
        platform_client=None, vault=None, public_url="https://channels.surogate.ai",
        settings=SimpleNamespace(channels={}), registry=ChannelRegistry(),
    )

    async def handle_routing_change(kind: str, identifier: str) -> None:
        changed.append((kind, identifier))
        heard.set()

    reconciler.handle_routing_change = handle_routing_change
    task = asyncio.create_task(reconciler.run(_redis_reset_once("channel_routing_changed:telegram:@bot")))
    await _until(heard, task)

    assert changed == [("telegram", "@bot")]
