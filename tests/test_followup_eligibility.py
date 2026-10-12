"""Who a Program audience reaches on a given day."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from surogates.followup.eligibility import Audience, day_index, ineligibility

NOW = datetime(2026, 10, 12, 8, 30, tzinfo=timezone.utc)
TODAY = date(2026, 10, 12)


def _check(audience, *, start=None, phase=None, enrolled=NOW, last=None):
    return ineligibility(
        audience, start_date=start, phase=phase, enrolled_at=enrolled,
        last_inbound_at=last, today=TODAY, now=NOW,
    )


def test_day_zero_is_the_start_date():
    assert day_index(TODAY, TODAY) == 0
    assert day_index(TODAY - timedelta(days=30), TODAY) == 30
    assert day_index(None, TODAY) is None


@pytest.mark.parametrize("days_ago, due", [
    (0, True), (30, True), (31, False), (-1, False),
])
def test_day_window_is_inclusive(days_ago, due):
    first_month = Audience(protocol="p", day_from=0, day_to=30)
    reason = _check(first_month, start=TODAY - timedelta(days=days_ago))
    assert (reason is None) is due


def test_open_ended_window_keeps_going():
    after_month_six = Audience(protocol="p", day_from=181)
    assert _check(after_month_six, start=TODAY - timedelta(days=400)) is None
    assert _check(after_month_six, start=TODAY - timedelta(days=100)) is not None


def test_a_window_needs_a_start_date():
    assert _check(Audience(protocol="p", day_to=30)) == "No start date recorded"


def test_no_window_needs_no_start_date():
    assert _check(Audience(protocol="p")) is None


def test_phase_filter():
    titration = Audience(protocol="p", phases=("titration",))
    assert _check(titration, phase="titration") is None
    assert "maintenance" in _check(titration, phase="maintenance")
    assert "unset" in _check(titration, phase=None)


def test_silence_is_measured_from_the_last_message():
    reach_out = Audience(protocol="p", silent_days=3)
    assert _check(reach_out, last=NOW - timedelta(days=3)) is None
    assert _check(reach_out, last=NOW - timedelta(days=2)) is not None


def test_someone_who_never_wrote_is_measured_from_enrolment():
    reach_out = Audience(protocol="p", silent_days=3)
    assert _check(reach_out, enrolled=NOW - timedelta(hours=5)) is not None
    assert _check(reach_out, enrolled=NOW - timedelta(days=4)) is None


def test_naive_timestamps_are_read_as_utc():
    reach_out = Audience(protocol="p", silent_days=1)
    naive = (NOW - timedelta(days=2)).replace(tzinfo=None)
    assert _check(reach_out, last=naive) is None


def test_parse_round_trip():
    parsed = Audience.from_config({
        "protocol": " post-op ", "day_from": 0, "day_to": 30,
        "phases": ["b", "a", "a", " "], "silent_days": 3,
    })
    assert parsed == Audience(
        protocol="post-op", day_from=0, day_to=30, phases=("a", "b"),
        silent_days=3,
    )
    assert Audience.from_config(None) is None


@pytest.mark.parametrize("raw", [
    "post-op",
    {},
    {"protocol": ""},
    {"protocol": "p", "day_from": 5, "day_to": 4},
    {"protocol": "p", "day_from": -1},
    {"protocol": "p", "day_to": 10**6},
    {"protocol": "p", "day_from": "3"},
    {"protocol": "p", "day_from": True},
    {"protocol": "p", "phases": "a"},
    {"protocol": "p", "silent_days": 0},
])
def test_malformed_audiences_are_refused(raw):
    with pytest.raises(ValueError):
        Audience.from_config(raw)
