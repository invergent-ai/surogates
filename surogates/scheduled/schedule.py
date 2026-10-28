from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from surogates.programs.cadence import next_occurrences

DEFAULT_LOOP_EXPIRY_DAYS = 3
DYNAMIC_LOOP_EXPIRY_DAYS = 7
DYNAMIC_LOOP_DISPLAY = "Dynamic loop (1 minute to 1 hour)"
DYNAMIC_LOOP_MIN_DELAY_SECONDS = 60
DYNAMIC_LOOP_MAX_DELAY_SECONDS = 3600
DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS = 600
DYNAMIC_LOOP_STALE_RUN_SECONDS = 900
MIN_INTERVAL_SECONDS = 60
#: How long a run may go on before its schedule stops waiting for it.
DEFAULT_MAX_RUN_SECONDS = 7200

#: How a schedule's next run is computed (the ``kind`` column).
CADENCE_KINDS = ("calendar", "interval", "once", "cron", "dynamic")

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_ALIASES = {
    "mon": "mon", "monday": "mon", "mondays": "mon",
    "tue": "tue", "tues": "tue", "tuesday": "tue", "tuesdays": "tue",
    "wed": "wed", "wednesday": "wed", "wednesdays": "wed",
    "thu": "thu", "thur": "thu", "thurs": "thu", "thursday": "thu", "thursdays": "thu",
    "fri": "fri", "friday": "fri", "fridays": "fri",
    "sat": "sat", "saturday": "sat", "saturdays": "sat",
    "sun": "sun", "sunday": "sun", "sundays": "sun",
}
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_UNIT_NAMES = {"s": "second", "m": "minute", "h": "hour", "d": "day"}

_UNIT = r"(?P<unit>s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?|d|days?)"
_ZONE = (
    r"(?:UTC|GMT|(?:Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|Indian|Pacific"
    r"|Etc|US|Canada|Brazil|Mexico|Chile)/[A-Za-z_+\-]+(?:/[A-Za-z_+\-]+)?)"
)
_TZ = rf"(?:\s*,?\s*(?:in\s+|tz\s+|time\s*zone:?\s+|\()?(?P<tz>{_ZONE})\)?)?"
_TIME = r"\d{1,2}(?:[:.h]\d{2})?\s*(?:am|pm)?"
_TIMES = rf"(?P<times>{_TIME}(?:\s*(?:,|and|&)\s*{_TIME})*)"
_DAYS = (
    r"(?P<days>day|weekday|weekdays|weekend|weekends|"
    r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*"
    r"(?:\s*(?:,|and|&)\s*(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*)*)"
)

