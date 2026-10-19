"""The bar a step holds to: every save lands, each call answers in its time, and nothing fails.

A step is judged on its hold period, the steady part from the last session's start to the step's deadline. Opens are
the exception: every session opens while the step ramps up, so its opens are judged wherever they came. Past the
deadline only the saves sent before it are judged: a turn whose forcesave had not gone is abandoned, its events marked
so.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, get_args

from oload.events import Cause, Event

# The bar's rows, as a broken one is named.
LOST = "lost saves"
FORCESAVE = "forcesave"
CALLBACK = "callback"
FLUSH = "flush"
OPEN = "open"
EDITOR = "editor errors"

FORCESAVE_P99_UNDER_S = 5.0
FORCESAVE_MAX_S = 10.0  # the harness gives up at 10 s and never retries
CALLBACK_P95_MAX_S = 10.0
CALLBACK_P99_MAX_S = 30.0  # the harness waits 30 s
FLUSH_MAX_S = 10.0  # otherwise the harness sends no forcesave
OPEN_P95_MAX_S = 15.0

# The calls whose times the stats keep. A marker check's time is the tool's own parsing, so not among them.
TIMED = ("open", "edit", "flush", "forcesave", "callback")


@dataclass(frozen=True, slots=True)
class Verdict:
    holds: bool
    broken: list[str]  # each broken row: its name, a colon, what broke it
    client_limited: bool  # it broke, and only on the load clients' own failures: no finding about the server
    stats: dict[str, Any]
    invalid: bool = False  # the tool's own request was refused somewhere in the step: nothing it measured stands
    reason: str | None = None  # why it is invalid


def judge(events: Iterable[Event], hold_from: float) -> Verdict:
    """The bar applied to *events* of one step whose hold period began at *hold_from*, a time.time().

    A callback that did not come within the harness's wait is a lost save, and counts in the callback times as longer
    than any; every other failure breaks its row. A failure of the load clients' own (cause ``client``) breaks the bar
    too, but when nothing else does the step is client-limited. The events of turns the deadline abandoned are not
    judged, but a request of the tool's own refused in one still makes the step invalid.
    """
    events = list(events)
    held = [e for e in events if (e.at >= hold_from or e.what == "open") and not e.abandoned]
    stats = _stats(held)
    tool = [e for e in events if e.cause == "tool"]
    if tool:
        return Verdict(False, [], False, stats, invalid=True,
                       reason=f"{len(tool)} request(s) of the tool's own refused; the first: {tool[0].detail}")
    broken = _broken(held)
    if not broken:
        return Verdict(True, [], False, stats)
    client_limited = not _broken([e for e in held if e.ok or e.cause != "client"])
    return Verdict(False, broken, client_limited, stats)


def _broken(events: list[Event]) -> list[str]:
    broken = []
    forcesave, callback, flush, opened = (_times(events, what) for what in ("forcesave", "callback", "flush", "open"))
    if forcesave and _percentile(forcesave, 99) >= FORCESAVE_P99_UNDER_S:
        broken.append(f"{FORCESAVE}: p99 {_percentile(forcesave, 99):.2f} s, not under {FORCESAVE_P99_UNDER_S:g} s")
    if forcesave and forcesave[-1] > FORCESAVE_MAX_S:
        over = sum(s > FORCESAVE_MAX_S for s in forcesave)
        broken.append(f"{FORCESAVE}: {over} over {FORCESAVE_MAX_S:g} s, the longest {forcesave[-1]:.2f} s")
    if callback and _percentile(callback, 95) > CALLBACK_P95_MAX_S:
        broken.append(f"{CALLBACK}: p95 {_percentile(callback, 95):.2f} s, over {CALLBACK_P95_MAX_S:g} s")
    if callback and _percentile(callback, 99) > CALLBACK_P99_MAX_S:
        broken.append(f"{CALLBACK}: p99 {_percentile(callback, 99):.2f} s, over {CALLBACK_P99_MAX_S:g} s")
    if flush and flush[-1] > FLUSH_MAX_S:
        over = sum(s > FLUSH_MAX_S for s in flush)
        broken.append(f"{FLUSH}: {over} acknowledged after {FLUSH_MAX_S:g} s, the longest {flush[-1]:.2f} s")
    if opened and _percentile(opened, 95) > OPEN_P95_MAX_S:
        broken.append(f"{OPEN}: p95 {_percentile(opened, 95):.2f} s, over {OPEN_P95_MAX_S:g} s")
    failed: dict[str, list[Event]] = {}
    for event in events:
        if not event.ok:
            failed.setdefault(_row(event), []).append(event)
    for row in (LOST, FORCESAVE, FLUSH, OPEN, EDITOR):
        if row in failed:
            causes = Counter(f"{e.what} {e.cause}" for e in failed[row])
            first = next((e.detail for e in failed[row] if e.detail), None)
            said = ", ".join(f"{n} {what}" for what, n in causes.most_common())
            broken.append(f"{row}: {len(failed[row])} failed ({said})" + (f"; the first: {first}" if first else ""))
    return broken


def _row(event: Event) -> str:
    """The row a failed *event* breaks."""
    if event.cause == "lost" or event.what in ("marker", "callback"):
        return LOST  # a callback that failed, or never came: the turn's save did not land
    return {"forcesave": FORCESAVE, "flush": FLUSH, "open": OPEN}.get(event.what, EDITOR)


def _times(events: list[Event], what: str) -> list[float]:
    """The sorted times of *what*'s answered calls; a callback that never came, as longer than any."""
    times = [e.seconds if e.ok else math.inf for e in events
             if e.what == what and (e.ok or (what == "callback" and e.cause == "timeout"))]
    return sorted(times)


def _percentile(ordered: list[float], p: float) -> float:
    """The nearest-rank percentile of *ordered*, sorted and not empty."""
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def _stats(events: list[Event]) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    for what in TIMED:
        times = _times(events, what)
        if times:
            stats[what] = {"n": len(times), "p50": _percentile(times, 50), "p95": _percentile(times, 95),
                           "p99": _percentile(times, 99), "max": times[-1]}
    stats["events"] = len(events)
    stats["failures"] = {cause: sum(1 for e in events if not e.ok and e.cause == cause) for cause in get_args(Cause)}
    return stats
