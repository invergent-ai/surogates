"""Read-only browser-minutes gate backed by the surogate-ops DB.

Browser pods are billed per wall-clock minute by surogate-ops'
``BrowserMonitor`` after they terminate. Enforcement, though, has to
happen *before* a pod starts — at the point of use, not at session
creation — so a project that is out of minutes simply can't open a new
browser while a plain chat session keeps working.

This module is the use-time gate: given a project (``org_id``), it
reads the ``browser_minutes`` row from the ops ``credit_balances``
table and raises :class:`BrowserCreditsExhaustedError` when the balance
is empty. Writes stay entirely on the ops side; this is SELECT-only,
mirroring the KB tools' access pattern.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import sqlalchemy as sa

from surogates.browser.base import BrowserCreditsExhaustedError
from surogates.db.ops_engine import get_ops_session_factory
from surogates.db.ops_models import OpsCreditBalance

logger = logging.getLogger(__name__)

_BROWSER_MINUTES_RESOURCE = "browser_minutes"


async def assert_browser_minutes_available(org_id: str) -> None:
    """Raise if ``org_id`` has no web-browsing minutes left.

    No-ops (allows provisioning) when:
      - ``org_id`` is empty — nothing to bill, can't gate.
      - the ops DB is not configured — self-hosted / OSS workers run
        without a billing platform, same as the KB tools.
      - no ``browser_minutes`` row exists — a project predating the
        credit seeding shouldn't be locked out by a missing row.
      - the cycle has ended but the writer side hasn't refreshed the
        plan grant yet — staying lenient here avoids a false block at
        the month boundary; the next ops-side touch re-grants the cycle.

    Raises :class:`BrowserCreditsExhaustedError` only when a current
    cycle's combined plan + top-up balance is at or below zero.
    """
    if not org_id:
        return

    factory = get_ops_session_factory()
    if factory is None:
        return

    async with factory() as session:
        row = (await session.execute(
            sa.select(OpsCreditBalance).where(
                OpsCreditBalance.project_id == org_id,
                OpsCreditBalance.resource == _BROWSER_MINUTES_RESOURCE,
            )
        )).scalar_one_or_none()

    if row is None:
        return

    total = (row.plan_remaining or 0) + (row.topup_remaining or 0)
    if total > 0:
        return

    if _cycle_pending_refresh(row.cycle_end):
        logger.info(
            "browser-minutes gate: project=%s balance<=0 but cycle ended "
            "%s — allowing provision pending ops-side refresh",
            org_id, row.cycle_end,
        )
        return

    raise BrowserCreditsExhaustedError(
        "This project has no web-browsing minutes remaining.",
    )


def _cycle_pending_refresh(cycle_end: datetime | None) -> bool:
    """True when the plan bucket is due a refresh the writer hasn't run.

    A ``cycle_end`` in the past means surogate-ops will re-grant the
    plan bucket on its next lazy refresh; until then the stored
    ``plan_remaining`` understates what the project actually has, so the
    gate should not block.
    """
    if cycle_end is None:
        return False
    if cycle_end.tzinfo is None:
        cycle_end = cycle_end.replace(tzinfo=timezone.utc)
    return cycle_end <= datetime.now(timezone.utc)


__all__ = ["assert_browser_minutes_available"]
