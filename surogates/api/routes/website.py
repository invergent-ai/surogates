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

All authority comes from :class:`WebsiteSettings`.  There is no
per-row agent record any more: a deployment serves exactly one agent,
identified by ``settings.agent_id``, and exposes it through this
channel when ``website.enabled`` is true.  Origin validation is the
conjunction of two checks: the configured allow-list (authoritative)
and the session cookie's ``origin`` claim (anchors a bootstrapped
session to the embed it came from).  A request must satisfy both.
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
    verify_publishable_key,
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
from surogates.session.events import EventType
from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.storage.tenant import agent_session_bucket
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


def _get_session_store(request: Request) -> SessionStore:
    store: SessionStore | None = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store not available.",
        )
    return store


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


def _verify_publishable_key_from_request(request: Request, settings: Settings) -> None:
    """Reject any request that does not present the configured key.

    Returns silently on success.  Three failure modes a legitimate
    embed has already pre-empted by reading its config correctly:
    HTTP 400 for a missing/malformed Authorization header, HTTP 401
    for the wrong token shape (e.g. a service-account key) or a
    mismatched value, HTTP 503 when the deployment is enabled but no
    key is configured (a misconfiguration the operator must fix).
    """
    expected = settings.website.publishable_key
    if not expected:
        # Deployment said website.enabled=true but didn't ship a key.
        # Surfaces this as 503 so the operator notices, rather than
        # silently letting any bearer through.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Website channel is enabled but no publishable key is configured.",
        )

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

    if not verify_publishable_key(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid publishable key.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _resolve_claims_from_cookie(
    request: Request,
) -> WebsiteSessionClaims:
    """Decode the session cookie from *request* or raise 401.

    The decoded claims are the authority for session ownership on
    messages/events — the cookie carries the session id, org, origin,
    and CSRF token.  A missing cookie is an unauthenticated request
    (expired or never bootstrapped); a malformed cookie is an expired
    or forged JWT; either way, 401.
    """
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing website session cookie; call POST /v1/website/sessions first.",
        )
    try:
        return decode_website_session_token(raw)
    except InvalidTokenError as exc:
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


async def _load_and_authorize_session(
    request: Request,
    path_session_id: UUID,
) -> WebsiteSessionClaims:
    """Resolve cookie, verify session id, enforce origin binding.

    Used by every cookie-authenticated route.  The path session id
    must match the claim so a visitor of one session cannot target
    another visitor's session by swapping the URL — the session JWT
    scopes to exactly one session.
    """
    settings = _get_settings(request)
    _require_website_enabled(settings)

    claims = await _resolve_claims_from_cookie(request)
    if claims.session_id != path_session_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {path_session_id} not found.",
        )

    request_origin = _extract_origin(request)
    allowed = parse_allowed_origins(settings.website.allowed_origins)
    _enforce_origin_binding(claims, request_origin, allowed)
    return claims


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/website/sessions",
    response_model=BootstrapResponse,
    status_code=status.HTTP_201_CREATED,
)
async def bootstrap_website_session(
    request: Request,
    response: Response,
) -> BootstrapResponse:
    """Exchange a publishable key + approved origin for a session cookie.

    Creates a fresh session owned by the deployment's org (no user
    row), mints the HttpOnly cookie the browser presents on
    subsequent requests, and returns the CSRF token the browser
    client echoes in ``X-CSRF-Token``.
    """
    settings = _get_settings(request)
    _require_website_enabled(settings)
    _verify_publishable_key_from_request(request, settings)

    request_origin = _extract_origin(request)
    allowed = parse_allowed_origins(settings.website.allowed_origins)
    if not origin_allowed(request_origin, allowed):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request origin is not in the configured allow-list.",
        )

    if not settings.org_id:
        # Visitor sessions need a real org to attach to (memory,
        # storage, governance are all org-scoped).  A misconfigured
        # deployment that hasn't set org_id cannot honour the channel
        # contract; surface it explicitly rather than crashing the
        # SQLAlchemy insert with a NULL constraint failure.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Website channel is enabled but the deployment org is not configured.",
        )

    try:
        org_uuid = UUID(settings.org_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Configured org_id is not a valid UUID.",
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

    store = _get_session_store(request)
    storage = request.app.state.storage

    session_id = uuid.uuid4()
    bucket = agent_session_bucket(settings.storage.bucket)
    normalized_origin = normalize_origin(request_origin)

    config: dict = {
        "storage_bucket": bucket,
        "workspace_path": storage.resolve_workspace_path(bucket, session_id),
        "website_origin": normalized_origin,
    }
    # Materialise the message cap onto session.config so the route's
    # 429 enforcement is decoupled from settings — the cookie-bound
    # cap stays stable for the visitor even if ops adjusts the channel
    # knob while the session is in flight.
    if settings.website.session_message_cap:
        config["session_message_cap"] = settings.website.session_message_cap

    session = await store.create_session(
        session_id=session_id,
        user_id=None,
        org_id=org_uuid,
        agent_id=settings.agent_id,
        channel=WEBSITE_CHANNEL,
        model=settings.llm.model,
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

    csrf_token = generate_csrf_token()
    cookie_token = create_website_session_token(
        session_id=session.id,
        org_id=org_uuid,
        origin=normalized_origin,
        csrf_token=csrf_token,
    )
    _set_session_cookie(
        response, token=cookie_token, expires_seconds=DEFAULT_SESSION_TTL_SECONDS,
    )

    return BootstrapResponse(
        session_id=session.id,
        csrf_token=csrf_token,
        expires_at=int(time.time()) + DEFAULT_SESSION_TTL_SECONDS,
        agent_name=settings.agent_id,
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

    store = _get_session_store(request)
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
    if session.status not in ("active", "idle", "failed", "paused"):
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

    if session.status in ("failed", "paused"):
        await store.update_session_status(session_id, "active")
        await store.emit_event(session_id, EventType.SESSION_RESUME, {})

    event_id = await store.emit_event(
        session_id,
        EventType.USER_MESSAGE,
        {"content": body.content},
    )
    await enqueue_session(
        request.app.state.redis, session.agent_id, session_id,
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
    store = _get_session_store(request)

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

    if session_check.status in _TERMINAL_STATUSES:
        remaining = await asyncio.shield(
            store.get_events(session_id, after=after, limit=1)
        )
        if not remaining:
            async def _terminal_generator():  # noqa: ANN202
                yield {
                    "event": "session.done",
                    "data": json.dumps(
                        {"reason": session_check.status,
                         "status": session_check.status},
                    ),
                    "retry": 0,
                }
            return EventSourceResponse(_terminal_generator())

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
                        yield {
                            "event": "session.done",
                            "data": json.dumps(
                                {"reason": session.status, "status": session.status},
                            ),
                            "retry": 0,
                        }
                        return

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

    Optional — sessions also auto-reset by the idle-reset job.  Useful
    for single-page apps that want to release server resources when
    the visitor closes the chat.
    """
    claims = await _load_and_authorize_session(request, session_id)
    header_csrf = request.headers.get(CSRF_HEADER_NAME)
    if not verify_csrf_token(claims.csrf_token, header_csrf):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing or mismatched {CSRF_HEADER_NAME} header.",
        )

    store = _get_session_store(request)
    await store.update_session_status(session_id, "completed")
    await store.emit_event(session_id, EventType.SESSION_COMPLETE, {})
    _clear_session_cookie(response)
