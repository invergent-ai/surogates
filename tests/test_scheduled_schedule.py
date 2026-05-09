from datetime import datetime, timezone

import pytest

from surogates.scheduled.schedule import (
    LoopCommand,
    parse_loop_command,
    parse_schedule,
)


def test_loop_leading_interval_parses_prompt() -> None:
    parsed = parse_loop_command("5m /babysit-prs")
    assert parsed == LoopCommand(interval="5m", prompt="/babysit-prs")


def test_loop_trailing_every_clause_parses_prompt() -> None:
    parsed = parse_loop_command("check deploys every 20m")
    assert parsed == LoopCommand(interval="20m", prompt="check deploys")


def test_loop_does_not_treat_plain_every_as_interval() -> None:
    parsed = parse_loop_command("check every PR")
    assert parsed == LoopCommand(interval="10m", prompt="check every PR")


def test_loop_default_interval() -> None:
    parsed = parse_loop_command("check queue health")
    assert parsed == LoopCommand(interval="10m", prompt="check queue health")


@pytest.mark.parametrize(
    ("expr", "cron"),
    [
        ("5m", "*/5 * * * *"),
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


def test_parse_raw_cron_and_compute_next_run() -> None:
    parsed = parse_schedule("0 9 * * 1-5", timezone_name="UTC")
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    next_run = parsed.next_after(now)
    assert next_run.isoformat().startswith("2026-05-11T09:00:00")
