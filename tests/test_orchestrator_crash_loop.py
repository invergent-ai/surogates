"""Crash-loop circuit breaker: repeated identical harness crashes must stop
re-dispatch instead of replaying a poisoned session for hours.

Regression suite for the PROD incident where a session crashed 40 times over
~9 hours (9.2M input tokens): every wake failed with the same provider 400,
the dispatcher's retry counter reset on each re-enqueue, and synthetic
mission-continuation messages kept re-waking the failed session.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.orchestrator.dispatcher import (
    _CRASH_LOOP_KEY_PREFIX,
    _CRASH_LOOP_THRESHOLD,
    Orchestrator,
)
from surogates.session.events import EventType


class FakeRedis:
    """Stateful in-memory stand-in for the few redis ops the breaker uses."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.zadds: list[tuple[str, dict]] = []

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.data.pop(key, None)

    async def zadd(self, key: str, mapping: dict) -> None:
        self.zadds.append((key, mapping))


class FakeStore:
    def __init__(self) -> None:
        self.emitted: list[tuple] = []
        self.statuses: list[tuple] = []
        self.events_since_trip: list = []
        self._next_event_id = 100

    async def emit_event(self, session_id, event_type, data) -> int:
        self.emitted.append((session_id, event_type, data))
        self._next_event_id += 1
        return self._next_event_id

    async def update_session_status(self, session_id, status) -> None:
        self.statuses.append((session_id, status))

    async def get_events(self, session_id, *, after=None, types=None, limit=None):
        return self.events_since_trip


def _make_orchestrator(redis, store, harness_factory) -> Orchestrator:
    return Orchestrator(
        redis_client=redis,
        session_store=store,
        harness_factory=harness_factory,
        agent_id="support-bot",
        queue_key="surogates:work_queue:support-bot",
        max_concurrent=1,
    )


def _session_fails(store: FakeStore) -> list[dict]:
    return [
        data for (_sid, etype, data) in store.emitted
        if etype == EventType.SESSION_FAIL
    ]


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher._BASE_RETRY_DELAY", 0,
    )


async def _trip_breaker(session_id, redis, store) -> tuple[Orchestrator, list]:
    """Drive _process with an always-identically-crashing harness until trip."""
    wakes: list[int] = []

    class CrashingHarness:
        async def wake(self, _sid):
            wakes.append(1)
            raise ValueError(
                "Invalid LLM response: response is None or has no choices"
            )

    orchestrator = _make_orchestrator(redis, store, lambda _sid: CrashingHarness())
    await orchestrator._process(session_id)
    return orchestrator, wakes


async def test_identical_crashes_trip_breaker_and_mark_terminal() -> None:
    session_id = uuid4()
    redis = FakeRedis()
    store = FakeStore()

    _, wakes = await _trip_breaker(session_id, redis, store)

    # The dispatcher's own retries produce exactly the threshold of
    # identical crashes; the trip replaces the max_retries_exhausted fail.
    assert len(wakes) == _CRASH_LOOP_THRESHOLD
    fails = _session_fails(store)
    assert len(fails) == 1
    assert fails[0]["reason"] == "crash_loop_detected"
    assert fails[0]["retryable"] is False
    assert store.statuses == [(session_id, "failed")]

    state = json.loads(redis.data[f"{_CRASH_LOOP_KEY_PREFIX}{session_id}"])
    assert state["tripped"] is True


async def test_tripped_breaker_blocks_redispatch_without_user_signal() -> None:
    session_id = uuid4()
    redis = FakeRedis()
    store = FakeStore()
    orchestrator, wakes = await _trip_breaker(session_id, redis, store)
    wakes.clear()
    store.events_since_trip = []  # nothing happened since the trip

    await orchestrator._process(session_id)

    assert wakes == []
    assert len(_session_fails(store)) == 1  # no new failure emitted


async def test_synthetic_continuation_does_not_clear_breaker() -> None:
    """The mission evaluator's synthetic user.message must NOT re-arm wakes.

    This is the exact mechanism that re-woke the PROD session all night.
    """
    session_id = uuid4()
    redis = FakeRedis()
    store = FakeStore()
    orchestrator, wakes = await _trip_breaker(session_id, redis, store)
    wakes.clear()
    store.events_since_trip = [
        SimpleNamespace(
            id=200,
            type=EventType.USER_MESSAGE.value,
            data={"content": "continue", "synthetic": "mission_continuation"},
        ),
    ]

    await orchestrator._process(session_id)

    assert wakes == []


async def test_real_user_message_clears_breaker() -> None:
    session_id = uuid4()
    redis = FakeRedis()
    store = FakeStore()
    orchestrator, wakes = await _trip_breaker(session_id, redis, store)
    wakes.clear()
    store.events_since_trip = [
        SimpleNamespace(
            id=200,
            type=EventType.USER_MESSAGE.value,
            data={"content": "try a different approach"},
        ),
    ]

    await orchestrator._process(session_id)

    assert len(wakes) > 0  # dispatch resumed


async def test_user_retry_resume_clears_breaker() -> None:
    session_id = uuid4()
    redis = FakeRedis()
    store = FakeStore()
    orchestrator, wakes = await _trip_breaker(session_id, redis, store)
    wakes.clear()
    store.events_since_trip = [
        SimpleNamespace(
            id=200,
            type=EventType.SESSION_RESUME.value,
            data={"source": "user_retry"},
        ),
    ]

    await orchestrator._process(session_id)

    assert len(wakes) > 0


async def test_different_crash_fingerprints_use_normal_retry_path() -> None:
    """Distinct consecutive errors are not a crash loop."""
    session_id = uuid4()
    redis = FakeRedis()
    store = FakeStore()
    errors = [
        ValueError("first failure mode"),
        TimeoutError("second failure mode"),
        RuntimeError("third failure mode"),
    ]
    wakes: list[int] = []

    class VaryingCrashHarness:
        async def wake(self, _sid):
            wakes.append(1)
            raise errors[len(wakes) - 1]

    orchestrator = _make_orchestrator(
        redis, store, lambda _sid: VaryingCrashHarness(),
    )
    await orchestrator._process(session_id)

    fails = _session_fails(store)
    assert len(fails) == 1
    assert fails[0]["reason"] == "max_retries_exhausted"
    state = json.loads(redis.data[f"{_CRASH_LOOP_KEY_PREFIX}{session_id}"])
    assert not state.get("tripped")


async def test_successful_wake_clears_crash_count() -> None:
    session_id = uuid4()
    redis = FakeRedis()
    store = FakeStore()
    wakes: list[int] = []

    class FlakyThenHealthyHarness:
        async def wake(self, _sid):
            wakes.append(1)
            if len(wakes) == 1:
                raise ValueError("transient blip")
            return None

    orchestrator = _make_orchestrator(
        redis, store, lambda _sid: FlakyThenHealthyHarness(),
    )
    await orchestrator._process(session_id)

    assert len(wakes) == 2
    assert _session_fails(store) == []
    assert f"{_CRASH_LOOP_KEY_PREFIX}{session_id}" not in redis.data
