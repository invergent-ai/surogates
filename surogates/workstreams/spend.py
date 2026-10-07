"""A project's turns that no message route admitted, counted as a typed one is.

The message route holds a typed message's turn against the user's
allowance and, on a monetized agent, their paid turns; the worker spends
the hold when the turn ends.  A project's threads, their helpers and the
master's report wakes start without a route, so their wake holds the turn
instead.  Ops is not involved beyond its existing planes.
"""

from __future__ import annotations

import logging
from typing import Any

from surogates.api.routes._commerce_turn import (
    buyer_identity,
    limit_notice,
    reserve_allowance,
    reserve_commerce,
)
from surogates.channels.memory_boundary import PROJECT_BOUNDARY_PREFIX
from surogates.runtime.platform_client import AllowanceExhaustedError, CommercePaymentRequiredError

logger = logging.getLogger(__name__)


def admitted_at_wake(session: Any) -> bool:
    """Whether *session*'s turns are held when it wakes: a project's session
    the user owns.  A routine run is held as every chat's routine run is."""
    config = getattr(session, "config", None) or {}
    return (
        str(config.get("workspace_boundary") or "").startswith(PROJECT_BOUNDARY_PREFIX)
        and session.user_id is not None
        and session.service_account_id is None
        and session.channel != "scheduled"
    )


async def admit_turn(
    session: Any,
    content: str,
    *,
    platform_client: Any,
    runtime_config_cache: Any,
    session_store: Any,
    session_factory: Any,
) -> str | None:
    """Hold *session*'s turn on each plane that holds nothing for it yet: a
    typed message's turn is already held by its route.  The user's words
    when a plane refuses it, the paid turn this call held going back; None
    when it may run.

    Raises ``CommerceReserveError`` or ``AllowanceReserveError`` when a
    plane cannot be reached, so the turn fails closed and is retried; the
    paid turn this call held goes back then too, and the retry holds again.
    """
    payload: dict = {}
    if runtime_config_cache is not None:
        try:
            payload = await runtime_config_cache.get(str(session.agent_id)) or {}
        except LookupError:
            pass  # ops does not know the agent: free, as the route reads it
    # The live row, not the wake's copy: a settle that ran since the wake read
    # the session has taken the holds that copy shows.
    config = (await session_store.get_session(session.id)).config or {}
    paid = None
    try:
        if str(payload.get("commerce_mode") or "free") != "free" and not config.get("commerce_reservations"):
            buyer = await buyer_identity(session_factory, org_id=session.org_id, user_id=session.user_id)
            # The builder's own people, with no buyer identity, pass unmetered.
            if buyer is not None:
                # Recorded only once the allowance takes the turn too: a hold
                # left listed is one the next wake trusts without asking.
                paid = await reserve_commerce(
                    platform_client=platform_client, session_store=session_store, session=session,
                    content=content, buyer=buyer, channel="web", record=False,
                )
        try:
            if not config.get("allowance_reservations"):
                await reserve_allowance(
                    platform_client=platform_client, runtime_payload=payload, session_store=session_store,
                    session_id=session.id, agent_id=session.agent_id, content=content,
                    end_user_id=str(session.user_id), channel="web", session_config=config,
                )
        except Exception:
            if paid is not None:
                await _release(platform_client, session, paid)
            raise
        if paid is not None:
            await session_store.append_session_config_list(session.id, "commerce_reservations", paid)
    except (AllowanceExhaustedError, CommercePaymentRequiredError) as exc:
        return limit_notice(exc.detail, payload.get("commerce_buy_url"))
    return None


async def _release(platform_client: Any, session: Any, hold: dict) -> None:
    """Give back at nothing spent the paid *hold* this turn took and will
    not run on."""
    try:
        await platform_client.commerce_debit(
            session.agent_id, entitlement_id=str(hold["entitlement_id"]),
            reserved_tokens=hold["reserved_tokens"], actual_tokens=0,
            reservation_id=hold["reservation_id"] or None,
        )
    except Exception:
        logger.warning(
            "Releasing a refused turn's paid hold failed for session %s; the ops reaper will release it",
            session.id, exc_info=True,
        )