_CADENCE_PATTERNS = (
    # 30m, 25h, 2d
    ("interval", re.compile(rf"^(?P<num>\d+)\s*{_UNIT}\b{_TZ}", re.I)),
    # every day at 9, every weekday at 8:30, every mon and wed at 9am, daily at 9
    ("calendar", re.compile(rf"^(?:every\s+|each\s+|on\s+)?{_DAYS}\s+at\s+{_TIMES}{_TZ}", re.I)),
    ("calendar", re.compile(rf"^(?P<daily>daily)\s+at\s+{_TIMES}{_TZ}", re.I)),
    ("calendar", re.compile(rf"^at\s+{_TIMES}\s+(?:every\s+|each\s+|on\s+)?{_DAYS}{_TZ}", re.I)),
    # every 2 hours, every hour, hourly, daily
    ("interval", re.compile(rf"^every\s+(?P<num>\d+)\s*{_UNIT}\b{_TZ}", re.I)),
    # Not "every second": that is "every second Tuesday" as often as not.
    ("interval", re.compile(rf"^every\s+(?P<unit>m|mins?|minutes?|h|hrs?|hours?|d|days?)\b{_TZ}", re.I)),
    ("interval", re.compile(rf"^(?P<word>hourly|daily){_TZ}(?=\s|[:,\-]|$)", re.I)),
)
#: A leading 5-field cron (`*/30 * * * *`); validated with croniter.
_LEADING_CRON_RE = re.compile(r"^(?P<cron>(?:[\d*/,\-A-Za-z?]+\s+){4}[\d*/,\-A-Za-z?]+)(?=\s|$)")
_SCHEDULE_LABEL_RE = re.compile(r"^(?:schedule|cadence)(?:\s*\d+)?\s*:\s*", re.I)
#: The other lines of a labelled block: a name is not the prompt, and a
#: prompt's label is not part of it.
_NAME_LINE_RE = re.compile(r"^\s*(?:name|title)\s*:.*$", re.I | re.M)
_PROMPT_LABEL_RE = re.compile(r"^\s*(?:prompt|task|instructions?)\s*:\s*", re.I | re.M)
_SEPARATOR_RE = re.compile(r"^\s*(?:[:,\-–]\s*)?")
_TRAILING_ANCHOR_RE = re.compile(r"\s(?=(?:every|each|daily|hourly|at)\b)", re.I)
#: Words that say the text asks for a cadence, read or not.
_CADENCE_INTENT_RE = re.compile(
    r"\b(?:every|daily|hourly|weekly|monthly|weekdays?|weekends?|cron|timezone|schedule)\b"
    r"|\*/\d|\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b",
    re.I,
)
_LOOP_VERBS = {"list": "list", "cancel": "cancel", "stop": "cancel",
               "pause": "pause", "resume": "resume", "help": "help"}


@dataclass(frozen=True, slots=True)
class ParsedSchedule:
    """A cadence: when a schedule's runs fall, in its named zone.

    ``kind`` is one of :data:`CADENCE_KINDS`.  ``calendar`` is weekdays
    plus local times (each local time fires once a day across DST);
    ``interval`` is a true elapsed period from ``anchor``; ``once`` is one
    instant; ``cron`` is a 5-field expression read on the zone's wall
    clock; ``dynamic`` has no cadence (each run picks the next delay).
    """

    kind: str
    display: str
    timezone_name: str = "UTC"
    cron: str | None = None
    days: tuple[str, ...] = ()
    times: tuple[str, ...] = ()
    seconds: int | None = None
    anchor: datetime | None = None
    at: datetime | None = None

    @property
    def is_dynamic(self) -> bool:
        return self.kind == "dynamic"

    @property
    def legacy_kind(self) -> str:
        """The ``schedule.kind`` older readers branch on."""
        return "dynamic_loop" if self.is_dynamic else "cron"

    def next_after(self, after: datetime) -> datetime | None:
        """The first run strictly after *after* (aware UTC), or None when
        there is none: a ``once`` already past, or a ``dynamic`` cadence."""
        after = _aware(after)
        if self.kind == "calendar":
            found = next_occurrences(
                after, weekdays=list(self.days), times_local=list(self.times),
                timezone=self.timezone_name,
            )
            return found[0] if found else None
        if self.kind == "interval":
            anchor = _aware(self.anchor) if self.anchor else after
            step = timedelta(seconds=int(self.seconds or 0))
            if step.total_seconds() <= 0:
                return None
            if after < anchor:
                return anchor
            periods = int((after - anchor) / step) + 1
            return anchor + periods * step
        if self.kind == "once":
            at = _aware(self.at) if self.at else None
            return at if at is not None and at > after else None
        if self.kind == "cron":
            return _next_cron(self.cron or "", self.timezone_name, after)
        return None

    def cadence(self) -> dict:
        """The ``cadence`` column: what :func:`schedule_from_row` reads back."""
        if self.kind == "calendar":
            return {"days": list(self.days), "times": list(self.times), "tz": self.timezone_name}
        if self.kind == "interval":
            return {
                "seconds": self.seconds,
                "anchor": _aware(self.anchor).isoformat() if self.anchor else None,
            }
        if self.kind == "once":
            return {"at": _aware(self.at).isoformat() if self.at else None}
        if self.kind == "cron":
            return {"cron": self.cron}
        return {}

    def anchored(self, now: datetime) -> ParsedSchedule:
        """An interval without an anchor counts from *now*."""
        if self.kind != "interval" or self.anchor is not None:
            return self
        return replace(self, anchor=_aware(now))


