"""A browser lives while it is used, not for an hour from its start.

Pods were killed 3600 s after they started, which took an active chat's
browser at 60 minutes and kept a finished run's up to an hour.  A run's
browser now closes when its turn ends, and any browser closes when no tool
used it for half an hour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from surogates.browser.registry import BrowserEntry
from surogates.harness.loop_artifact_completion import ArtifactCompletionMixin
from surogates.jobs.browser_reaper import IDLE_CLOSE_SECONDS, close_idle_browsers

NOW = datetime.now(timezone.utc)


def _entry(sid: str, *, provisioned: timedelta, used: timedelta | None) -> BrowserEntry:
    return BrowserEntry(
        session_id=sid, org_id="o", user_id="u", rest_url="http://b:1", cdp_url="ws://b:2",
        live_view_url="ws://b:3", provisioned_at=NOW - provisioned, browser_id=f"pod-{sid}",
        last_used_at=None if used is None else NOW - used,
    )


def test_an_entry_keeps_its_last_use_through_the_registry():
    entry = _entry("s", provisioned=timedelta(hours=2), used=timedelta(minutes=1))
    assert BrowserEntry.from_json(entry.to_json()) == entry
    legacy = entry.to_json().replace('"last_used_at"', '"ignored"')
    assert BrowserEntry.from_json(legacy).last_used_at is None


class _Registry:
    def __init__(self, entries):
        self._entries = {e.session_id: e for e in entries}

    async def entries(self):
        return dict(self._entries)


async def test_only_a_browser_nobody_used_for_half_an_hour_is_closed():
    registry = _Registry([
        _entry("long-chat", provisioned=timedelta(hours=3), used=timedelta(minutes=2)),
        _entry("left", provisioned=timedelta(hours=1), used=timedelta(minutes=45)),
        _entry("old-entry", provisioned=timedelta(hours=1), used=None),
        _entry("new", provisioned=timedelta(minutes=5), used=None),
    ])
    closed = []

    async def close(session_id):
        closed.append(session_id)

    assert await close_idle_browsers(registry=registry, close=close) == 2
    assert sorted(closed) == ["left", "old-entry"]
    assert IDLE_CLOSE_SECONDS == 30 * 60


async def test_one_browser_that_will_not_close_does_not_keep_the_rest_open():
    registry = _Registry([
        _entry("stuck", provisioned=timedelta(hours=1), used=None),
        _entry("left", provisioned=timedelta(hours=1), used=None),
    ])
    closed = []

    async def close(session_id):
        if session_id == "stuck":
            raise RuntimeError("backend down")
        closed.append(session_id)

    assert await close_idle_browsers(registry=registry, close=close) == 1
    assert closed == ["left"]


class _Completion(ArtifactCompletionMixin):
    def __init__(self, pool):
        self._browser_pool = pool


@pytest.mark.parametrize(
    ("channel", "closed"),
    [("scheduled", True), ("delegation", True), ("worker", True), ("task", True), ("studio", False), ("slack", False)],
)
async def test_a_runs_or_helpers_browser_closes_when_its_turn_ends(channel, closed):
    destroyed = []

    async def destroy_for_session(session_id):
        destroyed.append(session_id)

    pool = SimpleNamespace(destroy_for_session=destroy_for_session)
    await _Completion(pool)._release_unattended_browser(SimpleNamespace(id="s1", channel=channel))
    assert destroyed == (["s1"] if closed else [])
