"""Per-turn commerce enforcement shared by every buyer-facing channel.

The website widget binds the buyer identity onto the session at
bootstrap; the web app derives it from the authenticated user at
message time. Both feed the same authorize → reserve → (worker)
settle pipeline: ops holds tokens before the turn runs and the
worker settles the ``commerce_reservations`` list afterwards,
regardless of channel.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status

from surogates.runtime.platform_client import (
    AllowanceExhaustedError,
    CommercePaymentRequiredError,
)
from surogates.session.store import SessionStore

logger = logging.getLogger(__name__)


def estimate_turn_tokens(content: str) -> int:
    """~4-chars/token heuristic — the same shape ops's
    estimate_prompt_tokens computes for the hosted buy page
    ((chars + 16) // 4 with the per-message framing overhead), so a
    visitor's hold is the same through either surface."""
    return (len(content) + 16) // 4


async def authorize_commerce_turn(
    request: Request,
    session,
    content: str,
    *,
    buyer: dict | None = None,
) -> None:
    """Gate one visitor message behind the agent's monetization mode.

    Free agents (the default, and every agent while the platform's
    commerce rollout flag is off — ops reports them as free) return
    immediately.  Monetized agents require a session-bound buyer
    identity and a successful ops-side token reservation; the receipt
    is pinned on ``session.config`` for the worker to settle after the
    turn.  402 details are structured (``{"code", "buy_url"}``) so the
    widget can render a paywall instead of a generic error.
    """
    payload = await runtime_commerce_payload(request, str(session.agent_id))
    mode = str(payload.get("commerce_mode") or "free")
    if mode == "free":
        return
    buy_url = payload.get("commerce_buy_url")
    if buyer is None:
        buyer = (session.config or {}).get("commerce_buyer") or {}
    if not buyer.get("firebase_uid"):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": "sign_in_required", "buy_url": buy_url},
        )
    client = getattr(request.app.state, "platform_client", None)
    if client is None:
        logger.error("platform_client is not wired on app.state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Access checks are temporarily unavailable; try again.",
        )
    try:
        receipt = await client.commerce_authorize(
            str(session.agent_id),
            firebase_uid=buyer["firebase_uid"],
            estimated_tokens=estimate_turn_tokens(content),
            email=buyer.get("email"),
            name=buyer.get("name"),
        )
    except CommercePaymentRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": exc.detail, "buy_url": buy_url},
        ) from exc
    except Exception as exc:
        # Fail closed: a paid agent must not serve free turns because
        # the metering plane blinked.
        logger.error(
            "Commerce authorization failed for session %s: %s",
            session.id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Access checks are temporarily unavailable; try again.",
        ) from exc
    if receipt.get("entitlement_id"):
        store = get_session_store(request)
        # Appended, not overwritten: a second message can land while a
        # turn is still running, and each hold must survive until the
        # worker's settlement takes the whole list atomically.
        await store.append_session_config_list(
            session.id,
            "commerce_reservations",
            {
                "entitlement_id": receipt["entitlement_id"],
                "reserved_tokens": int(receipt.get("reserved_tokens") or 0),
                "reservation_id": receipt.get("reservation_id") or "",
            },
        )


async def authorize_allowance_turn(
    request: Request,
    session,
    content: str,
    *,
    end_user_id: str,
) -> None:
    """Gate one end-user turn behind their per-user token allowance.

    Unlike :func:`authorize_commerce_turn` this applies to **every**
    signed-in end-user regardless of identity provider or channel, and is
    independent of the agent's commerce mode: it enforces the operator's
    per-user slice of their own subscription.  It is a no-op unless ops
    projects a positive ``end_user_token_allowance`` (kill-switch on and
    the operator set a cap), so free/uncapped agents are unaffected.

    On success the reservation receipt is pinned on ``session.config`` for
    the worker to settle after the turn.  An exhausted allowance raises
    402 ``allowance_exhausted``.
    """
    payload = await runtime_commerce_payload(request, str(session.agent_id))
    allowance = payload.get("end_user_token_allowance")
    if allowance is None:
        return
    client = getattr(request.app.state, "platform_client", None)
    if client is None:
        logger.error("platform_client is not wired on app.state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Access checks are temporarily unavailable; try again.",
        )
    try:
        receipt = await client.allowance_authorize(
            str(session.agent_id),
            end_user_id=end_user_id,
            estimated_tokens=estimate_turn_tokens(content),
        )
    except AllowanceExhaustedError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": exc.detail or "allowance_exhausted"},
        ) from exc
    except Exception as exc:
        # Fail closed: a capped agent must not serve unmetered turns
        # because the allowance plane blinked.
        logger.error(
            "Allowance authorization failed for session %s: %s",
            session.id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Access checks are temporarily unavailable; try again.",
        ) from exc
    if receipt.get("allowance_id"):
        store = get_session_store(request)
        # Appended, not overwritten: a second message can land while a
        # turn is still running, and each hold must survive until the
        # worker settles the whole list atomically.
        await store.append_session_config_list(
            session.id,
            "allowance_reservations",
            {
                "allowance_id": receipt["allowance_id"],
                "reserved_tokens": int(receipt.get("reserved_tokens") or 0),
                "reservation_id": receipt.get("reservation_id") or "",
            },
        )


def get_session_store(request: Request) -> "SessionStore":
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store not available.",
        )
    return store


async def runtime_commerce_payload(request: Request, agent_id: str) -> dict:
    """The agent's runtime-config payload, or ``{}`` when the cache is
    absent or has no entry — reads as free mode either way. The single
    cache-access point for commerce gating."""
    runtime_cache = getattr(request.app.state, "runtime_config_cache", None)
    if runtime_cache is None:
        return {}
    try:
        return await runtime_cache.get(agent_id) or {}
    except LookupError:
        return {}


async def firebase_buyer_identity(
    request: Request, tenant,
) -> dict | None:
    """``{firebase_uid, email, name}`` for the signed-in user, or
    ``None`` when the principal has no project-Firebase identity —
    the one rule for "can this user be metered/charged", shared by
    the web message gate and the Plan & tokens tab."""
    if getattr(tenant, "user_id", None) is None:
        return None
    from sqlalchemy import select

    from surogates.db.models import User

    session_factory = request.app.state.session_factory
    async with session_factory() as db:
        user = await db.scalar(
            select(User).where(
                User.id == tenant.user_id,
                User.org_id == tenant.org_id,
            )
        )
    if (
        user is None
        or not (user.auth_provider or "").startswith("firebase:")
        or not user.external_id
    ):
        return None
    return {
        "firebase_uid": user.external_id,
        "email": user.email,
        "name": user.display_name,
    }