def resolve_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc


def local_iso(moment: datetime | None, timezone_name: str) -> str | None:
    """*moment* on *timezone_name*'s clock, with its offset
    (``2026-10-14T08:30:00+03:00``): what a model restates in its user's
    language without converting anything."""
    if moment is None:
        return None
    return _aware(moment).astimezone(resolve_timezone(timezone_name)).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class LoopCommand:
    """What ``/loop <text>`` asks for.

    ``verb`` is ``create`` (``schedule`` None means a dynamic loop), a
    command on existing loops (``list``, ``cancel``, ``pause``, ``resume``
    with ``target`` the id, or None when none was given), ``help``, or
    ``model``: the text asks for a cadence the harness cannot read, so the
    model makes it.
    """

    verb: str
    prompt: str = ""
    schedule: ParsedSchedule | None = None
    target: str | None = None


def parse_loop_command(raw: str, *, default_timezone: str = "UTC") -> LoopCommand:
    text = raw.strip()
    if not text:
        return LoopCommand(verb="help")

    first, _, rest = text.partition(" ")
    verb = _LOOP_VERBS.get(first.lower())
    rest = rest.strip()
    # ``/loop stop the build if it is green`` is a prompt; ``/loop stop`` and
    # ``/loop cancel <id>`` are commands.
    # ``/loop list new issues every hour`` and ``/loop help me triage …`` are
    # requests: list and help are commands only on their own.
    if verb in {"list", "help"}:
        if not rest or (verb == "list" and rest.lower() == "all"):
            return LoopCommand(verb=verb, target=rest or None)
    elif verb is not None and len(rest.split()) <= 1:
        return LoopCommand(verb=verb, target=rest or None)

    labelled = _SCHEDULE_LABEL_RE.match(text) is not None
    schedule, prompt = _leading_cadence(_SCHEDULE_LABEL_RE.sub("", text, count=1), default_timezone)
    if schedule is not None and labelled:
        prompt = _PROMPT_LABEL_RE.sub("", _NAME_LINE_RE.sub("", prompt)).strip()
    if schedule is None:
        schedule, prompt = _trailing_cadence(text, default_timezone)
    if schedule is not None:
        if not prompt:
            raise ValueError("Say what to run after the cadence, e.g. /loop every day at 9 check the inbox")
        return LoopCommand(verb="create", prompt=prompt, schedule=schedule)
    if _CADENCE_INTENT_RE.search(text):
        return LoopCommand(verb="model", prompt=text)
    return LoopCommand(verb="create", prompt=text)


def parse_dynamic_loop_schedule(*, timezone_name: str = "UTC") -> ParsedSchedule:
    resolve_timezone(timezone_name)
    return ParsedSchedule(kind="dynamic", display=DYNAMIC_LOOP_DISPLAY, timezone_name=timezone_name)


def clamp_dynamic_loop_delay(delay_seconds: int | float) -> int:
    return max(
        DYNAMIC_LOOP_MIN_DELAY_SECONDS,
        min(DYNAMIC_LOOP_MAX_DELAY_SECONDS, int(delay_seconds)),
    )


def parse_schedule(value: str, *, timezone_name: str = "UTC") -> ParsedSchedule:
    """A 5-field cron (read on *timezone_name*'s clock) or an interval
    (``10m``, ``25h``, ``every 2 hours``)."""
    text = value.strip()
    if not text:
        raise ValueError("Schedule cannot be empty.")
    resolve_timezone(timezone_name)
    interval = parse_interval(text)
    if interval is not None:
        return interval_schedule(interval, timezone_name=timezone_name)
    if not croniter.is_valid(text):
        raise ValueError(
            "Invalid schedule. Use an interval like '10m' or a 5-field cron expression.",
        )
    return ParsedSchedule(kind="cron", cron=text, display=humanize_cron(text), timezone_name=timezone_name)


