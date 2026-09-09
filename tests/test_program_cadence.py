"""Turning a Program's cadence into UTC instants, DST included."""

from __future__ import annotations

from datetime import datetime

from surogates.programs.cadence import next_occurrences


def test_two_times_a_day_gives_two_occurrences():
    # "Every 12 hours" is two times of day, not an interval — an interval
    # anchored to a start instant drifts into the small hours.
    # 2026-09-09 is a Wednesday. Start at 05:00 UTC, strictly before the
    # first slot — 09:00 EEST is exactly 06:00 UTC, and "after" is strict,
    # so starting at 06:00 would skip it and return [18, 6].
    got = next_occurrences(
        datetime(2026, 9, 9, 5, 0),
        weekdays=["wed"],
        times_local=["09:00", "21:00"],
        timezone="Europe/Bucharest",
        count=2,
    )
    assert [d.hour for d in got] == [6, 18]  # 09:00 and 21:00 EEST = UTC+3


def test_it_skips_days_not_in_the_set():
    got = next_occurrences(
        datetime(2026, 9, 9, 12, 0),  # Wednesday
        weekdays=["mon"],
        times_local=["09:00"],
        timezone="UTC",
        count=1,
    )
    assert got[0].weekday() == 0  # the following Monday


def test_a_nonexistent_local_time_moves_to_the_first_valid_instant():
    # Spring forward: 03:00-04:00 does not exist on 2026-03-29 in Bucharest.
    got = next_occurrences(
        datetime(2026, 3, 28, 12, 0),
        weekdays=["sun"],
        times_local=["03:30"],
        timezone="Europe/Bucharest",
        count=1,
    )
    assert got[0] is not None  # resolved, not skipped and not crashed


def test_a_repeated_local_time_fires_once():
    # Autumn back: 03:00-04:00 happens twice on 2026-10-25 in Bucharest.
    got = next_occurrences(
        datetime(2026, 10, 24, 12, 0),
        weekdays=["sun"],
        times_local=["03:30"],
        timezone="Europe/Bucharest",
        count=2,
    )
    assert len(set(got)) == len(got)
    # The second is the following week, not the repeated hour.
    assert (got[1] - got[0]).days >= 6
