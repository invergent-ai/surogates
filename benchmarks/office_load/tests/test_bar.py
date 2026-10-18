"""The bar a step is judged by: each row at its threshold and just past it, over the hold period."""

from __future__ import annotations

import math

import pytest

from oload.bar import judge
from oload.events import Event

HOLD = 1_000_000.0  # the hold period's start, time.time()
IN_HOLD = HOLD + 30
RAMP_UP = HOLD - 30


def ev(what: str, seconds: float = 1.0, *, ok: bool = True, cause: str | None = None, at: float = IN_HOLD,
       session: int = 0) -> Event:
    return Event(session, "docx", what, ok, seconds, cause, at)  # type: ignore[arg-type]


def timed(what: str, values: list[float]) -> list[Event]:
    return [ev(what, seconds, session=i) for i, seconds in enumerate(values)]


def healthy() -> list[Event]:
    """A step's events, every row well inside the bar: 100 sessions, a turn each."""
    events = timed("open", [3.0] * 100)
    for what, seconds in (("edit", 2.0), ("flush", 0.5), ("forcesave", 0.5), ("callback", 2.0), ("marker", 0.1)):
        events += timed(what, [seconds] * 100)
    return events


def rows(events: list[Event]) -> list[str]:
    """The rows of the bar *events* break, each named once."""
    return sorted({broken.split(":")[0] for broken in judge(events, HOLD).broken})


def test_a_healthy_step_holds() -> None:
    verdict = judge(healthy(), HOLD)
    assert verdict.holds and verdict.broken == []
    assert not verdict.client_limited and not verdict.invalid


# Forcesave: p99 under 5 s, none over 10 s.

@pytest.mark.parametrize(("p99", "broken"), [(4.9, []), (5.0, ["forcesave"]), (5.1, ["forcesave"])])
def test_forcesave_p99_is_under_5_s(p99: float, broken: list[str]) -> None:
    assert rows(timed("forcesave", [1.0] * 98 + [p99] * 2)) == broken


@pytest.mark.parametrize(("longest", "broken"), [(10.0, []), (10.1, ["forcesave"])])
def test_no_forcesave_over_10_s(longest: float, broken: list[str]) -> None:
    events = timed("forcesave", [1.0] * 200 + [longest])
    assert judge(events, HOLD).stats["forcesave"]["p99"] == 1.0  # only the longest one is past the bar
    assert rows(events) == broken


def test_a_forcesave_that_timed_out_breaks() -> None:
    assert rows(healthy() + [ev("forcesave", 10.0, ok=False, cause="timeout")]) == ["forcesave"]


# Save callback: p95 at most 10 s, p99 at most 30 s.

@pytest.mark.parametrize(("p95", "broken"), [(10.0, []), (10.1, ["callback"])])
def test_callback_p95_is_at_most_10_s(p95: float, broken: list[str]) -> None:
    assert rows(timed("callback", [2.0] * 94 + [p95] * 6)) == broken


@pytest.mark.parametrize(("p99", "broken"), [(30.0, []), (30.1, ["callback"])])
def test_callback_p99_is_at_most_30_s(p99: float, broken: list[str]) -> None:
    assert rows(timed("callback", [2.0] * 98 + [p99] * 2)) == broken


@pytest.mark.parametrize(("late", "broken"), [(1, []), (2, ["callback"])])
def test_a_callback_that_never_came_counts_past_the_wait(late: int, broken: list[str]) -> None:
    """The harness waits 30 s: one in a hundred may come later, so it is no lost save, but its p99 counts it."""
    events = timed("callback", [2.0] * (100 - late)) + [ev("callback", 31.0, ok=False, cause="timeout")] * late
    verdict = judge(events, HOLD)
    assert rows(events) == broken
    assert verdict.stats["callback"]["max"] == math.inf


# Flush: always acknowledged within 10 s.

@pytest.mark.parametrize(("longest", "broken"), [(10.0, []), (10.1, ["flush"])])
def test_every_flush_is_acknowledged_within_10_s(longest: float, broken: list[str]) -> None:
    assert rows(timed("flush", [0.5] * 200 + [longest])) == broken


def test_a_flush_not_acknowledged_breaks() -> None:
    assert rows(healthy() + [ev("flush", 0.3, ok=False, cause="server")]) == ["flush"]


# Document open: p95 at most 15 s, no failures.

@pytest.mark.parametrize(("p95", "broken"), [(15.0, []), (15.1, ["open"])])
def test_open_p95_is_at_most_15_s(p95: float, broken: list[str]) -> None:
    assert rows(timed("open", [3.0] * 94 + [p95] * 6)) == broken


# One failure breaks: an open, a lost save, an editor error.