_WORD_SECONDS = {"hourly": 3600, "daily": 86400}


def parse_interval(text: str) -> int | None:
    """Seconds in ``10m`` / ``25h`` / ``every 2 hours`` / ``hourly``; None when it is not one."""
    match = re.fullmatch(rf"(?:every\s+)?(?P<num>\d+)\s*{_UNIT}", text.strip(), re.I)
    if match:
        return _seconds(int(match.group("num")), match.group("unit"))
    match = re.fullmatch(rf"every\s+{_UNIT}", text.strip(), re.I)
    if match:
        return _seconds(1, match.group("unit"))
    return _WORD_SECONDS.get(text.strip().lower())


def interval_schedule(
    seconds: int, *, timezone_name: str = "UTC", anchor: datetime | None = None,
) -> ParsedSchedule:
    resolve_timezone(timezone_name)
    if seconds < MIN_INTERVAL_SECONDS:
        raise ValueError("The shortest interval is 1 minute.")
    return ParsedSchedule(
        kind="interval", seconds=seconds, anchor=anchor,
        display=_display_seconds(seconds), timezone_name=timezone_name,
    )


def calendar_schedule(days: list, times: list[str], *, timezone_name: str = "UTC") -> ParsedSchedule:
    """*days* are names (``mon``, what is stored) or numbers 0-6 from Monday."""
    resolve_timezone(timezone_name)
    given = {weekday_name(d) for d in days}
    wanted = [d for d in WEEKDAYS if d in given]
    if not wanted:
        raise ValueError(f"Days must be among {', '.join(WEEKDAYS)}.")
    clean = sorted({_clock(t) for t in times})
    if not clean:
        raise ValueError("Give at least one time of day, like 09:00.")
    return ParsedSchedule(
        kind="calendar", days=tuple(wanted), times=tuple(clean), timezone_name=timezone_name,
        display=_display_calendar(wanted, clean, timezone_name),
    )


def weekday_name(day) -> str:
    """``mon``..``sun`` from a stored day: a name, or a number 0-6 from Monday."""
    if isinstance(day, int) and not isinstance(day, bool) and 0 <= day < 7:
        return WEEKDAYS[day]
    if isinstance(day, str) and day.strip().lower()[:3] in WEEKDAYS:
        return day.strip().lower()[:3]
    raise ValueError(f"Not a weekday: {day!r}. Days must be among {', '.join(WEEKDAYS)}.")


def once_schedule(at: datetime, *, timezone_name: str = "UTC") -> ParsedSchedule:
    """One run at *at*; a naive *at* is read on *timezone_name*'s clock."""
    tz = resolve_timezone(timezone_name)
    moment = at.replace(tzinfo=tz) if at.tzinfo is None else at
    return ParsedSchedule(
        kind="once", at=moment.astimezone(timezone.utc), timezone_name=timezone_name,
        display=f"Once, {moment.astimezone(tz).strftime('%a %d %b %Y %H:%M')} ({timezone_name})",
    )


def schedule_from_fields(
    *,
    cron: str | None = None,
    interval: str | None = None,
    at: str | None = None,
    days: list[str] | None = None,
    times: list[str] | None = None,
    timezone_name: str = "UTC",
) -> ParsedSchedule:
    """A cadence from exactly one of: a 5-field *cron*, an *interval*
    (``90m``, ``every 2 hours``), a one-off local time *at*
    (``2026-10-28T15:30``, read on *timezone_name*'s clock unless it has an
    offset), or *times* of day on *days* (every day when omitted)."""
    given = [bool(cron), bool(interval), bool(at), bool(days or times)]
    if sum(given) != 1:
        raise ValueError("Give exactly one of: cron, interval, at, or times (with optional days).")
    if cron:
        return parse_schedule(cron, timezone_name=timezone_name)
    if interval:
        seconds = parse_interval(interval)
        if seconds is None:
            raise ValueError(f"Not an interval: {interval!r}. Use e.g. 90m, 2h, every 3 days.")
        return interval_schedule(seconds, timezone_name=timezone_name)
    if at:
        try:
            moment = datetime.fromisoformat(at.strip())
        except ValueError as exc:
            raise ValueError(f"Not a date and time: {at!r}. Use e.g. 2026-10-28T15:30.") from exc
        return once_schedule(moment, timezone_name=timezone_name)
    return calendar_schedule(list(days or WEEKDAYS), list(times or []), timezone_name=timezone_name)


