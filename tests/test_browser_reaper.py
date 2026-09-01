"""The registry reaper: dead browsers are pruned and their sessions told.

Browsers die without the server hearing — a host-level kill leaves the
registry entry behind, ``browser.destroyed`` never fires, and every consumer
then discovers the corpse separately: the preview by erroring, the shell by
timing out, the card by lying. One sweeper owns the truth instead: probe each
entry, prune what is provably gone, and emit the missing event so every
existing consumer — reducer, card, pane — corrects itself through the event
stream it already reads.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from surogates.browser.registry import BrowserEntry
from surogates.jobs.browser_reaper import (
    FAILURES_BEFORE_PRUNE,
    FAILURES_KEY,
    sweep_browser_registry,
)


def _entry(session_id: str, *, age_seconds: float = 600.0) -> BrowserEntry:
    return BrowserEntry(
        session_id=session_id,
        org_id="org-1",
        user_id="",
        rest_url=f"http://127.0.0.1:3000{session_id[-1]}",
        cdp_url=f"ws://127.0.0.1:3100{session_id[-1]}",
        live_view_url=f"ws://127.0.0.1:3200{session_id[-1]}",
        provisioned_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        browser_id=f"container-{session_id}",
    )


class FakeRegistry:
    def __init__(self, entries: dict[str, BrowserEntry]) -> None:
        self._entries = dict(entries)
        self.deleted: list[str] = []

    async def entries(self) -> dict[str, BrowserEntry]:
        return dict(self._entries)

    async def delete(self, session_id: str) -> None:
        self.deleted.append(session_id)
        self._entries.pop(session_id, None)


class FakeRedis:
    """Just the hash ops the failure counter uses."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, int]] = {}

    async def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + amount
        return bucket[field]

    async def hdel(self, key: str, *fields: str) -> int:
        bucket = self.hashes.get(key, {})
        removed = 0
        for field in fields:
            if field in bucket:
                del bucket[field]
                removed += 1
        return removed


def _emit_recorder():
    events: list[tuple[str, str, dict]] = []

    async def emit(session_id: str, event_type: str, data: dict) -> None:
        events.append((session_id, event_type, data))

    return emit, events


async def test_one_failed_probe_is_not_a_verdict() -> None:
    # A single failed dial could be the host having a moment; pruning on it
    # would delete live browsers, which is the bug this design replaces.
    registry = FakeRegistry({"s-1": _entry("s-1")})
    redis = FakeRedis()
    emit, events = _emit_recorder()

    async def probe(_entry: BrowserEntry) -> bool:
        return False

    pruned = await sweep_browser_registry(
        registry=registry, redis=redis, emit=emit, probe=probe,
    )

    assert pruned == 0
    assert registry.deleted == []
    assert events == []
    assert redis.hashes[FAILURES_KEY]["s-1"] == 1


async def test_repeated_failure_prunes_and_tells_the_session() -> None:
    registry = FakeRegistry({"s-1": _entry("s-1")})
    redis = FakeRedis()
    emit, events = _emit_recorder()

    async def probe(_entry: BrowserEntry) -> bool:
        return False

    for _ in range(FAILURES_BEFORE_PRUNE):
        await sweep_browser_registry(
            registry=registry, redis=redis, emit=emit, probe=probe,
        )

    assert registry.deleted == ["s-1"]
    # The emit is the point: browser.destroyed reaches the session's event
    # stream, and the card, pane and reducer already know what to do with it.
    assert len(events) == 1
    session_id, event_type, data = events[0]
    assert session_id == "s-1"
    assert event_type == "browser.destroyed"
    assert data["browser_id"] == "container-s-1"
    # The counter does not linger for a session that no longer has an entry.
    assert "s-1" not in redis.hashes.get(FAILURES_KEY, {})


async def test_a_live_browser_clears_its_strikes() -> None:
    registry = FakeRegistry({"s-1": _entry("s-1")})
    redis = FakeRedis()
    redis.hashes[FAILURES_KEY] = {"s-1": 1}
    emit, events = _emit_recorder()

    async def probe(_entry: BrowserEntry) -> bool:
        return True

    pruned = await sweep_browser_registry(
        registry=registry, redis=redis, emit=emit, probe=probe,
    )

    assert pruned == 0
    assert registry.deleted == []
    assert events == []
    # An earlier blip must not carry over: two failures a week apart are not
    # "two consecutive failures".
    assert "s-1" not in redis.hashes.get(FAILURES_KEY, {})


async def test_fresh_entries_are_left_alone() -> None:
    # A browser inside its startup window can legitimately refuse the dial
    # (REST is waited on, CDP arrives later); reaping it would kill every
    # provision from under its first tool call.
    registry = FakeRegistry({"s-1": _entry("s-1", age_seconds=5.0)})
    redis = FakeRedis()
    emit, events = _emit_recorder()
    probed: list[str] = []

    async def probe(entry: BrowserEntry) -> bool:
        probed.append(entry.session_id)
        return False

    pruned = await sweep_browser_registry(
        registry=registry, redis=redis, emit=emit, probe=probe,
    )

    assert pruned == 0
    assert probed == []
    assert events == []


async def test_one_dead_browser_does_not_shadow_a_live_one() -> None:
    registry = FakeRegistry({
        "s-1": _entry("s-1"),
        "s-2": _entry("s-2"),
    })
    redis = FakeRedis()
    redis.hashes[FAILURES_KEY] = {"s-1": FAILURES_BEFORE_PRUNE - 1}
    emit, events = _emit_recorder()

    async def probe(entry: BrowserEntry) -> bool:
        return entry.session_id == "s-2"

    pruned = await sweep_browser_registry(
        registry=registry, redis=redis, emit=emit, probe=probe,
    )

    assert pruned == 1
    assert registry.deleted == ["s-1"]
    assert [e[0] for e in events] == ["s-1"]


async def test_a_failing_emit_does_not_stop_the_prune_or_the_sweep() -> None:
    # The entry is provably wrong either way, and the next session to sweep
    # must still get its turn.
    registry = FakeRegistry({
        "s-1": _entry("s-1"),
        "s-2": _entry("s-2"),
    })
    redis = FakeRedis()
    redis.hashes[FAILURES_KEY] = {
        "s-1": FAILURES_BEFORE_PRUNE - 1,
        "s-2": FAILURES_BEFORE_PRUNE - 1,
    }

    async def emit(_session_id: str, _event_type: str, _data: dict) -> None:
        raise RuntimeError("event store is down")

    async def probe(_entry: BrowserEntry) -> bool:
        return False

    pruned = await sweep_browser_registry(
        registry=registry, redis=redis, emit=emit, probe=probe,
    )

    assert pruned == 2
    assert sorted(registry.deleted) == ["s-1", "s-2"]
