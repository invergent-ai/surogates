"""Public-website channel routes.

Three endpoints implement the end-to-end flow an anonymous visitor
needs to talk to the deployment's agent embedded on a public website:

* ``POST /v1/website/sessions`` — bootstrap.  Authenticated with the
  configured publishable key (``surg_wk_...``) plus an ``Origin``
  header in the configured allow-list.  Creates a session, issues the
  visitor's HttpOnly cookie, returns the CSRF token the browser
  client must echo on every subsequent state-changing request.
* ``POST /v1/website/sessions/{id}/messages`` — send a user message.
  Requires the cookie plus a matching ``X-CSRF-Token`` header
  (double-submit CSRF).  The cookie's baked-in origin claim is
  re-checked against the request origin so a stolen cookie cannot be
  replayed from a different embed.
* ``GET /v1/website/sessions/{id}/events`` — SSE stream of session
  events.  Cookie-authenticated; ``EventSource`` cannot set custom
  headers, so the CSRF header isn't required (GETs are safe by
  CSRF's standard assumption — nothing is mutated).

The deployment-wide on-switch comes from :class:`WebsiteSettings`; the
agent identity is resolved per-request from the publishable key via
``channel_routing(website:<key>)`` -- the same mechanism the
Slack/Telegram adapters use -- so each agent has its own key.  The
origin allow-list and session message cap are per-agent when the
routing row's ``config`` carries them (projected from Studio's Website
channel form), falling back to the global :class:`WebsiteSettings`
values otherwise.  Origin validation is the conjunction of two checks:
the effective allow-list (authoritative) and the session cookie's
``origin`` claim (anchors a bootstrapped session to the embed it came
from).  A request must satisfy both; cookie-authenticated calls also
re-resolve the routing row so deactivating the channel cuts live
sessions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from surogates.channels.website_keys import (
    PUBLISHABLE_KEY_PREFIX,
    is_publishable_key,
)
from surogates.channels.website_origin import (
    normalize_origin,
    origin_allowed,
    parse_allowed_origins,
)
from surogates.channels.website_session import (
    COOKIE_NAME,
    CSRF_HEADER_NAME,
    DEFAULT_SESSION_TTL_SECONDS,
    WebsiteSessionClaims,
    create_website_session_token,
    decode_website_session_token,
    generate_csrf_token,
    verify_csrf_token,
)
from surogates.config import Settings, enqueue_session
from surogates.api.routes._commerce_turn import (
    authorize_allowance_turn,
    authorize_commerce_turn,
    estimate_turn_tokens,
    get_session_store,
)
from surogates.runtime.platform_client import (
    CommercePaymentRequiredError,
    PlatformAuthError,
)
from surogates.channels.constants import multi_session_disabled
from surogates.session.events import EventType
from surogates.session.models import REUSABLE_SESSION_STATUSES
from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.storage.tenant import agent_session_bucket
from surogates.tenant.auth.firebase import (
    FirebaseTokenError,
    verify_firebase_id_token,
)
from surogates.tenant.auth.jwt import InvalidTokenError

logger = logging.getLogger(__name__)

router = APIRouter()


WEBSITE_CHANNEL = "website"
# Upper bound on a single visitor message.  Website widgets are an
# interactive surface; we cap much lower than the API channel so a
# single misbehaving client cannot submit multi-megabyte prompts.
_MAX_MESSAGE_LENGTH = 8_000
# Terminal session statuses that close the SSE stream.  Mirrors the
# interactive web channel so the visitor client can share event-handling
# logic if it wants.
_TERMINAL_STATUSES = frozenset({"completed", "archived"})
_MAX_STREAM_DURATION = 300
_POLL_INTERVAL = 0.5


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class BootstrapResponse(BaseModel):
    """Body returned on a successful bootstrap.

    ``session_id`` is redundant with the cookie claims but convenient
    for clients that want to display a stable identifier.  ``csrf_token``
    is what the browser client must echo on every subsequent POST; the
    server compares it constant-time against the cookie JWT's ``csrf``
    claim.  ``agent_name`` is the deployment's :attr:`Settings.agent_id`
    (typically a slug like ``"support-bot"``) so the widget has a
    stable label to render.
    """

    session_id: UUID
    csrf_token: str
    expires_at: int
    agent_name: str


class BootstrapRequest(BaseModel):
    """Optional bootstrap body.

    ``firebase_id_token`` binds a signed-in end user to the visitor
    session — required before a monetized agent will accept messages.
    The embedding site supplies it via the widget's
    ``getFirebaseIdToken`` hook (same Firebase project the agent's
    self-registration and hosted buy page use). Anonymous bootstraps
    (no body) keep working unchanged for free agents.
    """

    firebase_id_token: str | None = None


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=_MAX_MESSAGE_LENGTH)


class SendMessageResponse(BaseModel):
    event_id: int
    status: str = "processing"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings




def _require_website_enabled(settings: Settings) -> None:
    """Refuse every website-channel request when the channel is disabled.

    A deployment without ``website.enabled`` should look identical to
    the embed as one that does not implement the route at all, so we
    return 404 rather than 503 — the path effectively does not exist
    when the channel is off.
    """
    if not settings.website.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website channel is not enabled on this deployment.",
        )


def _extract_bearer(request: Request) -> str | None:
    """Return the raw bearer token from the Authorization header, if any."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return header[7:].strip() or None