def schedule_from_cadence(
    kind: str,
    *,
    days: list | None = None,
    times: list[str] | None = None,
    tz: str | None = None,
    seconds: int | None = None,
    at: datetime | str | None = None,
    cron: str | None = None,
) -> ParsedSchedule:
    """A cadence as the Studio form and the scheduled-work API give it."""
    zone = tz or "UTC"
    if kind == "calendar":
        return calendar_schedule(list(days or WEEKDAYS), list(times or []), timezone_name=zone)
    if kind == "interval":
        return interval_schedule(int(seconds or 0), timezone_name=zone)
    if kind == "once":
        if at is None:
            raise ValueError("A one-off needs a date and time.")
        return once_schedule(datetime.fromisoformat(at) if isinstance(at, str) else at, timezone_name=zone)
    if kind == "cron":
        if not cron or not croniter.is_valid(cron.strip()):
            raise ValueError("Not a 5-field cron expression.")
        return parse_schedule(cron, timezone_name=zone)
    raise ValueError(f"Unknown cadence kind: {kind!r}. Use one of calendar, interval, once, cron.")


def upcoming_runs(row, *, count: int = 3) -> list[datetime]:
    """The next *count* runs of a live, fixed-cadence schedule, within its
    run limit and expiry; none for a paused, finished or dynamic one."""
    if getattr(row, "status", "active") != "active" or getattr(row, "next_run_at", None) is None:
        return []
    cadence = schedule_from_row(row)
    if cadence.is_dynamic:
        return [_aware(row.next_run_at)]
    limit = getattr(row, "repeat_limit", None)
    if limit is not None:
        count = min(count, max(0, limit - (getattr(row, "run_count", 0) or 0)))
    expires = getattr(row, "expires_at", None)
    fires: list[datetime] = []
    moment: datetime | None = _aware(row.next_run_at)
    while moment is not None and len(fires) < count:
        if expires is not None and moment >= _aware(expires):
            break
        fires.append(moment)
        moment = cadence.next_after(moment)
    return fires


def schedule_from_row(row) -> ParsedSchedule:
    """The cadence of a ``scheduled_sessions`` row, from its ``kind`` and
    ``cadence`` columns.

    Rows are written by the control plane too, so any malformed one is a
    ValueError: its callers mark the schedule failed instead of crashing.
    """
    try:
        return _schedule_from_row(row)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"Malformed schedule cadence: {exc}") from exc


def _schedule_from_row(row) -> ParsedSchedule:
    tz = getattr(row, "timezone", None) or "UTC"
    kind = getattr(row, "kind", None)
    cadence = getattr(row, "cadence", None) or {}
    if kind is None:
        # Only a runtime older than ``kind`` still writes these, while a
        # release rolls out; ``surogates migrate`` backfills the rest.
        legacy = getattr(row, "schedule", None) or {}
        tz = str(legacy.get("timezone_name") or tz)
        if legacy.get("kind") == "dynamic_loop":
            return parse_dynamic_loop_schedule(timezone_name=tz)
        return parse_schedule(str(legacy["cron"]), timezone_name=tz)
    if kind == "calendar":
        return calendar_schedule(cadence.get("days") or [], cadence.get("times") or [],
                                 timezone_name=str(cadence.get("tz") or tz))
    if kind == "interval":
        anchor = cadence.get("anchor")
        return interval_schedule(
            int(cadence.get("seconds") or 0), timezone_name=tz,
            anchor=datetime.fromisoformat(anchor) if anchor else None,
        )
    if kind == "once":
        return once_schedule(datetime.fromisoformat(cadence["at"]), timezone_name=tz)
    if kind == "cron":
        return parse_schedule(str(cadence["cron"]), timezone_name=tz)
    if kind == "dynamic":
        return parse_dynamic_loop_schedule(timezone_name=tz)
    raise ValueError(f"Unknown schedule kind: {kind}")


