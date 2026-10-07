"""A project's turns that no message route admitted, counted as a typed one is.

The message route holds a typed message's turn against the user's
allowance and, on a monetized agent, their paid turns; the worker spends
the hold when the turn ends.  A project's threads, their helpers and the
master's report wakes start without a route, so their wake holds the turn
instead.  Ops is not involved beyond its existing planes.
"""

from __future__ import annotations

from typing import Any

from surogates.api.routes._commerce_turn import (
    buyer_identity,
    limit_notice,
    reserve_allowance,
    reserve_commerce,
)
from surogates.channels.memory_boundary import PROJECT_BOUNDARY_PREFIX
from surogates.runtime.platform_client import AllowanceExhaustedError, CommercePaymentRequiredError


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
    when a plane refuses it, None when it may run.

    Raises ``CommerceReserveError`` or ``AllowanceReserveError`` when a
    plane cannot be reached, so the turn fails closed and is retried.
    """
    payload: dict = {}
    if runtime_config_cache is not None:
        try:
            payload = await runtime_config_cache.get(str(session.agent_id)) or {}
        except LookupError:
            pass  # ops does not know the agent: free, as the route reads it
    config = session.config or {}
    try:
        if str(payload.get("commerce_mode") or "free") != "free" and not config.get("commerce_reservations"):
            buyer = await buyer_identity(session_factory, org_id=session.org_id, user_id=session.user_id)
            # The builder's own people, with no buyer identity, pass unmetered.
            if buyer is not None:
                await reserve_commerce(
                    platform_client=platform_client, session_store=session_store, session=session,
                    content=content, buyer=buyer, channel="web",
                )
        if not config.get("allowance_reservations"):
            await reserve_allowance(
                platform_client=platform_client, runtime_payload=payload, session_store=session_store,
                session_id=session.id, agent_id=session.agent_id, content=content,
                end_user_id=str(session.user_id), channel="web", session_config=config,
            )
    except (AllowanceExhaustedError, CommercePaymentRequiredError) as exc:
        return limit_notice(exc.detail, payload.get("commerce_buy_url"))
    return None
