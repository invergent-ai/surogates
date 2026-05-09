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
DYNAMIC_LOOP_EXPIRY_DAYS = 7
DYNAMIC_LOOP_DISPLAY = "Dynamic loop (1 minute to 1 hour)"
DYNAMIC_LOOP_MIN_DELAY_SECONDS = 60
DYNAMIC_LOOP_MAX_DELAY_SECONDS = 3600
DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS = 600

_LEADING_INTERVAL_RE = re.compile(r"^(\d+)([smhd])(?:\s+)(.+)$", re.I | re.S)
_LEADING_EVERY_RE = re.compile(
    r"^every\s+(?P<num>\d+)\s*"
    r"(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)"
    r"\s+(?P<prompt>.+)$",
    re.I | re.S,
)
_TRAILING_EVERY_RE = re.compile(
    r"^(?P<prompt>.+?)\s+every\s+(?P<num>\d+)\s*"
    r"(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\s*$",
    re.I | re.S,
)
_DURATION_RE = re.compile(r"^(?P<num>\d+)\s*(?P<unit>s|m|h|d)$", re.I)


@dataclass(frozen=True, slots=True)
class LoopCommand:
    interval: str | None
    prompt: str


@dataclass(frozen=True, slots=True)
class ParsedSchedule:
    kind: str
    cron: str | None
    display: str
    timezone_name: str = "UTC"
    adjusted_from: str | None = None

    def next_after(self, after: datetime) -> datetime:
        if self.cron is None:
            raise ValueError(f"{self.kind} schedules do not have a cron cadence")
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

    leading_every = _LEADING_EVERY_RE.match(text)
    if leading_every:
        unit = _normalize_unit(leading_every.group("unit"))
        return LoopCommand(
            interval=f"{int(leading_every.group('num'))}{unit}",
            prompt=leading_every.group("prompt").strip(),
        )

    trailing = _TRAILING_EVERY_RE.match(text)
    if trailing:
        unit = _normalize_unit(trailing.group("unit"))
        return LoopCommand(
            interval=f"{int(trailing.group('num'))}{unit}",
            prompt=trailing.group("prompt").strip(),
        )

    return LoopCommand(interval=None, prompt=text)


def parse_dynamic_loop_schedule(*, timezone_name: str = "UTC") -> ParsedSchedule:
    resolve_timezone(timezone_name)
    return ParsedSchedule(
        kind="dynamic_loop",
        cron=None,
        display=DYNAMIC_LOOP_DISPLAY,
        timezone_name=timezone_name,
    )


def clamp_dynamic_loop_delay(delay_seconds: int | float) -> int:
    return max(
        DYNAMIC_LOOP_MIN_DELAY_SECONDS,
        min(DYNAMIC_LOOP_MAX_DELAY_SECONDS, int(delay_seconds)),
    )


def parse_schedule(value: str, *, timezone_name: str = "UTC") -> ParsedSchedule:
    text = value.strip()
    if not text:
        raise ValueError("Schedule cannot be empty.")

    resolve_timezone(timezone_name)
    duration = _DURATION_RE.match(text)
    if duration:
        cron, display, adjusted_from = _duration_to_cron(
            int(duration.group("num")),
            duration.group("unit").lower(),
        )
        return ParsedSchedule(
            kind="cron",
            cron=cron,
            display=display,
            timezone_name=timezone_name,
            adjusted_from=adjusted_from,
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


def _duration_to_cron(amount: int, unit: str) -> tuple[str, str, str | None]:
    if amount <= 0:
        raise ValueError("Interval must be greater than zero")
    original_amount = amount
    original_unit = unit
    if unit == "s":
        amount = max(1, math.ceil(amount / 60))
        unit = "m"
    if unit == "m":
        if amount < 60:
            adjusted = _nearest_clean_minute_interval(amount)
            adjusted_from = None
            if adjusted != amount or original_unit == "s":
                adjusted_from = _display_duration(original_amount, original_unit)
            if adjusted == 60:
                return "0 */1 * * *", "Every 1 hour", adjusted_from
            return (
                f"*/{adjusted} * * * *",
                f"Every {adjusted} minute{'s' if adjusted != 1 else ''}",
                adjusted_from,
            )
        hours = max(1, math.ceil(amount / 60))
        return (
            f"0 */{hours} * * *",
            f"Every {hours} hour{'s' if hours != 1 else ''}",
            _display_duration(original_amount, original_unit),
        )
    if unit == "h":
        if amount > 23:
            days = math.ceil(amount / 24)
            return (
                f"0 0 */{days} * *",
                f"Every {days} day{'s' if days != 1 else ''}",
                _display_duration(original_amount, original_unit),
            )
        return (
            f"0 */{amount} * * *",
            f"Every {amount} hour{'s' if amount != 1 else ''}",
            None,
        )
    if unit == "d":
        return (
            f"0 0 */{amount} * *",
            f"Every {amount} day{'s' if amount != 1 else ''}",
            None,
        )
    raise ValueError(f"Unsupported interval unit: {unit}")


def _nearest_clean_minute_interval(amount: int) -> int:
    clean = (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60)
    return min(clean, key=lambda candidate: (abs(candidate - amount), candidate < amount))


def _display_duration(amount: int, unit: str) -> str:
    if unit == "s":
        label = "second"
    elif unit == "m":
        label = "minute"
    elif unit == "h":
        label = "hour"
    elif unit == "d":
        label = "day"
    else:
        label = unit
    return f"Every {amount} {label}{'s' if amount != 1 else ''}"


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
