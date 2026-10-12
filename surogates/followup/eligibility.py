"""Decide, for one person and one Program audience, whether today is their day.

Pure: no database, no clock of its own.  The caller supplies "today" in the
Program's timezone, because a day index measured in UTC would move a person
in Bucharest to their next phase at 03:00 local time on the wrong date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

__all__ = ["Audience", "day_index", "ineligibility"]

#: The longest a protocol can run, in days.  Bounds the window fields so a
#: typo like 3650000 is refused rather than silently meaning "forever".
MAX_DAY = 3650


@dataclass(frozen=True)
class Audience:
    """Which enrolled people a Program speaks to.

    ``day_from``/``day_to`` are inclusive and count from the enrollment's
    ``start_date`` (day 0).  ``phases`` restricts to people whose enrollment
    carries one of these phases.  ``silent_days`` keeps only people who have
    not written for at least that many days.
    """

    protocol: str
    day_from: int | None = None
    day_to: int | None = None
    phases: tuple[str, ...] = ()
    silent_days: int | None = None

    @property
    def needs_start_date(self) -> bool:
        return self.day_from is not None or self.day_to is not None

    @classmethod
    def from_config(cls, raw: Any) -> "Audience | None":
        """Parse a projected ``audience`` value; ``None`` means "fixed roster".

        Raises ``ValueError`` on a malformed audience.  The caller must treat
        that as "this Program messages nobody", never as "no audience": the
        fallback would be the explicit roster, which for an audience Program
        is empty by design — or worse, stale.
        """
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("audience must be an object")
        protocol = str(raw.get("protocol") or "").strip()
        if not protocol:
            raise ValueError("audience.protocol is required")

        def _day(key: str) -> int | None:
            value = raw.get(key)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"audience.{key} must be a whole number")
            if not 0 <= value <= MAX_DAY:
                raise ValueError(f"audience.{key} must be between 0 and {MAX_DAY}")
            return value

        day_from, day_to = _day("day_from"), _day("day_to")
        if day_from is not None and day_to is not None and day_from > day_to:
            raise ValueError("audience.day_from must not be after day_to")

        phases_raw = raw.get("phases") or []
        if not isinstance(phases_raw, list):
            raise ValueError("audience.phases must be a list")
        phases = tuple(
            sorted({str(p).strip() for p in phases_raw if str(p).strip()})
        )

        silent = raw.get("silent_days")
        if silent is not None:
            if isinstance(silent, bool) or not isinstance(silent, int):
                raise ValueError("audience.silent_days must be a whole number")
            if not 1 <= silent <= MAX_DAY:
                raise ValueError(
                    f"audience.silent_days must be between 1 and {MAX_DAY}"
                )

        return cls(
            protocol=protocol, day_from=day_from, day_to=day_to,
            phases=phases, silent_days=silent,
        )


def day_index(start_date: date | None, today: date) -> int | None:
    """Days since ``start_date``, with the start itself being day 0."""
    if start_date is None:
        return None
    return (today - start_date).days


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def ineligibility(
    audience: Audience,
    *,
    start_date: date | None,
    phase: str | None,
    enrolled_at: datetime,
    last_inbound_at: datetime | None,
    today: date,
    now: datetime,
) -> str | None:
    """Why this person is not due today, or ``None`` when they are.

    The reason is operator wording: it explains a person's absence from a
    Program's history when someone asks.
    """
    if audience.needs_start_date:
        index = day_index(start_date, today)
        if index is None:
            return "No start date recorded"
        if audience.day_from is not None and index < audience.day_from:
            return f"Day {index} is before day {audience.day_from}"
        if audience.day_to is not None and index > audience.day_to:
            return f"Day {index} is past day {audience.day_to}"

    if audience.phases and (phase or "") not in audience.phases:
        return f"Phase {phase or 'unset'} is not one of {', '.join(audience.phases)}"

    if audience.silent_days is not None:
        # Someone who never wrote is measured from when they were enrolled:
        # otherwise every new enrollment would look silent since forever.
        last = _as_utc(last_inbound_at or enrolled_at)
        if _as_utc(now) - last < timedelta(days=audience.silent_days):
            return f"Wrote within the last {audience.silent_days} days"

    return None
