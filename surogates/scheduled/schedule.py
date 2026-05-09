from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

DEFAULT_LOOP_INTERVAL = "10m"
DEFAULT_LOOP_EXPIRY_DAYS = 3

_LEADING_INTERVAL_RE = re.compile(r"^(\d+)([smhd])(?:\s+)(.+)$", re.I | re.S)
_TRAILING_EVERY_RE = re.compile(
    r"^(?P<prompt>.+?)\s+every\s+(?P<num>\d+)\s*"
    r"(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\s*$",
    re.I | re.S,
)
_DURATION_RE = re.compile(r"^(?P<num>\d+)\s*(?P<unit>s|m|h|d)$", re.I)


@dataclass(frozen=True, slots=True)
class LoopCommand:
    interval: str
    prompt: str


@dataclass(frozen=True, slots=True)
class ParsedSchedule:
    kind: str
    cron: str
    display: str
    timezone_name: str = "UTC"

    def next_after(self, after: datetime) -> datetime:
        tz = resolve_timezone(self.timezone_name)
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        base = after.astimezone(tz)
        next_local = croniter(self.cron, base).get_next(datetime)
        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=tz)
        return next_local.astimezone(timezone.utc)


def resolve_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc


def parse_loop_command(raw: str) -> LoopCommand:
    text = raw.strip()
    if not text:
        raise ValueError("Usage: /loop [interval] <prompt>")

    leading = _LEADING_INTERVAL_RE.match(text)
    if leading:
        return LoopCommand(
            interval=f"{int(leading.group(1))}{leading.group(2).lower()}",
            prompt=leading.group(3).strip(),
        )

    trailing = _TRAILING_EVERY_RE.match(text)
    if trailing:
        unit = _normalize_unit(trailing.group("unit"))
        return LoopCommand(
            interval=f"{int(trailing.group('num'))}{unit}",
            prompt=trailing.group("prompt").strip(),
        )

    return LoopCommand(interval=DEFAULT_LOOP_INTERVAL, prompt=text)


def parse_schedule(value: str, *, timezone_name: str = "UTC") -> ParsedSchedule:
    text = value.strip()
    if not text:
        raise ValueError("Schedule cannot be empty.")

    resolve_timezone(timezone_name)
    duration = _DURATION_RE.match(text)
    if duration:
        cron, display = _duration_to_cron(
            int(duration.group("num")),
            duration.group("unit").lower(),
        )
        return ParsedSchedule(
            kind="cron",
            cron=cron,
            display=display,
            timezone_name=timezone_name,
        )

    if not croniter.is_valid(text):
        raise ValueError(
            "Invalid schedule. Use an interval like '10m' or a 5-field cron expression.",
        )
    return ParsedSchedule(
        kind="cron",
        cron=text,
        display=humanize_cron(text),
        timezone_name=timezone_name,
    )


def _normalize_unit(unit: str) -> str:
    first = unit.lower()[0]
    if first in {"s", "m", "h", "d"}:
        return first
    raise ValueError(f"Unsupported interval unit: {unit}")


def _duration_to_cron(amount: int, unit: str) -> tuple[str, str]:
    if amount <= 0:
        raise ValueError("Interval must be greater than zero")
    if unit == "s":
        amount = max(1, math.ceil(amount / 60))
        unit = "m"
    if unit == "m":
        if amount <= 59:
            return (
                f"*/{amount} * * * *",
                f"Every {amount} minute{'s' if amount != 1 else ''}",
            )
        hours = max(1, math.ceil(amount / 60))
        return f"0 */{hours} * * *", f"Every {hours} hour{'s' if hours != 1 else ''}"
    if unit == "h":
        if amount > 23:
            days = math.ceil(amount / 24)
            return f"0 0 */{days} * *", f"Every {days} day{'s' if days != 1 else ''}"
        return f"0 */{amount} * * *", f"Every {amount} hour{'s' if amount != 1 else ''}"
    if unit == "d":
        return f"0 0 */{amount} * *", f"Every {amount} day{'s' if amount != 1 else ''}"
    raise ValueError(f"Unsupported interval unit: {unit}")


def humanize_cron(cron: str) -> str:
    parts = cron.split()
    if len(parts) != 5:
        return cron
    minute, hour, day_of_month, month, day_of_week = parts
    if (
        minute.startswith("*/")
        and hour == "*"
        and day_of_month == "*"
        and month == "*"
        and day_of_week == "*"
    ):
        n = minute[2:]
        return f"Every {n} minute{'s' if n != '1' else ''}"
    if (
        minute == "0"
        and hour.startswith("*/")
        and day_of_month == "*"
        and month == "*"
        and day_of_week == "*"
    ):
        n = hour[2:]
        return f"Every {n} hour{'s' if n != '1' else ''}"
    if (
        minute == "0"
        and hour.isdigit()
        and day_of_month == "*"
        and month == "*"
        and day_of_week.upper() in {"1-5", "MON-FRI"}
    ):
        return f"Weekdays at {int(hour):02d}:00"
    return cron


def apply_deterministic_jitter(
    run_at: datetime,
    schedule_id: str,
    *,
    period_seconds: int,
) -> datetime:
    cap = min(max(0, period_seconds // 10), 900)
    if cap == 0:
        return run_at
    digest = hashlib.sha256(schedule_id.encode("utf-8")).hexdigest()
    offset = int(digest[:8], 16) % (cap + 1)
    return run_at + timedelta(seconds=offset)
