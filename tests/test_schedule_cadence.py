"""Cadences: when a schedule's runs fall, and what ``/loop`` reads."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from surogates.scheduled.schedule import (
    calendar_schedule,
    humanize_cron,
    once_schedule,
    parse_loop_command,
    parse_schedule,
    schedule_from_fields,
    schedule_from_row,
)

UTC = timezone.utc


def _fires(schedule, after, count):
    out = []
    for _ in range(count):
        after = schedule.next_after(after)
        out.append(after)
    return out


def test_a_daily_cron_in_a_zone_fires_once_when_the_clocks_go_back():
    # 03:30 in Bucharest happens twice on 25 Oct 2026; the run happens once.
    schedule = parse_schedule("30 3 * * *", timezone_name="Europe/Bucharest")
    assert _fires(schedule, datetime(2026, 10, 24, 12, tzinfo=UTC), 3) == [
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 26, 1, 30, tzinfo=UTC),
        datetime(2026, 10, 27, 1, 30, tzinfo=UTC),
    ]


def test_a_daily_cron_fires_once_when_the_clocks_go_forward():
    # 03:30 does not exist on 29 Mar 2026: it fires at the instant it maps to.
    schedule = parse_schedule("30 3 * * *", timezone_name="Europe/Bucharest")
    assert _fires(schedule, datetime(2026, 3, 28, 12, tzinfo=UTC), 2) == [
        datetime(2026, 3, 29, 1, 30, tzinfo=UTC),
        datetime(2026, 3, 30, 0, 30, tzinfo=UTC),
    ]


def test_a_frequent_cron_inside_the_repeated_hour_has_a_next_run():
    # 03:15 the second time round (01:15 UTC): every 03:xx left that hour
    # already ran the first time, so the next run is 04:00.
    schedule = parse_schedule("*/5 * * * *", timezone_name="Europe/Bucharest")
    assert schedule.next_after(datetime(2026, 10, 25, 1, 15, tzinfo=UTC)) == datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
    first_pass = _fires(schedule, datetime(2026, 10, 25, 0, 50, tzinfo=UTC), 3)
    assert first_pass == [
        datetime(2026, 10, 25, 0, 55, tzinfo=UTC),
        datetime(2026, 10, 25, 2, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 2, 5, tzinfo=UTC),
    ]


def test_a_calendar_time_fires_once_a_day_across_the_change():
    schedule = calendar_schedule(["mon", "tue", "wed", "thu", "fri", "sat", "sun"], ["3:30"],
                                 timezone_name="Europe/Bucharest")
    fires = _fires(schedule, datetime(2026, 10, 24, 12, tzinfo=UTC), 3)
    assert [f.date().isoformat() for f in fires] == ["2026-10-25", "2026-10-26", "2026-10-27"]


def test_an_interval_is_elapsed_time_from_its_anchor_however_long():
    # 25h used to become "0 0 */2 * *": odd days of the month.
    schedule = parse_schedule("25h").anchored(datetime(2026, 1, 1, tzinfo=UTC))
    assert _fires(schedule, datetime(2026, 1, 1, tzinfo=UTC), 3) == [
        datetime(2026, 1, 2, 1, tzinfo=UTC),
        datetime(2026, 1, 3, 2, tzinfo=UTC),
        datetime(2026, 1, 4, 3, tzinfo=UTC),
    ]
    with pytest.raises(ValueError, match="1 minute"):
        parse_schedule("30s")


def test_a_one_off_has_one_run_then_none():
    schedule = once_schedule(datetime(2026, 10, 28, 15, 30), timezone_name="Europe/Bucharest")
    assert schedule.next_after(datetime(2026, 10, 27, tzinfo=UTC)) == datetime(2026, 10, 28, 13, 30, tzinfo=UTC)
    assert schedule.next_after(datetime(2026, 10, 29, tzinfo=UTC)) is None


def test_fields_give_exactly_one_cadence():
    assert schedule_from_fields(interval="90m").seconds == 5400
    assert schedule_from_fields(times=["9"], timezone_name="Europe/Bucharest").days == (
        "mon", "tue", "wed", "thu", "fri", "sat", "sun",
    )
    with pytest.raises(ValueError, match="exactly one"):
        schedule_from_fields(cron="0 9 * * *", interval="1h")
    with pytest.raises(ValueError, match="exactly one"):
        schedule_from_fields()


@pytest.mark.parametrize("text, prompt, display, tz", [
    ("30m check the deploy", "check the deploy", "Every 30 minutes", "UTC"),
    ("every 2 hours check the deploy", "check the deploy", "Every 2 hours", "UTC"),
    ("check the deploy every 2 hours", "check the deploy", "Every 2 hours", "UTC"),
    ("every day at 9 in Europe/Bucharest check the inbox", "check the inbox",
     "Every day at 09:00 (Europe/Bucharest)", "Europe/Bucharest"),
    ("every weekday at 8:30am, 17:00 Europe/Bucharest: report", "report",
     "Weekdays at 08:30, 17:00 (Europe/Bucharest)", "Europe/Bucharest"),
    ("every mon and wed at 9pm review PRs", "review PRs", "Mon, Wed at 21:00 (UTC)", "UTC"),
    ("post a thread daily at 10", "post a thread", "Every day at 10:00 (UTC)", "UTC"),
    # The shape handed to the model on prod: a cron and a labelled zone.
    ("Schedule: */30 * * * * Timezone: Europe/Bucharest post on X", "post on X",
     "Every 30 minutes", "Europe/Bucharest"),
    ("0 9 * * 1-5 send the digest", "send the digest", "Weekdays at 09:00", "UTC"),
])
def test_loop_reads_cadences_zones_and_crons(text, prompt, display, tz):
    command = parse_loop_command(text)
    assert (command.verb, command.prompt) == ("create", prompt)
    assert (command.schedule.display, command.schedule.timezone_name) == (display, tz)


@pytest.mark.parametrize("text, verb, target", [
    ("", "help", None),
    ("list", "list", None),
    ("cancel", "cancel", None),
    ("stop", "cancel", None),
    ("cancel 4b1c", "cancel", "4b1c"),
    ("pause 4b1c", "pause", "4b1c"),
    ("resume", "resume", None),
    ("list all", "list", "all"),
])
def test_loop_commands_are_commands_never_prompts(text, verb, target):
    command = parse_loop_command(text)
    assert (command.verb, command.target, command.schedule) == (verb, target, None)


def test_loop_without_a_cadence_is_dynamic_and_one_it_cannot_read_goes_to_the_model():
    dynamic = parse_loop_command("stop the build if it is green")
    assert (dynamic.verb, dynamic.schedule, dynamic.prompt) == ("create", None, "stop the build if it is green")
    for text in ("every second tuesday post the newsletter", "post on X at 9:30 each weekday please"):
        assert parse_loop_command(text).verb == "model"


def test_cron_reads_in_words_where_it_can():
    assert humanize_cron("30 3 * * *") == "Every day at 03:30"
    assert humanize_cron("0 9 * * 1,3") == "Mon, Wed at 09:00"
    assert humanize_cron("0 9 1 * *") == "0 9 1 * *"


@pytest.mark.parametrize("text", [
    "list new GitHub issues every hour",
    "help me triage the inbox every day at 9",
])
def test_list_and_help_followed_by_more_are_requests(text):
    command = parse_loop_command(text)
    assert command.verb in {"create", "model"}
    assert command.prompt


def test_stored_days_are_names_or_numbers_from_monday():
    names = calendar_schedule(["mon", "wed", "fri"], ["09:00"])
    numbers = calendar_schedule([0, 2, 4], ["09:00"])
    assert names.days == numbers.days == ("mon", "wed", "fri")
    assert numbers.cadence()["days"] == ["mon", "wed", "fri"]


@pytest.mark.parametrize("cadence", [
    {"days": [9], "times": ["09:00"]},
    {"days": [None], "times": ["09:00"]},
    {"times": ["09:00"], "days": "mon"},
    {},
], ids=["day-out-of-range", "null-day", "days-not-a-list", "empty"])
def test_a_malformed_row_is_a_value_error(cadence):
    row = SimpleNamespace(kind="calendar", cadence=cadence, timezone="UTC", schedule={})
    with pytest.raises(ValueError):
        schedule_from_row(row)
    with pytest.raises(ValueError):
        schedule_from_row(SimpleNamespace(kind="once", cadence={}, timezone="UTC", schedule={}))
