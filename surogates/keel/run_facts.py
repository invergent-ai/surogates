"""What a Keel report says of a session's work: what it used, and the bundle its wake ran with.

Keel refuses a whole report when one field is out of its bounds, and a step
or an item whose report is refused stops.  A bookkeeping field must never
cost that, so each value is brought within Keel's bounds before it is sent: a
count under-reports, a cost that cannot be told is ``"0"``, and a bundle
version that does not fit is none.

A report names the use of the session's work, which is the session's own and
that of every helper under it, and the bundle version of the session's latest
wake.  Reading either asks the store.  A report that cannot read its usage
fails with the store.  A bundle version that cannot be read is none, and
never fails a report.  The telling of an end never fails, since Keel must
hear of the end whatever the database is doing: an end whose usage cannot be
read goes without it.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from surogates.session.events import EventType
from surogates.session.store import TreeUsage

__all__ = [
    "BUNDLE_VERSION_CHARS",
    "MAX_COUNT",
    "bundle_version_in",
    "compile_id_in",
    "usage_report",
    "wake_bundle_version",
    "work_usage",
    "work_usage_or_none",
]

logger = logging.getLogger(__name__)

#: The longest bundle version Keel takes.
BUNDLE_VERSION_CHARS = 200
#: The largest count Keel takes: a signed 64-bit integer.
MAX_COUNT = 2**63 - 1
#: The longest cost Keel takes, in characters.
_COST_CHARS = 40


def bundle_version_in(value: object) -> str | None:
    """*value* when it is a bundle version Keel takes: text of 1 to
    :data:`BUNDLE_VERSION_CHARS` characters.  None otherwise."""
    if isinstance(value, str) and 1 <= len(value) <= BUNDLE_VERSION_CHARS:
        return value
    return None


def compile_id_in(value: object) -> str | None:
    """*value* when it is a knowledge base compile id as ops writes one: a lowercase hyphenated UUID.  None otherwise.

    A page read before reads named their compile, or whose compile left
    anything else, reports no compile rather than a wrong one.
    """
    if not isinstance(value, str):
        return None
    try:
        return value if value == str(UUID(value)) else None
    except ValueError:
        return None


def _count(value: int) -> int:
    return max(0, min(int(value), MAX_COUNT))


def _cost(value: Decimal) -> str:
    """*value* as a plain decimal string Keel takes.  A cost that is not finite
    or is negative, a signed zero included, is ``"0"``, and so is one whose
    text would be longer than Keel takes.  It is never written with an exponent."""
    if not value.is_finite() or value.is_signed():
        return "0"
    text = format(value, "f")
    return text if len(text) <= _COST_CHARS else "0"


def usage_report(used: TreeUsage) -> dict[str, Any]:
    """What a session's work used, as Keel's ``usage`` object, within Keel's bounds."""
    return {
        "input_tokens": _count(used.input_tokens),
        "output_tokens": _count(used.output_tokens),
        "cache_read_tokens": _count(used.cache_read_tokens),
        "estimated_cost_usd": _cost(used.estimated_cost_usd),
    }


async def work_usage(store: Any, session_id: UUID) -> dict[str, Any]:
    """What the session's work used so far, its helpers' use included, as Keel's
    ``usage`` object.  It raises whatever the store raises."""
    return usage_report(await store.session_tree_usage(session_id))


async def work_usage_or_none(store: Any, session_id: UUID) -> dict[str, Any] | None:
    """As :func:`work_usage`, but None when the store cannot say.  It never
    raises: the telling of an end must go on without it."""
    try:
        return await work_usage(store, session_id)
    except Exception as exc:
        logger.warning("Session %s: its usage was not read (%s)", session_id, type(exc).__name__)
        return None


async def wake_bundle_version(store: Any, session_id: UUID) -> str | None:
    """The bundle version the session's latest wake ran with: that of its newest
    ``harness.wake`` event that records one.  None when no wake did (an agent
    without a bundle, or a session whose wakes all came before wakes recorded
    it), when the recorded one is out of Keel's bounds, or when the store
    cannot say.  It never raises."""
    try:
        wake = await store.last_event(session_id, EventType.HARNESS_WAKE, with_key="bundle_version")
    except Exception as exc:
        logger.warning("Session %s: its bundle version was not read (%s)", session_id, type(exc).__name__)
        return None
    if wake is None:
        return None
    return bundle_version_in(wake.data.get("bundle_version"))