def _extract_origin(request: Request) -> str:
    """Return the request's ``Origin`` header, or raise 400.

    Every public-website request must carry an Origin header — browsers
    always set one on cross-origin or credentialled requests, and a
    server-to-server attempt without one is not a browser embed.  This
    keeps the rest of the route simple: below here, ``origin`` is a
    string we can compare against the allow-list directly.
    """
    origin = request.headers.get("origin")
    if not origin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Origin header; the website channel requires a browser origin.",
        )
    return origin


def _set_session_cookie(
    response: Response,
    *,
    token: str,
    expires_seconds: int,
) -> None:
    """Set the HttpOnly session cookie scoped to the API origin.

    ``SameSite=None`` with ``Secure=True`` is the only combination that
    permits cross-site credentialled requests — a website embedded on
    ``customer.com`` that talks to our API domain is cross-site by
    definition, and the cookie has to ride along.

    ``Path=/`` is intentionally broad.  The API is mounted behind
    ``StripApiPrefixMiddleware``, which means the browser sees
    ``/api/v1/website/...`` but the FastAPI routes live at
    ``/v1/website/...``; pinning the cookie to either form would break
    the other.  Cross-route leakage is not a concern because (a) the
    cookie is ``HttpOnly`` so only this server reads it, (b) the JWT
    ``type`` claim is ``website_session`` and every other route rejects
    that type at the auth layer, and (c) the global auth middleware
    doesn't read cookies at all — only ``Authorization: Bearer`` and
    ``?token=`` query params.
    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=expires_seconds,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


async def _resolve_claims_from_cookie(
    request: Request,
    *,
    optional: bool = False,
) -> WebsiteSessionClaims | None:
    """Decode the session cookie from *request* or raise 401.

    With ``optional`` a missing/invalid cookie returns ``None`` instead
    (the single-session bootstrap probes the cookie without failing the
    request).

    The decoded claims are the authority for session ownership on
    messages/events — the cookie carries the session id, org, origin,
    and CSRF token.  A missing cookie is an unauthenticated request
    (expired or never bootstrapped); a malformed cookie is an expired
    or forged JWT; either way, 401.
    """
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        if optional:
            return None
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing website session cookie; call POST /v1/website/sessions first.",
        )
    try:
        return decode_website_session_token(raw)
    except InvalidTokenError as exc:
        if optional:
            return None
        logger.debug("Invalid website session cookie: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired website session; re-bootstrap required.",
        ) from exc


def _enforce_origin_binding(
    claims: WebsiteSessionClaims,
    request_origin: str,
    allowed_origins: tuple[str, ...],
) -> None:
    """Fail the request unless origin matches both the cookie and config.

    A stolen cookie replayed from another embed — even another embed
    of the same deployment's agent — fails here because
    ``claims.origin`` captures the origin at bootstrap time; ops
    shrinking the allow-list takes effect on the next request because
    the allow-list is read from settings on every call.
    """
    if normalize_origin(request_origin) != claims.origin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request origin does not match the session's bootstrap origin.",
        )
    if not origin_allowed(request_origin, allowed_origins):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request origin is not in the configured allow-list.",
        )


def _routing_channel_config(routing: dict | None) -> dict:
    """The per-agent behavior blob projected into ``channel_routing.config``."""
    config = (routing or {}).get("config")
    return config if isinstance(config, dict) else {}


def _agent_allowed_origins(routing_config: dict) -> tuple[str, ...] | None:
    """Per-agent origin allow-list from routing config, or ``None`` when the
    agent has not configured one (fall back to the deployment-global list).

    Ops projects a parsed list; a CSV string is accepted too so a
    hand-written routing row behaves the same.  Both funnel through
    :func:`parse_allowed_origins` so normalization has one home.
    """
    raw = routing_config.get("allowed_origins")
    if raw is None:
        return None
    csv = raw if isinstance(raw, str) else ",".join(str(origin) for origin in raw)
    return parse_allowed_origins(csv) or None


async def _reusable_cookie_session(
    request: Request,
    *,
    agent_id: str,
    org_uuid: UUID,
    normalized_origin: str,
    publishable_key: str,
):
    """The visitor's cookie-bound session, for single-session bootstraps.

    With the agent's "multi session" capability off, a re-bootstrap from
    a browser still holding a valid session cookie must return the
    visitor to their existing conversation instead of minting a new
    session.  The cookie is trusted only when its claims bind to the
    same publishable key, org and origin this bootstrap resolved; on a
    missing/invalid cookie, any claim mismatch, or a session that is
    gone or not reusable, the caller falls through to a fresh create.
    """
    claims = await _resolve_claims_from_cookie(request, optional=True)
    if claims is None:
        return None
    if (
        claims.channel_identifier != publishable_key
        or claims.origin != normalized_origin
        or claims.org_id != org_uuid
    ):
        return None
    store = get_session_store(request)
    try:
        session = await store.get_session(claims.session_id)
    except SessionNotFoundError:
        return None
    config = session.config or {}
    if (
        session.agent_id != agent_id
        or session.channel != WEBSITE_CHANNEL
        or session.status not in REUSABLE_SESSION_STATUSES
        # Only the canonical single-session conversation qualifies — a
        # cookie left over from a multi-session era is not adopted.
        or config.get("single_session") is not True
    ):
        return None
    # A capped-out session must not be reused: the 429 on send tells the
    # visitor to bootstrap a new session, so the bootstrap has to
    # actually deliver one (message_count never resets and the cap is
    # pinned at creation).
    cap = config.get("session_message_cap") or 0
    if cap and (session.message_count or 0) >= cap:
        return None
    return session


async def _load_and_authorize_session(
    request: Request,
    path_session_id: UUID,
) -> WebsiteSessionClaims:
    """Resolve cookie, verify session id, enforce origin binding.

    Used by every cookie-authenticated route.  The path session id
    must match the claim so a visitor of one session cannot target
    another visitor's session by swapping the URL — the session JWT
    scopes to exactly one session.

    When the cookie names its bootstrap ``channel_identifier``, the
    per-agent routing row is re-resolved on every call: turning the
    agent's website channel off (row deactivated) cuts live sessions
    with 403, and a per-agent origin allow-list — when configured —
    replaces the deployment-global one.  Cookies minted before the
    claim existed keep the legacy global-only behavior.
    """
    settings = _get_settings(request)
    _require_website_enabled(settings)

    claims = await _resolve_claims_from_cookie(request)
    if claims.session_id != path_session_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {path_session_id} not found.",
        )

    allowed = parse_allowed_origins(settings.website.allowed_origins)
    cache = getattr(request.app.state, "channel_routing_cache", None)
    if claims.channel_identifier and cache is not None:
        routing = await cache.get(f"website:{claims.channel_identifier}")
        if not routing or not routing.get("agent_id"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The website channel for this agent has been turned off.",
            )
        agent_origins = _agent_allowed_origins(_routing_channel_config(routing))
        if agent_origins is not None:
            allowed = agent_origins

    request_origin = _extract_origin(request)
    _enforce_origin_binding(claims, request_origin, allowed)
    return claims


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def _resolve_website_routing(request: Request) -> dict:
    """Resolve the owning agent for a website request from its publishable key.

    The ``Authorization: Bearer`` publishable key is both the identifier and
    the auth: it is looked up in ``channel_routing`` (``website:<key>``) to
    find ``(org_id, agent_id)`` — the same way the Slack/Telegram adapters
    resolve their tenant.  A missing or inactive row is indistinguishable from
    "no such key" and returns 404, so a wrong key cannot enumerate agents.
    """
    token = _extract_bearer(request)
    if not token or not is_publishable_key(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Website bootstrap requires a publishable key "
                f"(prefix {PUBLISHABLE_KEY_PREFIX!r}) in the Authorization header."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    cache = getattr(request.app.state, "channel_routing_cache", None)
    if cache is None:
        # A wired deployment always has this; None means a startup/config
        # bug. Surface it as 503 (not a misleading 404) so it's debuggable.
        logger.error("channel_routing_cache is not wired on app.state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Website channel routing is temporarily unavailable.",
        )
    routing = await cache.get(f"website:{token}")
    if not routing or not routing.get("agent_id") or not routing.get("org_id"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown or inactive website key.",
        )
    return routing


async def _enforce_website_rate_limit(
    request: Request, org_id: str, agent_id: str,
) -> None:
    """Per-tenant rate limit keyed on the agent resolved from the publishable
    key / session cookie -- so the website channel never needs ``?agent_id=``
    on the request. Mirrors ``rate_limit_dep`` (same limiter, 60s window,
    default ceiling, same 429); the per-agent ``governance.rate_limit_rpm``
    override is intentionally not applied on this anonymous public channel.
    """
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    if not await limiter.try_consume(org_id, agent_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="per-tenant rate limit exceeded",
        )


# ---------------------------------------------------------------------------
# Commerce enforcement (monetized agents)
# ---------------------------------------------------------------------------


async def _resolve_commerce_buyer(
    request: Request, agent_id: str, firebase_id_token: str,
) -> dict | None:
    """Verify a visitor-supplied Firebase ID token and return the buyer
    identity to pin on the session, or ``None`` when no identity can
    be bound.

    Verification runs against the agent project's own Firebase (the
    same one the buy page and self-registration use), so the returned
    ``firebase_uid`` IS the buyer identity ops meters against. Claim
    normalisation is shared with the ``/auth/firebase/exchange`` flow
    so both paths derive the identical identity for the same user.

    Failures degrade to an anonymous session rather than failing the
    bootstrap: binding an identity only matters for monetized agents,
    and those enforce at message time (a missing buyer 402s with
    ``sign_in_required``, which the widget answers by re-bootstrapping
    with a fresh token) — a stale token or an unconfigured project
    must not break the widget for free agents.
    """
    from surogates.api.routes.auth import (
        _display_name_from_firebase_claims,
        _email_from_firebase_claims,
    )

    runtime_cache = getattr(request.app.state, "runtime_config_cache", None)
    firebase_cache = getattr(request.app.state, "firebase_config_cache", None)
    payload: dict = {}
    if runtime_cache is not None:
        try:
            payload = await runtime_cache.get(agent_id) or {}
        except LookupError:
            payload = {}
    project_id = payload.get("project_id")
    fb = None
    if firebase_cache is not None and project_id:
        try:
            fb = await firebase_cache.get(project_id)
        except LookupError:
            fb = None
    if fb is None:
        logger.info(
            "Ignoring firebase_id_token at website bootstrap for agent "
            "%s: project has no Firebase auth configured",
            agent_id,
        )
        return None
    try:
        claims = await verify_firebase_id_token(
            firebase_id_token, fb.firebase_project_id,
        )
    except FirebaseTokenError as exc:
        logger.info(
            "Ignoring invalid firebase_id_token at website bootstrap "
            "for agent %s: %s",
            agent_id,
            exc,
        )
        return None
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        return None
    email, _verified = _email_from_firebase_claims(claims)
    return {
        "firebase_uid": subject,
        "email": email,
        "name": _display_name_from_firebase_claims(claims, email),
    }


def _issue_bootstrap_response(
    response: Response,
    *,
    session_id: UUID,
    org_uuid: UUID,
    origin: str,
    publishable_key: str,
    agent_id: str,
) -> BootstrapResponse:
    """Mint the CSRF token + session cookie and build the bootstrap body.

    The cookie-mint contract (TTL, claim set, response shape) has one
    home — both the fresh-create and single-session reuse paths return
    through here.
    """
    csrf_token = generate_csrf_token()
    cookie_token = create_website_session_token(
        session_id=session_id,
        org_id=org_uuid,
        origin=origin,
        csrf_token=csrf_token,
        channel_identifier=publishable_key,
    )
    _set_session_cookie(
        response, token=cookie_token, expires_seconds=DEFAULT_SESSION_TTL_SECONDS,
    )
    return BootstrapResponse(
        session_id=session_id,
        csrf_token=csrf_token,
        expires_at=int(time.time()) + DEFAULT_SESSION_TTL_SECONDS,
        agent_name=agent_id,
    )


@router.post(
    "/website/sessions",
    response_model=BootstrapResponse,
    status_code=status.HTTP_201_CREATED,
)
async def bootstrap_website_session(
    request: Request,
    response: Response,
    body: BootstrapRequest | None = None,
) -> BootstrapResponse:
    """Exchange a publishable key + approved origin for a session cookie.

    The publishable key resolves the owning agent via ``channel_routing``
    (``website:<key>``); the request Origin is checked against the agent's
    allow-list from the routing config, falling back to the deployment
    global.  Creates a fresh session owned by the agent's org (no user
    row), mints the HttpOnly cookie the browser presents on subsequent
    requests, and returns the CSRF token echoed in ``X-CSRF-Token``.
    """
    settings = _get_settings(request)
    _require_website_enabled(settings)
    routing = await _resolve_website_routing(request)
    await _enforce_website_rate_limit(
        request, routing["org_id"], routing["agent_id"],
    )

    request_origin = _extract_origin(request)
    routing_config = _routing_channel_config(routing)
    allowed = _agent_allowed_origins(routing_config)
    if allowed is None:
        allowed = parse_allowed_origins(settings.website.allowed_origins)
    if not origin_allowed(request_origin, allowed):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request origin is not in the configured allow-list.",
        )

    agent_id = routing["agent_id"]
    try:
        org_uuid = UUID(routing["org_id"])
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tenant org_id is not a valid UUID.",
        ) from exc

    if not settings.storage.bucket:
        # ``agent_session_bucket`` raises ValueError on an empty bucket,
        # which Starlette would surface as a 500 with a stack trace
        # through the public website surface.  Match the org_id /
        # llm.model fail-loud shape and return 503 explicitly.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage bucket is not configured (settings.storage.bucket is empty).",
        )

    store = get_session_store(request)
    storage = request.app.state.storage

    session_id = uuid.uuid4()
    bucket = agent_session_bucket(settings.storage.bucket)
    normalized_origin = normalize_origin(request_origin)

    publishable_key = _extract_bearer(request) or ""

    single_session = multi_session_disabled(routing_config)
    if single_session:
        existing = await _reusable_cookie_session(
            request,
            agent_id=agent_id,
            org_uuid=org_uuid,
            normalized_origin=normalized_origin,
            publishable_key=publishable_key,
        )
        if existing is not None:
            # Buyer identity pins at bootstrap (see the fresh-create path
            # below), and the sign-in recovery flow re-bootstraps with a
            # Firebase token expecting exactly that — so a reused session
            # must absorb the token too, or a signed-in buyer would 402
            # forever against their buyer-less canonical session.
            if body is not None and body.firebase_id_token:
                buyer = await _resolve_commerce_buyer(
                    request, agent_id, body.firebase_id_token,
                )
                if buyer is not None and (
                    (existing.config or {}).get("commerce_buyer") != buyer
                ):
                    await store.update_session_config_key(
                        existing.id, "commerce_buyer", buyer,
                    )
            # Absorb the per-buyer embed identity onto a reused session too,
            # or a canonical session created before the key carried an
            # embed_end_user_id would skip the allowance gate on every turn.
            embed_eu = routing_config.get("embed_end_user_id")
            if embed_eu and (existing.config or {}).get(
                "embed_end_user_id",
            ) != str(embed_eu):
                await store.update_session_config_key(
                    existing.id, "embed_end_user_id", str(embed_eu),
                )
            response.status_code = status.HTTP_200_OK
            return _issue_bootstrap_response(
                response,
                session_id=existing.id,
                org_uuid=org_uuid,
                origin=normalized_origin,
                publishable_key=publishable_key,
                agent_id=agent_id,
            )

    config: dict = {
        "storage_bucket": bucket,
        "workspace_path": storage.resolve_workspace_path(bucket, session_id),
        "website_origin": normalized_origin,
        "channel_identifier": publishable_key,
    }
    if single_session:
        # The fresh session becomes the visitor's canonical conversation
        # that every later re-bootstrap resolves to.
        config["single_session"] = True
    # Materialise the message cap onto session.config so the route's
    # 429 enforcement is decoupled from settings — the cookie-bound
    # cap stays stable for the visitor even if ops adjusts the channel
    # knob while the session is in flight.  A per-agent cap from the
    # routing config takes precedence over the deployment-global one.
    try:
        agent_cap = int(routing_config.get("session_message_cap") or 0)
    except (TypeError, ValueError):
        agent_cap = 0
    cap = agent_cap or settings.website.session_message_cap
    if cap:
        config["session_message_cap"] = cap

    # Per-buyer embed: a widget key a buyer minted carries the buyer's
    # end_user_id in the routing config. Pin it so every anonymous
    # visitor turn on the buyer's site draws from that buyer's purchased
    # allowance (the resell-on-your-site flow). Absent on an operator's
    # own agent key, which stays anonymous.
    embed_end_user_id = routing_config.get("embed_end_user_id")
    if embed_end_user_id:
        config["embed_end_user_id"] = str(embed_end_user_id)

    # Server-verified buyer identity, pinned at bootstrap so every
    # later message inherits it from the session row rather than
    # re-verifying a client-supplied token per message.
    if body is not None and body.firebase_id_token:
        buyer = await _resolve_commerce_buyer(
            request, agent_id, body.firebase_id_token,
        )
        if buyer is not None:
            config["commerce_buyer"] = buyer

    session = await store.create_session(
        session_id=session_id,
        user_id=None,
        org_id=org_uuid,
        agent_id=agent_id,
        channel=WEBSITE_CHANNEL,
        model=None,
        config=config,
    )
    try:
        await storage.create_bucket(bucket)
    except Exception:
        logger.exception(
            "Failed to provision agent bucket for session %s; rolling back",
            session_id,
        )
        try:
            await store.update_session_status(session_id, "failed")
        except Exception:
            logger.warning(
                "Rollback of session %s after bucket failure itself failed",
                session_id, exc_info=True,
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to provision session workspace; try again.",
        )

    return _issue_bootstrap_response(
        response,
        session_id=session.id,
        org_uuid=org_uuid,
        origin=normalized_origin,
        publishable_key=publishable_key,
        agent_id=agent_id,
    )


@router.post(
    "/website/sessions/{session_id}/messages",
    response_model=SendMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_website_message(
    session_id: UUID,
    body: SendMessageRequest,
    request: Request,
) -> SendMessageResponse:
    """Send a visitor message to the session, triggering agent processing.

    Double-submit CSRF: the ``X-CSRF-Token`` header must match the
    ``csrf`` claim baked into the cookie JWT.  An attacker who can
    forge a cross-site POST cannot read the HttpOnly cookie, so they
    cannot produce a matching header value.
    """
    claims = await _load_and_authorize_session(request, session_id)

    header_csrf = request.headers.get(CSRF_HEADER_NAME)
    if not verify_csrf_token(claims.csrf_token, header_csrf):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing or mismatched {CSRF_HEADER_NAME} header.",
        )

    store = get_session_store(request)
    try:
        session = await store.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    if session.org_id != claims.org_id:
        # Org drift between JWT and row is a hard invariant violation;
        # treat like session not found so we never leak cross-org state.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    await _enforce_website_rate_limit(
        request, str(session.org_id), session.agent_id,
    )
    if session.status not in ("active", "idle", "failed", "paused", "completed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is in '{session.status}' state and cannot accept messages.",
        )

    # Enforce the per-session message cap captured at bootstrap.  Read
    # from ``session.config`` rather than live settings so the cap a
    # visitor was admitted under stays stable for the whole session.
    cap = session.config.get("session_message_cap") if session.config else None
    if cap and session.message_count >= cap:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Session message cap reached; bootstrap a new session to continue."
            ),
        )

    await authorize_commerce_turn(request, session, body.content)

    # Per-buyer embed: when the widget key was minted by a buyer, this
    # anonymous visitor's turn draws from that buyer's purchased
    # allowance (``always`` because the buyer holds the allowance even
    # when the agent has no default cap). An exhausted allowance 402s
    # with the buy link the widget renders as a paywall.
    embed_end_user_id = (session.config or {}).get("embed_end_user_id")
    if embed_end_user_id:
        await authorize_allowance_turn(
            request,
            session,
            body.content,
            end_user_id=str(embed_end_user_id),
            always=True,
            channel="website",
        )

    if session.status in ("failed", "paused", "completed"):
        await store.update_session_status(session_id, "active")
        await store.emit_event(session_id, EventType.SESSION_RESUME, {})

    event_id = await store.emit_event(
        session_id,
        EventType.USER_MESSAGE,
        {"content": body.content},
    )
    await enqueue_session(
        request.app.state.redis,
        org_id=str(session.org_id),
        agent_id=session.agent_id,
        session_id=session_id,
    )
    return SendMessageResponse(event_id=event_id)


@router.get("/website/sessions/{session_id}/events")
async def stream_website_events(
    session_id: UUID,
    request: Request,
    after: int = 0,
) -> EventSourceResponse:
    """Stream session events via SSE to the visitor's browser.

    ``EventSource`` cannot set custom headers, so CSRF does not apply
    here (and the request doesn't mutate state).  Authentication is
    cookie-only; the decoded claims carry the session id, and the
    Origin header is re-validated against both the cookie's bound
    origin and the deployment's live allow-list.
    """
    claims = await _load_and_authorize_session(request, session_id)
    store = get_session_store(request)

    try:
        session_check = await asyncio.shield(store.get_session(session_id))
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found.",
        )
    if session_check.org_id != claims.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found.",
        )

    # Pre-loop fast-close for terminal sessions used to live here, but it
    # raced with concurrent POST /messages: a message landing between the
    # ``after`` check and the close meant the SESSION_RESUME pubsub publish
    # had no subscriber, and the new turn never reached the client.
    # ``event_generator`` now subscribes to pubsub first and applies a brief
    # grace window before declaring a terminal session done, so the single
    # path is correct for both fresh and historical opens.

    redis = getattr(request.app.state, "redis", None)

    async def event_generator():  # noqa: ANN202
        cursor = after
        elapsed = 0.0
        in_replay = True
        REPLAY_LIMIT = 5000
        LIVE_LIMIT = 50

        pubsub = None
        if redis is not None:
            try:
                pubsub = redis.pubsub()
                await pubsub.subscribe(f"surogates:session:{session_id}")
            except Exception:
                pubsub = None

        try:
            yield {"comment": "connected"}

            while elapsed < _MAX_STREAM_DURATION:
                if await request.is_disconnected():
                    return

                if in_replay:
                    events = await asyncio.shield(
                        store.get_events(
                            session_id,
                            after=cursor,
                            limit=REPLAY_LIMIT,
                            exclude_types=[EventType.LLM_DELTA],
                        )
                    )
                else:
                    events = await asyncio.shield(
                        store.get_events(session_id, after=cursor, limit=LIVE_LIMIT)
                    )

                for event in events:
                    yield {
                        "id": str(event.id),
                        "event": event.type,
                        "data": json.dumps(event.data, default=str),
                    }
                    if event.id is not None:
                        cursor = event.id

                if not events:
                    in_replay = False
                    try:
                        session = await asyncio.shield(
                            store.get_session(session_id)
                        )
                    except SessionNotFoundError:
                        yield {
                            "event": "session.done",
                            "data": json.dumps({"reason": "session_not_found"}),
                            "retry": 0,
                        }
                        return

                    if session.status in _TERMINAL_STATUSES:
                        # Race guard: a POST /messages currently in flight
                        # commits SESSION_RESUME and publishes on
                        # ``surogates:session:{id}``. We subscribed above so
                        # any publish issued after that point is queued. Wait
                        # briefly before declaring the session done, then
                        # re-check status — the POST may have flipped it
                        # back to active.
                        #
                        # Loop on get_message until a real publish or the
                        # deadline: redis-py queues the subscribe-confirm
                        # message and ``get_message(ignore_subscribe_messages=
                        # True)`` consumes it returning None, which without a
                        # loop would collapse our grace window to zero on the
                        # very first call after subscribe.
                        if pubsub is not None:
                            sse_loop = asyncio.get_event_loop()
                            deadline = sse_loop.time() + _POLL_INTERVAL
                            while sse_loop.time() < deadline:
                                remaining = max(0.0, deadline - sse_loop.time())
                                try:
                                    msg = await asyncio.wait_for(
                                        pubsub.get_message(
                                            ignore_subscribe_messages=True,
                                            timeout=remaining,
                                        ),
                                        timeout=remaining + 0.2,
                                    )
                                except (asyncio.TimeoutError, Exception):
                                    break
                                if msg is not None:
                                    break
                            try:
                                session = await asyncio.shield(
                                    store.get_session(session_id)
                                )
                            except SessionNotFoundError:
                                yield {
                                    "event": "session.done",
                                    "data": json.dumps(
                                        {"reason": "session_not_found"},
                                    ),
                                    "retry": 0,
                                }
                                return

                        if session.status in _TERMINAL_STATUSES:
                            yield {
                                "event": "session.done",
                                "data": json.dumps(
                                    {"reason": session.status, "status": session.status},
                                ),
                                "retry": 0,
                            }
                            return
                        # Status flipped to active during grace window -- loop
                        # back so the next iteration fetches the new events.
                        elapsed += _POLL_INTERVAL
                        continue

                    if pubsub is not None:
                        try:
                            await asyncio.wait_for(
                                pubsub.get_message(
                                    ignore_subscribe_messages=True,
                                    timeout=_POLL_INTERVAL,
                                ),
                                timeout=_POLL_INTERVAL + 0.5,
                            )
                        except (asyncio.TimeoutError, Exception):
                            pass
                    else:
                        await asyncio.sleep(_POLL_INTERVAL)

                    elapsed += _POLL_INTERVAL

            yield {
                "event": "stream.timeout",
                "data": json.dumps({"reason": "max_duration_exceeded"}),
            }
        except asyncio.CancelledError:
            return
        finally:
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe()
                    await pubsub.aclose()
                except Exception:
                    pass

    return EventSourceResponse(event_generator())


@router.post(
    "/website/sessions/{session_id}/end",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def end_website_session(
    session_id: UUID,
    request: Request,
    response: Response,
) -> None:
    """Explicit end-of-visit hook: marks the session completed and clears the cookie.

    Useful for single-page apps that want to release server resources
    when the visitor closes the chat.
    """
    claims = await _load_and_authorize_session(request, session_id)
    header_csrf = request.headers.get(CSRF_HEADER_NAME)
    if not verify_csrf_token(claims.csrf_token, header_csrf):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing or mismatched {CSRF_HEADER_NAME} header.",
        )

    store = get_session_store(request)
    await store.update_session_status(session_id, "completed")
    await store.emit_event(session_id, EventType.SESSION_COMPLETE, {})
    _clear_session_cookie(response)
