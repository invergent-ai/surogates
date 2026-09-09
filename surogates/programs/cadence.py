"""Turn a Program's cadence into UTC instants.

A cadence is a weekday set plus local times in a named zone.  Not cron: the
operator UI has to preview the next occurrences and explain DST, and both are
near-impossible to express over a cron string.

"Every 12 hours" is expressed as two times of day, never as an interval.  An
interval anchored to a start instant drifts, and eventually asks a patient for
their blood pressure at 03:00.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

_UTC = ZoneInfo("UTC")

_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

#: A year of any weekday set fits comfortably; the bound only stops an empty
#: or unsatisfiable cadence from looping forever.
_MAX_DAYS_SCANNED = 400


def next_occurrences(
    after: datetime,
    *,
    weekdays: list[str],
    times_local: list[str],
    timezone: str,
    count: int = 1,
) -> list[datetime]:
    """The next *count* fire instants strictly after *after*.

    *after* and the returned instants are naive UTC.  Returns fewer than
    *count* — possibly none — when the cadence is empty or cannot be
    satisfied within the scan window.
    """
    if not weekdays or not times_local:
        return []

    tz = ZoneInfo(timezone)
    wanted = {_WEEKDAYS[d] for d in weekdays if d in _WEEKDAYS}
    if not wanted:
        return []

    parsed = sorted(
        time(int(hh), int(mm))
        for hh, _, mm in (t.partition(":") for t in times_local)
    )

    found: list[datetime] = []
    day = after.replace(tzinfo=_UTC).astimezone(tz).date()
    for _ in range(_MAX_DAYS_SCANNED):
        if day.weekday() in wanted:
            for slot in parsed:
                local = datetime.combine(day, slot, tzinfo=tz)
                # A nonexistent local time (spring forward) resolves to a
                # real instant rather than raising; a repeated one (autumn
                # back) resolves to its first occurrence, so it fires once.
                utc = local.astimezone(_UTC).replace(tzinfo=None)
                if utc > after and utc not in found:
                    found.append(utc)
                    if len(found) == count:
                        return found
        day += timedelta(days=1)
    return found