def humanize_cron(cron: str) -> str:
    parts = cron.split()
    if len(parts) != 5:
        return cron
    minute, hour, day_of_month, month, day_of_week = parts
    every_day = day_of_month == "*" and month == "*"
    if minute.startswith("*/") and hour == "*" and every_day and day_of_week == "*":
        n = minute[2:]
        return f"Every {n} minute{'s' if n != '1' else ''}"
    if minute == "0" and hour.startswith("*/") and every_day and day_of_week == "*":
        n = hour[2:]
        return f"Every {n} hour{'s' if n != '1' else ''}"
    if minute.isdigit() and hour.isdigit() and every_day:
        clock = f"{int(hour):02d}:{int(minute):02d}"
        if day_of_week == "*":
            return f"Every day at {clock}"
        if day_of_week.upper() in {"1-5", "MON-FRI"}:
            return f"Weekdays at {clock}"
        days = _cron_days(day_of_week)
        if days:
            return f"{', '.join(d.capitalize() for d in days)} at {clock}"
    return cron


def _next_cron(cron: str, timezone_name: str, after: datetime) -> datetime:
    """The first wall-clock match of *cron* after *after*, as UTC.

    Iterated on naive local time so a wall time repeated by the autumn
    change fires once (its first occurrence), and one skipped by the
    spring change fires at the instant it maps to.  Across a spring gap the
    mapped instants are not in wall-clock order, so the search goes on a
    few wall hours past the first match and keeps the earliest; inside a
    repeated hour the first matches map to instants already past, so it
    walks on until one is after *after*.
    """
    tz = resolve_timezone(timezone_name)
    local = after.astimezone(tz).replace(tzinfo=None)
    it = croniter(cron, local)
    best: datetime | None = None
    best_wall: datetime | None = None
    for _ in range(2000):
        wall = it.get_next(datetime)
        if best_wall is not None and wall > best_wall + timedelta(hours=3):
            break
        candidate = wall.replace(tzinfo=tz).astimezone(timezone.utc)
        if candidate > after and (best is None or candidate < best):
            best, best_wall = candidate, wall
    if best is None:
        raise ValueError(f"Schedule {cron!r} has no next run.")
    return best


def _leading_cadence(text: str, default_tz: str) -> tuple[ParsedSchedule | None, str]:
    cron = _LEADING_CRON_RE.match(text)
    if cron and croniter.is_valid(cron.group("cron")):
        rest = text[cron.end():]
        tz_match = re.match(_TZ, rest, re.I)
        tz = (tz_match.group("tz") if tz_match else None) or default_tz
        rest = rest[tz_match.end():] if tz_match else rest
        return (
            ParsedSchedule(kind="cron", cron=cron.group("cron"),
                           display=humanize_cron(cron.group("cron")), timezone_name=_tz(tz)),
            _SEPARATOR_RE.sub("", rest, count=1).strip(),
        )
    for kind, pattern in _CADENCE_PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        schedule = _from_match(kind, match, default_tz)
        if schedule is not None:
            return schedule, _SEPARATOR_RE.sub("", text[match.end():], count=1).strip()
    return None, text


