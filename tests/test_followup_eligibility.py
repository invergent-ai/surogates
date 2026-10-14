"""Who a Program audience reaches on a given day."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from surogates.followup.eligibility import (
    MAX_DAY,
    Audience,
    day_index,
    ineligibility,
    next_contact,
    start_date_problem,
)

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
    {"protocol": "p", "day_from": -366},
    {"protocol": "p", "day_to": 10**6},
    {"protocol": "p", "day_from": "3"},
    {"protocol": "p", "day_from": True},
    {"protocol": "p", "phases": "a"},
    {"protocol": "p", "silent_days": 0},
])
def test_malformed_audiences_are_refused(raw):
    with pytest.raises(ValueError):
        Audience.from_config(raw)


def test_an_audience_round_trips_through_its_config():
    audience = Audience.from_config(
        {"protocol": "post-op", "day_from": 0, "day_to": 30,
         "phases": ["b", " a ", "b"], "silent_days": 2},
    )
    assert audience.to_config() == {
        "protocol": "post-op", "day_from": 0, "day_to": 30,
        "phases": ["a", "b"], "silent_days": 2,
    }
    assert Audience.from_config(audience.to_config()) == audience
    assert Audience(protocol="p").to_config() == {
        "protocol": "p", "day_from": None, "day_to": None,
        "phases": [], "silent_days": None,
    }


@pytest.mark.parametrize("offset, ok", [
    (0, True), (MAX_DAY, True), (-MAX_DAY, True),
    (MAX_DAY + 1, False), (-MAX_DAY - 1, False),
])
def test_a_start_date_must_be_within_the_protocol_horizon(offset, ok):
    problem = start_date_problem(TODAY + timedelta(days=offset), TODAY)
    assert (problem is None) is ok


# --- next_contact -----------------------------------------------------------

_DAILY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FIRST_MONTH = {
    "name": "TAVI — prima lună", "timezone": "Europe/Bucharest",
    "weekdays": _DAILY, "times_local": ["08:30"],
    "audience": {"protocol": "tavi", "day_from": 0, "day_to": 30},
}
LATER = {
    "name": "TAVI — luna 2-3", "timezone": "Europe/Bucharest",
    "weekdays": ["mon"], "times_local": ["09:00"],
    "audience": {"protocol": "tavi", "day_from": 31, "day_to": 90},
}
AT_TUESDAY = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)  # Tue 13:00 Bucharest


def _next(configs, **kw):
    args = {
        "protocol": "tavi", "start_date": AT_TUESDAY.date(), "phase": None,
        "enrolled_at": AT_TUESDAY, "last_inbound_at": AT_TUESDAY, "now": AT_TUESDAY,
    } | kw
    return next_contact(configs, **args)


def test_next_contact_is_the_next_slot_of_the_matching_program():
    at, config = _next([LATER, FIRST_MONTH])
    assert at == datetime(2026, 10, 14, 5, 30, tzinfo=timezone.utc)
    assert config["name"] == "TAVI — prima lună"


def test_someone_past_the_first_month_waits_for_the_later_program():
    at, config = _next([FIRST_MONTH, LATER], start_date=date(2026, 9, 3))  # day 40
    assert config["name"] == "TAVI — luna 2-3"
    assert at == datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)  # Mon 09:00


def test_a_window_that_opens_far_ahead_is_still_found():
    far = FIRST_MONTH | {"audience": {"protocol": "tavi", "day_from": 500}}
    at, _ = _next([far])
    assert (at.date() - AT_TUESDAY.date()).days in (499, 500)


@pytest.mark.parametrize("kw", [
    {"start_date": None},  # windowed Programs need a start
    {"start_date": date(2026, 1, 1)},  # past every window
    {"protocol": "other"},
])
def test_nobody_is_reached_outside_every_program(kw):
    assert _next([FIRST_MONTH, LATER], **kw) is None


def test_phase_and_silence_are_respected():
    phased = FIRST_MONTH | {"audience": {"protocol": "tavi", "phases": ["acute"]}}
    assert _next([phased], phase="stable") is None
    assert _next([phased], phase="acute") is not None
    silent = FIRST_MONTH | {"audience": {"protocol": "tavi", "silent_days": 3}}
    at, _ = _next([silent])
    assert at == datetime(2026, 10, 17, 5, 30, tzinfo=timezone.utc)


# --- days before the start date ----------------------------------------------

def test_days_before_the_start_are_negative_and_can_be_targeted():
    eve = Audience.from_config({"protocol": "p", "day_from": -1, "day_to": -1})
    assert day_index(TODAY + timedelta(days=1), TODAY) == -1
    assert _check(eve, start=TODAY + timedelta(days=1)) is None
    assert _check(eve, start=TODAY) == "Day 0 is past day -1"
    assert _check(eve, start=TODAY + timedelta(days=3)) == "Day -3 is before day -1"


def test_the_next_contact_before_a_future_start_is_the_day_before():
    reminder = FIRST_MONTH | {"audience": {"protocol": "tavi", "day_from": -1, "day_to": -1}}
    start = AT_TUESDAY.date() + timedelta(days=10)
    at, _ = _next([reminder], start_date=start)
    assert at == datetime(2026, 10, 22, 5, 30, tzinfo=timezone.utc)  # day -1, 08:30


def test_a_dry_run_reaches_the_days_before_the_start():
    from surogates.programs.simulate import simulate

    step = FIRST_MONTH | {"id": "s1", "audience": {"protocol": "tavi", "day_from": -2, "day_to": 0}}
    events = simulate(
        [step], start_date=date(2026, 10, 20), timezone=None, days=14,
        today=date(2026, 10, 13),
    )
    assert [e["at"][:10] for e in events] == ["2026-10-18", "2026-10-19", "2026-10-20"]
