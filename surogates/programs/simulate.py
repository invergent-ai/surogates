"""A dry run: when a journey's steps would write to one person.

Pure, and answers with the same cadence and audience rules materialize
uses, assuming the person never writes back and nothing is skipped.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from surogates.followup.eligibility import Audience, ineligibility
from surogates.programs.cadence import iter_occurrences

__all__ = ["simulate"]


def simulate(
    steps: list[dict],
    *,
    start_date: date | None,
    timezone: str | None,
    days: int,
    today: date,
    phase: str | None = None,
) -> list[dict]:
    """``[{at, step_id, name}]`` over *days* days from *today*, in order.

    *timezone* is the person's; without one each step fires in its own.  A
    step whose audience cannot be read never fires, as in materialize.
    """
    events = []
    for step in steps:
        try:
            audience = Audience.from_config(step.get("audience"))
        except ValueError:
            continue
        zone = ZoneInfo(timezone or str(step.get("timezone") or "UTC"))
        begin = datetime.combine(today, time(0), tzinfo=zone)
        end = begin + timedelta(days=days)
        for at in iter_occurrences(
            begin - timedelta(seconds=1), weekdays=step.get("weekdays") or [],
            times_local=step.get("times_local") or [], timezone=zone.key,
        ):
            if at >= end:
                break
            if audience is not None and ineligibility(
                audience, start_date=start_date, phase=phase, enrolled_at=begin,
                last_inbound_at=None, today=at.astimezone(zone).date(), now=at,
            ) is not None:
                continue
            events.append({"at": at.isoformat(), "step_id": step.get("id"), "name": step.get("name") or ""})
    return sorted(events, key=lambda e: e["at"])