def _trailing_cadence(text: str, default_tz: str) -> tuple[ParsedSchedule | None, str]:
    for anchor in reversed(list(_TRAILING_ANCHOR_RE.finditer(text))):
        tail = text[anchor.end():].strip()
        schedule, rest = _leading_cadence(tail, default_tz)
        if schedule is not None and not rest.strip(" .!"):
            return schedule, text[:anchor.start()].strip().rstrip(",:-")
    return None, text


def _from_match(kind: str, match: re.Match, default_tz: str) -> ParsedSchedule | None:
    groups = match.groupdict()
    tz = _tz(groups.get("tz") or default_tz)
    if kind == "interval":
        word = (groups.get("word") or "").lower()
        if word:
            seconds = _WORD_SECONDS[word]
        else:
            seconds = _seconds(int(groups.get("num") or 1), groups["unit"])
        return interval_schedule(seconds, timezone_name=tz)
    if groups.get("daily"):
        days = list(WEEKDAYS)
    else:
        days = _days(groups.get("days") or "")
    if not days:
        return None
    times = [t for t in re.split(r"\s*(?:,|and|&)\s*", groups["times"].strip()) if t]
    return calendar_schedule(days, [_clock(t) for t in times], timezone_name=tz)


def _days(text: str) -> list[str]:
    words = [w for w in re.split(r"\s*(?:,|and|&)\s*", text.strip().lower()) if w]
    if words in (["day"],):
        return list(WEEKDAYS)
    if words in (["weekday"], ["weekdays"]):
        return list(WEEKDAYS[:5])
    if words in (["weekend"], ["weekends"]):
        return ["sat", "sun"]
    days = []
    for word in words:
        day = _DAY_ALIASES.get(word)
        if day is None:
            return []
        days.append(day)
    return days


def _clock(text: str) -> str:
    match = re.fullmatch(r"(?P<h>\d{1,2})(?:[:.h](?P<m>\d{2}))?\s*(?P<ampm>am|pm)?", text.strip(), re.I)
    if match is None:
        raise ValueError(f"Not a time of day: {text!r}")
    hour, minute = int(match.group("h")), int(match.group("m") or 0)
    ampm = (match.group("ampm") or "").lower()
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError(f"Not a time of day: {text!r}")
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        raise ValueError(f"Not a time of day: {text!r}")
    return f"{hour:02d}:{minute:02d}"


def _tz(name: str) -> str:
    name = "UTC" if name.upper() in {"UTC", "GMT"} else name
    resolve_timezone(name)
    return name


def _seconds(amount: int, unit: str) -> int:
    if amount <= 0:
        raise ValueError("Interval must be greater than zero")
    return amount * _UNIT_SECONDS[unit.lower()[0]]


def _display_seconds(seconds: int) -> str:
    return f"Every {duration_words(seconds).removeprefix('1 ')}"


def duration_words(seconds: int) -> str:
    """``2 hours``, ``1 minute``: *seconds* in the largest whole unit."""
    for unit in ("d", "h", "m", "s"):
        size = _UNIT_SECONDS[unit]
        if seconds % size == 0:
            n = seconds // size
            return f"{n} {_UNIT_NAMES[unit]}" + ("" if n == 1 else "s")
    return f"{seconds} seconds"


def _display_calendar(days: list[str], times: list[str], timezone_name: str) -> str:
    if len(days) == 7:
        when = "Every day"
    elif days == list(WEEKDAYS[:5]):
        when = "Weekdays"
    elif days == ["sat", "sun"]:
        when = "Weekends"
    else:
        when = ", ".join(d.capitalize() for d in days)
    return f"{when} at {', '.join(times)} ({timezone_name})"


def _cron_days(field: str) -> list[str]:
    names = {"0": "sun", "7": "sun", "1": "mon", "2": "tue", "3": "wed", "4": "thu", "5": "fri", "6": "sat"}
    out = []
    for part in field.split(","):
        day = names.get(part) or (part.lower() if part.lower() in WEEKDAYS else None)
        if day is None:
            return []
        out.append(day)
    return out


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)