@pytest.mark.parametrize(("failure", "row"), [
    (ev("open", 2.0, ok=False, cause="server"), "open"),
    (ev("open", 90.0, ok=False, cause="timeout"), "open"),
    (ev("marker", 0.1, ok=False, cause="lost"), "lost saves"),  # the save lacks the turn's last edit
    (ev("forcesave", 0.2, ok=False, cause="lost"), "lost saves"),  # nothing to save after the turn edited
    (ev("callback", 3.0, ok=False, cause="server"), "lost saves"),  # the save failed, or its file did not come
    (ev("edit", 1.0, ok=False, cause="server"), "editor errors"),  # not applied, or the page gone
    (ev("edit", 300.0, ok=False, cause="timeout"), "editor errors"),
])
def test_one_failure_breaks(failure: Event, row: str) -> None:
    verdict = judge(healthy() + [failure], HOLD)
    assert not verdict.holds and rows(healthy() + [failure]) == [row]
    assert not verdict.client_limited


# The hold period.

def test_the_ramp_up_is_not_judged() -> None:
    ramp_up = [ev("forcesave", 12.0, at=RAMP_UP), ev("marker", 0.1, ok=False, cause="lost", at=RAMP_UP),
               ev("edit", 1.0, ok=False, cause="server", at=RAMP_UP), ev("callback", 40.0, at=RAMP_UP)]
    verdict = judge(healthy() + ramp_up, HOLD)
    assert verdict.holds
    assert verdict.stats["forcesave"]["max"] == 0.5 and verdict.stats["failures"]["lost"] == 0


def test_opens_are_judged_through_the_ramp_up_where_every_session_opens() -> None:
    assert rows(healthy() + [ev("open", 2.0, ok=False, cause="server", at=RAMP_UP)]) == ["open"]
    assert rows(timed("open", [3.0] * 94) + [ev("open", 20.0, at=RAMP_UP)] * 6) == ["open"]


def test_a_marker_check_is_the_tools_own_time() -> None:
    verdict = judge(healthy() + timed("marker", [60.0] * 100), HOLD)
    assert verdict.holds
    assert "marker" not in verdict.stats


# Whose failure: the load clients' own, or the tool's.

def test_only_client_failures_make_a_client_limited_step() -> None:
    clients = [ev("open", 0.1, ok=False, cause="client", at=RAMP_UP), ev("edit", 0.1, ok=False, cause="client"),
               ev("flush", 0.1, ok=False, cause="client")]
    verdict = judge(healthy() + clients, HOLD)
    assert not verdict.holds and verdict.client_limited
    assert rows(healthy() + clients) == ["editor errors", "flush", "open"]


@pytest.mark.parametrize("also", [
    [ev("marker", 0.1, ok=False, cause="lost")],  # a failure of the document server's as well
    timed("forcesave", [6.0] * 10),  # a row broken by its times, which no client failure explains
])
def test_a_client_failure_does_not_excuse_another_break(also: list[Event]) -> None:
    verdict = judge(healthy() + [ev("edit", 0.1, ok=False, cause="client")] + also, HOLD)
    assert not verdict.holds and not verdict.client_limited


def test_a_step_with_a_tool_error_is_invalid_wherever_it_came() -> None:
    for at in (IN_HOLD, RAMP_UP):
        verdict = judge(healthy() + [Event(3, "pptx", "error", False, 0.0, "tool", at, "400: bad op")], HOLD)
        assert verdict.invalid and not verdict.holds and not verdict.client_limited
        assert verdict.reason is not None and "400: bad op" in verdict.reason


# The numbers kept.

def test_stats() -> None:
    events = timed("open", [float(s) for s in range(1, 101)]) + [
        ev("forcesave", 0.4), ev("forcesave", 10.0, ok=False, cause="timeout"),
        ev("callback", 2.0), ev("callback", 31.0, ok=False, cause="timeout"),
        ev("marker", 0.1, ok=False, cause="lost"), ev("edit", 1.0, ok=False, cause="client"),
        ev("open", 0.1, ok=False, cause="server"),
    ]
    stats = judge(events, HOLD).stats
    assert stats["open"] == {"n": 100, "p50": 50.0, "p95": 95.0, "p99": 99.0, "max": 100.0}
    assert stats["forcesave"] == {"n": 1, "p50": 0.4, "p95": 0.4, "p99": 0.4, "max": 0.4}  # answered ones
    assert stats["callback"]["n"] == 2 and stats["callback"]["p50"] == 2.0 and stats["callback"]["max"] == math.inf
    assert "edit" not in stats and "flush" not in stats  # none answered
    assert stats["events"] == 107
    assert stats["failures"] == {"lost": 1, "timeout": 2, "server": 1, "client": 1, "tool": 0}
