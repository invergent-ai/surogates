from datetime import datetime, timezone

import pytest

from surogates.scheduled.schedule import (
    DYNAMIC_LOOP_DISPLAY,
    LoopCommand,
    parse_dynamic_loop_schedule,
    parse_loop_command,
    parse_schedule,
)


def test_loop_leading_interval_parses_prompt() -> None:
    parsed = parse_loop_command("5m /babysit-prs")
    assert parsed == LoopCommand(interval="5m", prompt="/babysit-prs")


def test_loop_trailing_every_clause_parses_prompt() -> None:
    parsed = parse_loop_command("check deploys every 20m")
    assert parsed == LoopCommand(interval="20m", prompt="check deploys")


def test_loop_leading_every_clause_parses_prompt() -> None:
    parsed = parse_loop_command("every 1 minute get bitcoin price")
    assert parsed == LoopCommand(interval="1m", prompt="get bitcoin price")


def test_loop_does_not_treat_plain_every_as_interval() -> None:
    parsed = parse_loop_command("check every PR")
    assert parsed == LoopCommand(interval=None, prompt="check every PR")


def test_loop_without_interval_is_dynamic() -> None:
    parsed = parse_loop_command("check queue health")
    assert parsed == LoopCommand(interval=None, prompt="check queue health")


def test_parse_dynamic_loop_schedule() -> None:
    parsed = parse_dynamic_loop_schedule(timezone_name="UTC")
    assert parsed.kind == "dynamic_loop"
    assert parsed.cron is None
    assert parsed.display == DYNAMIC_LOOP_DISPLAY


@pytest.mark.parametrize(
    ("expr", "cron"),
    [
        ("5m", "*/5 * * * *"),
        ("7m", "*/6 * * * *"),
        ("90m", "0 */2 * * *"),
        ("2h", "0 */2 * * *"),
        ("1d", "0 0 */1 * *"),
        ("45s", "*/1 * * * *"),
        ("120s", "*/2 * * * *"),
    ],
)
def test_parse_interval_to_cron(expr: str, cron: str) -> None:
    parsed = parse_schedule(expr, timezone_name="UTC")
    assert parsed.kind == "cron"
    assert parsed.cron == cron
    assert parsed.display


@pytest.mark.parametrize(
    ("expr", "adjusted_from", "display"),
    [
        ("7m", "Every 7 minutes", "Every 6 minutes"),
        ("90m", "Every 90 minutes", "Every 2 hours"),
    ],
)
def test_parse_interval_records_adjusted_cadence(
    expr: str,
    adjusted_from: str,
    display: str,
) -> None:
    parsed = parse_schedule(expr, timezone_name="UTC")
    assert parsed.display == display
    assert parsed.adjusted_from == adjusted_from


def test_parse_raw_cron_and_compute_next_run() -> None:
    parsed = parse_schedule("0 9 * * 1-5", timezone_name="UTC")
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    next_run = parsed.next_after(now)
    assert next_run.isoformat().startswith("2026-05-11T09:00:00")
