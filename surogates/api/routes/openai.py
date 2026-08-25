"""OpenAI-compatible chat completions over one agent.

Mounted so that an OpenAI client configured with::

    base_url = "https://<agent-host>/v1/api"
    api_key  = "surg_sk_…"

reaches ``POST /v1/api/chat/completions`` and ``GET /v1/api/models``.  That
prefix is not a coincidence: ``/v1/api/`` is already the only path where a
service-account token is accepted, so the endpoint lands inside the existing
auth boundary rather than opening a second one.  The agent itself comes from
the ``Host`` header, exactly as every other route on this host resolves it.

One request runs one real agent turn — the whole agent, with its skills,
tools, memory, workspace and browser.  What the client gets back is the turn's
final answer; the tool calls the agent made along the way are deliberately not
surfaced as OpenAI ``tool_calls``, because a client would try to answer them
and the request would hang forever.

Session mapping, history reconciliation and the wire translation live in
:mod:`surogates.channels.openai_conversation` and
:mod:`surogates.channels.openai_shape`; this module is the I/O around them.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
import uuid
from typing import Any, AsyncIterator
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.exc import IntegrityError

from surogates.api.routes._commerce_turn import (
    authorize_allowance_turn,
    runtime_commerce_payload,
)
from surogates.api.routes._shared import require_token_binds_agent
from surogates.channels.constants import API_CHANNEL
from surogates.channels.openai_conversation import (
    CONVERSATION_HEADER,
    ConversationScope,
    ReconcileAction,
    conversation_key,
    idempotency_key_for,
    reconcile,
    resolves_to_existing_session,
)
from surogates.channels.openai_shape import (
    DONE_SENTINEL,
    ImagePart,
    OpenAIRequestError,
    ParsedChatRequest,
    build_chat_response,
    build_chunk,
    build_error_body,
    build_final_chunk,
    build_models_response,
    build_role_chunk,
    build_usage_chunk,
    parse_chat_request,
    seed_text_for,
    sse_data,
    usage_from_cost_summary,
)
from surogates.config import enqueue_session
from surogates.runtime import (
    AgentRuntimeContext,
    agent_runtime_context_dep,
    rate_limit_dep,
)
from surogates.session.events import EventType
from surogates.session.provisioning import create_agent_session
from surogates.session.store import SessionStore
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()

#: Ceiling on a non-streaming completion.  There is no way to keep a blocking
#: HTTP request alive indefinitely through the proxies in front of this — an
#: agent turn that runs longer needs ``stream: true``, where the SSE frames
#: keep the connection demonstrably alive.  The error says so explicitly
#: rather than letting the caller hit an opaque gateway timeout.
NON_STREAMING_BUDGET_SECONDS = 300.0

#: Ceiling on a streamed completion.  Generous because the connection is being
#: fed; a turn still running past this is stuck, not slow.
STREAMING_BUDGET_SECONDS = 3600.0

#: How long to wait between polls of the event log while a turn runs.  The
#: Redis pub/sub nudge is the primary wake-up; this is the fallback that keeps
#: a missed notification from parking the request for its whole budget.
_POLL_INTERVAL = 0.4

#: Cadence for the SSE keepalive comment while the agent is silent.  Sized
#: under the ~100s idle cap a CDN or ingress typically applies.
_KEEPALIVE_INTERVAL = 15.0

#: Mirrors the message route's per-message image ceiling.
_MAX_IMAGES = 5
#: Raw bytes per image, matching the message route.
_MAX_IMAGE_BYTES = 20_000_000
#: How long to spend fetching one remote image before giving up.
_IMAGE_FETCH_TIMEOUT = 15.0

_TERMINAL_EVENTS = {
    EventType.SESSION_COMPLETE.value,
    EventType.SESSION_FAIL.value,
    EventType.SESSION_STOPPED.value,
    EventType.SESSION_PAUSE.value,
}

#: Maps a terminal session event onto an OpenAI ``finish_reason``.  ``stop`` is
#: the only one that means "the agent answered"; the rest are reported honestly
#: so a client can tell a truncated turn from a finished one.
_FINISH_REASONS = {
    EventType.SESSION_COMPLETE.value: "stop",
    EventType.SESSION_FAIL.value: "stop",
    EventType.SESSION_STOPPED.value: "stop",
    EventType.SESSION_PAUSE.value: "stop",
}


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def _error_response(error: OpenAIRequestError) -> JSONResponse:
    return JSONResponse(status_code=error.status, content=build_error_body(error))


def _upstream_error(message: str, *, code: str, status_code: int = 502) -> OpenAIRequestError:
    return OpenAIRequestError(
        message, type="api_error", code=code, status=status_code,
    )


# ---------------------------------------------------------------------------
# dependencies / helpers
# ---------------------------------------------------------------------------


def _store(request: Request) -> SessionStore:
    store: SessionStore | None = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store not available.",
        )
    return store


def _require_api_key_principal(tenant: TenantContext) -> UUID:
    """Only a bare service-account token may drive this endpoint.

    A worker-minted ``service_account_session`` JWT also carries a service
    account, but it is scoped to one session — accepting it here would let a
    leaked session token open new conversations, the same rule
    ``/v1/api/prompts`` enforces.
    """
    if tenant.service_account_id is None:
        # 403, not 401: the credential authenticated fine, it is simply the
        # wrong kind of principal.  Matches ``_require_service_account_api_route``
        # on the sibling ``/v1/api/*`` routes; a missing credential never gets
        # this far, the auth middleware 401s it first.
        raise OpenAIRequestError(
            "This endpoint requires a Surogate API key (prefix 'surg_sk_').",
            type="invalid_request_error",
            code="invalid_api_key",
            status=403,
        )
    if tenant.session_scope_id is not None:
        raise OpenAIRequestError(
            "Session-scoped tokens cannot start conversations.",
            type="invalid_request_error",
            code="invalid_api_key",
            status=403,
        )
    return tenant.service_account_id


async def _model_name(request: Request, agent: AgentRuntimeContext) -> str:
    """What the agent advertises itself as.

    The slug, when the control plane projects one: it is stable across renames
    and is already the agent's DNS name, so the value a caller puts in their
    config matches the host they point at.  Falls back to the agent id.
    """
    payload = await runtime_commerce_payload(request, agent.agent_id)
    slug = payload.get("slug")
    if isinstance(slug, str) and slug.strip():
        return slug.strip()
    return agent.agent_id


def _injection_detector():
    from surogates.api.routes.sessions import _get_injection_detector

    return _get_injection_detector()


def _screen(text: str, *, what: str) -> None:
    """Run caller-supplied text through the same screen every message gets.

    Seeded history bypasses the message route entirely — it is written
    straight into the event log — so without this an integration could hide a
    payload in a "prior turn" and have the agent carry it out with its full
    tool set on the next innocuous prompt.
    """
    if not text.strip():
        return
    result = _injection_detector().detect(text, source="api_channel")
    if result.is_injection:
        raise OpenAIRequestError(
            f"{what} blocked: {result.explanation}",
            code="content_blocked",
            status=422,
        )


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


async def _resolve_images(parsed: ParsedChatRequest) -> list[dict[str, str]]:
    """Turn parsed image parts into the message route's ``images`` payload.

    Remote URLs are fetched here because the event log stores inline data; the
    fetch is bounded in time and size, and a failure is reported to the caller
    rather than silently dropping the image — an agent answering "I can't see
    an image" when one was sent is worse than a 400.
    """
    if not parsed.images:
        return []
    if len(parsed.images) > _MAX_IMAGES:
        raise OpenAIRequestError(
            f"At most {_MAX_IMAGES} images per message (got {len(parsed.images)}).",
            param="messages",
        )

    resolved: list[dict[str, str]] = []
    remote = [p for p in parsed.images if p.url]
    fetched: dict[int, tuple[str, str]] = {}
    if remote:
        async with httpx.AsyncClient(
            timeout=_IMAGE_FETCH_TIMEOUT, follow_redirects=True,
        ) as client:
            results = await asyncio.gather(
                *(_fetch_image(client, part) for part in remote),
                return_exceptions=True,
            )
        for part, result in zip(remote, results):
            if isinstance(result, OpenAIRequestError):
                raise result
            if isinstance(result, BaseException):
                raise OpenAIRequestError(
                    f"Could not fetch image {part.url}: {type(result).__name__}",
                    param="messages",
                )
            fetched[id(part)] = result

    for part in parsed.images:
        if part.url:
            mime, data = fetched[id(part)]
            resolved.append({"data": data, "mime_type": mime})
            continue
        payload = part.data or ""
        try:
            decoded = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            raise OpenAIRequestError(
                "Invalid base64 image data.", param="messages",
            ) from None
        if len(decoded) > _MAX_IMAGE_BYTES:
            raise OpenAIRequestError(
                f"Image exceeds {_MAX_IMAGE_BYTES // 1_000_000}MB limit.",
                param="messages",
            )
        resolved.append({"data": payload, "mime_type": part.mime_type})
    return resolved


async def _fetch_image(
    client: httpx.AsyncClient, part: ImagePart,
) -> tuple[str, str]:
    """Fetch one remote image, returning ``(mime_type, base64)``."""
    from surogates.channels.openai_shape import _ALLOWED_IMAGE_MIMES

    try:
        response = await client.get(str(part.url))
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise OpenAIRequestError(
            f"Could not fetch image {part.url}: {exc}", param="messages",
        ) from exc

    mime = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if mime not in _ALLOWED_IMAGE_MIMES:
        raise OpenAIRequestError(
            f"Image at {part.url} is {mime or 'of unknown type'}; supported: "
            + ", ".join(sorted(_ALLOWED_IMAGE_MIMES)),
            param="messages",
        )
    body = response.content
    if len(body) > _MAX_IMAGE_BYTES:
        raise OpenAIRequestError(
            f"Image at {part.url} exceeds "
            f"{_MAX_IMAGE_BYTES // 1_000_000}MB limit.",
            param="messages",
        )
    return mime, base64.b64encode(body).decode("ascii")


# ---------------------------------------------------------------------------
# session resolution
# ---------------------------------------------------------------------------


class _ResolvedTurn:
    """The session this request runs in, and how it got there."""

    __slots__ = ("session", "action", "reason", "conversation_key")

    def __init__(self, session, action: ReconcileAction, reason: str | None, key: str):
        self.session = session
        self.action = action
        self.reason = reason
        self.conversation_key = key


async def _resolve_session(
    *,
    request: Request,
    store: SessionStore,
    tenant: TenantContext,
    agent: AgentRuntimeContext,
    parsed: ParsedChatRequest,
    service_account_id: UUID,
    explicit_id: str | None,
) -> _ResolvedTurn:
    """Find, continue, or fork the session this conversation belongs to."""
    scope = ConversationScope(
        service_account_id=str(service_account_id), end_user=parsed.user,
    )
    prior_user_turns = [t.content for t in parsed.prior_turns if t.role == "user"]
    key = conversation_key(
        prior_user_turns, scope=scope, explicit_id=explicit_id,
    )
    idem = idempotency_key_for(agent.agent_id, key)

    existing = None
    if resolves_to_existing_session(
        prior_turns=parsed.prior_turns, explicit_id=explicit_id,
    ):
        existing = await store.get_session_by_idempotency_key(tenant.org_id, idem)

    events = None
    if existing is not None:
        # A session found under this key but belonging to another agent would
        # be a control-plane bug rather than a client one; refuse loudly
        # instead of running the turn against the wrong agent.
        if existing.agent_id != agent.agent_id:
            raise _upstream_error(
                "Conversation belongs to a different agent.",
                code="conversation_agent_mismatch",
            )
        events = await store.get_events(
            existing.id, types=[EventType.USER_MESSAGE],
        )

    decision = reconcile(
        prior_turns=parsed.prior_turns,
        session_events=events,
        pinned=explicit_id is not None,
    )

    if decision.action is ReconcileAction.APPEND and existing is not None:
        return _ResolvedTurn(existing, decision.action, None, key)

    # CREATE and FORK both mint a session seeded with the caller's history.
    # A fork deliberately does NOT reuse the conversation key: the old session
    # keeps it until this turn completes and re-keys, so a concurrent request
    # replaying the same history still resolves deterministically.
    session = await _create_seeded_session(
        request=request,
        store=store,
        tenant=tenant,
        agent=agent,
        parsed=parsed,
        service_account_id=service_account_id,
        idempotency_key=None if decision.action is ReconcileAction.FORK else idem,
        seed_turns=decision.seed_turns,
    )
    return _ResolvedTurn(session, decision.action, decision.reason, key)


async def _create_seeded_session(
    *,
    request: Request,
    store: SessionStore,
    tenant: TenantContext,
    agent: AgentRuntimeContext,
    parsed: ParsedChatRequest,
    service_account_id: UUID,
    idempotency_key: str | None,
    seed_turns,
) -> Any:
    """Create the session and write the caller's history into it.

    Seeded turns are screened exactly as a live message would be, then written
    without enqueueing: they record what already happened, so the worker must
    not wake and answer the last one.
    """
    for turn in seed_turns:
        _screen(turn.content, what="Message")

    config: dict[str, Any] = {"service_account_id": str(service_account_id)}
    if parsed.user:
        config["openai_end_user"] = parsed.user

    try:
        session = await create_agent_session(
            store=store,
            storage=request.app.state.storage,
            settings=request.app.state.settings,
            user_id=None,
            org_id=tenant.org_id,
            agent_id=agent.agent_id,
            channel=API_CHANNEL,
            config=config,
            service_account_id=service_account_id,
            idempotency_key=idempotency_key,
        )
    except IntegrityError:
        # Two concurrent first requests on one conversation. The database is
        # the lock; the loser reads the winner's session and continues in it
        # rather than creating a duplicate.
        if idempotency_key is None:
            raise
        existing = await store.get_session_by_idempotency_key(
            tenant.org_id, idempotency_key,
        )
        if existing is None:
            raise
        return existing

    for turn in seed_turns:
        if turn.role == "user":
            await store.emit_event(
                session.id,
                EventType.USER_MESSAGE,
                {"content": seed_text_for(turn), "synthetic": "seed"},
            )
        else:
            await store.emit_event(
                session.id,
                EventType.LLM_RESPONSE,
                {
                    "message": {"role": "assistant", "content": turn.content},
                    "synthetic": "seed",
                },
            )
    return session


async def _rekey(store: SessionStore, session, *, agent_id: str, key: str) -> None:
    """Point the conversation key at this session for the caller's next turn.

    The key names the state a request is REPLYING to, so it advances by one
    turn each time.  Moving it here — after the turn is accepted — is what
    lets the next request find this session, and what makes a regenerate of
    the previous turn miss and fork instead of duplicating a turn.

    Best-effort: a collision means another session already claimed the key
    (a concurrent identical conversation), and the correct outcome is that
    this one simply stops being reachable by derivation rather than that the
    request fails after the agent has already been asked to work.
    """
    try:
        await store.set_session_idempotency_key(
            session.id, idempotency_key_for(agent_id, key),
        )
    except Exception:
        logger.info(
            "Could not advance the conversation key for session %s; the next "
            "turn will fork instead of continuing",
            session.id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# running a turn
# ---------------------------------------------------------------------------


async def _start_turn(
    *,
    request: Request,
    store: SessionStore,
    session,
    parsed: ParsedChatRequest,
    images: list[dict[str, str]],
) -> int:
    """Emit the user message and wake the worker.  Returns the event id."""
    if session.status in ("failed", "paused", "completed"):
        await store.update_session_status(session.id, "active")
        await store.emit_event(session.id, EventType.SESSION_RESUME, {})

    data: dict[str, Any] = {"content": parsed.prompt}
    if images:
        data["images"] = images
    event_id = await store.emit_event(session.id, EventType.USER_MESSAGE, data)

    await enqueue_session(
        request.app.state.redis,
        org_id=str(session.org_id),
        agent_id=session.agent_id,
        session_id=session.id,
    )
    return event_id


class _TurnReader:
    """Reads one turn's events out of the log, from a starting cursor.

    Shared by the buffered and streamed paths so they cannot disagree about
    what "this turn" means: everything after the user message that started it,
    up to and including the first terminal session event.
    """

    def __init__(self, store: SessionStore, session_id: UUID, after: int) -> None:
        self._store = store
        self._session_id = session_id
        self._cursor = after
        self.finished = False
        self.finish_reason = "stop"
        self.usage: dict[str, Any] = {}
        self.failure: str | None = None

    async def poll(self) -> list[Any]:
        """Return events since the last poll, marking the turn finished."""
        events = await self._store.get_events(self._session_id, after=self._cursor)
        if events:
            self._cursor = events[-1].id
        out = []
        for event in events:
            out.append(event)
            if event.type in _TERMINAL_EVENTS:
                self.finished = True
                self.finish_reason = _FINISH_REASONS.get(event.type, "stop")
                data = event.data or {}
                if event.type == EventType.SESSION_COMPLETE.value:
                    self.usage = usage_from_cost_summary(data.get("cost_summary"))
                elif event.type == EventType.SESSION_FAIL.value:
                    self.failure = str(data.get("error") or "the agent failed the turn")
                break
        return out


def _answer_text(events: list[Any]) -> tuple[str, str]:
    """The turn's final answer and its reasoning, from a list of events.

    An ``llm.response`` carrying ``tool_calls`` is the agent on its way to a
    tool, not the turn's conclusion, and a seeded one is history we wrote
    ourselves — both are skipped, or a failed turn would be reported as a
    successful answer to the wrong question.
    """
    content = ""
    for event in reversed(events):
        if event.type != EventType.LLM_RESPONSE.value:
            continue
        data = event.data or {}
        if data.get("synthetic"):
            continue
        message = data.get("message") or {}
        if message.get("tool_calls"):
            continue
        text = message.get("content")
        if isinstance(text, str) and text.strip():
            content = text
            break

    reasoning = "".join(
        str((e.data or {}).get("reasoning") or "")
        for e in events
        if e.type == EventType.LLM_DELTA.value
    )
    return content, reasoning


async def _wait_for_answer(
    reader: _TurnReader, *, redis, session_id: UUID, budget: float,
) -> list[Any]:
    """Collect one turn's events, waking on the pub/sub nudge.

    The poll is the fallback, not the mechanism: a missed notification costs
    ``_POLL_INTERVAL``, not the whole budget.
    """
    collected: list[Any] = []
    deadline = time.monotonic() + budget
    pubsub = None
    if redis is not None:
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(f"surogates:session:{session_id}")
        except Exception:
            pubsub = None
    try:
        while True:
            collected.extend(await reader.poll())
            if reader.finished:
                return collected
            if time.monotonic() >= deadline:
                raise _upstream_error(
                    "The agent did not finish this turn in "
                    f"{budget:.0f}s. Retry with \"stream\": true, which has a "
                    "far larger budget because the connection stays fed.",
                    code="agent_turn_timeout",
                    status_code=504,
                )
            if pubsub is not None:
                try:
                    await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=_POLL_INTERVAL,
                    )
                    continue
                except Exception:
                    pubsub = None
            await asyncio.sleep(_POLL_INTERVAL)
    finally:
        if pubsub is not None:
            try:
                await pubsub.aclose()
            except Exception:
                logger.debug("pubsub close failed", exc_info=True)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@router.get("/api/models")
async def list_models(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    agent: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> Any:
    """Advertise this agent as a single model.

    Clients call this to populate a model picker and to prove the endpoint is
    reachable before sending a completion.
    """
    try:
        _require_api_key_principal(tenant)
        require_token_binds_agent(tenant, agent.agent_id)
    except OpenAIRequestError as error:
        return _error_response(error)
    except HTTPException as exc:
        return _error_response(OpenAIRequestError(
            str(exc.detail), type="invalid_request_error",
            code="agent_mismatch", status=exc.status_code,
        ))

    return build_models_response(
        model=await _model_name(request, agent), created=int(time.time()),
    )


@router.post("/api/chat/completions")
async def chat_completions(
    request: Request,
    response: Response,
    tenant: TenantContext = Depends(get_current_tenant),
    agent: AgentRuntimeContext = Depends(agent_runtime_context_dep),
    _rate: None = Depends(rate_limit_dep),
) -> Any:
    """Answer one chat completion by running one real agent turn."""
    try:
        body = await request.json()
    except Exception:
        return _error_response(OpenAIRequestError("Request body must be JSON."))

    try:
        service_account_id = _require_api_key_principal(tenant)
        require_token_binds_agent(tenant, agent.agent_id)
        parsed = parse_chat_request(body)
        _screen(parsed.prompt, what="Message")
        images = await _resolve_images(parsed)
    except OpenAIRequestError as error:
        return _error_response(error)
    except HTTPException as exc:
        return _error_response(OpenAIRequestError(
            str(exc.detail), type="invalid_request_error",
            code="agent_mismatch", status=exc.status_code,
        ))

    store = _store(request)
    model = await _model_name(request, agent)
    explicit_id = request.headers.get(CONVERSATION_HEADER)

    try:
        resolved = await _resolve_session(
            request=request, store=store, tenant=tenant, agent=agent,
            parsed=parsed, service_account_id=service_account_id,
            explicit_id=explicit_id,
        )
    except OpenAIRequestError as error:
        return _error_response(error)

    session = resolved.session

    # Billed exactly like every other channel: the API key's owner is the
    # party whose allowance this turn draws from, the same shape the website
    # embed uses for anonymous visitors on a buyer's site. A no-op unless the
    # control plane projects a cap for this agent.
    try:
        await authorize_allowance_turn(
            request, session, parsed.prompt,
            end_user_id=str(service_account_id),
            channel=API_CHANNEL,
        )
    except HTTPException as exc:
        detail = exc.detail
        message = detail.get("code") if isinstance(detail, dict) else str(detail)
        if exc.status_code == status.HTTP_402_PAYMENT_REQUIRED:
            return _error_response(OpenAIRequestError(
                f"This agent's allowance is exhausted ({message}).",
                type="insufficient_quota", code=str(message), status=402,
            ))
        return _error_response(_upstream_error(
            "Access checks are temporarily unavailable; try again.",
            code="allowance_unavailable", status_code=exc.status_code,
        ))

    after = await _start_turn(
        request=request, store=store, session=session,
        parsed=parsed, images=images,
    )

    next_key = conversation_key(
        [t.content for t in parsed.prior_turns if t.role == "user"] + [parsed.prompt],
        scope=ConversationScope(
            service_account_id=str(service_account_id), end_user=parsed.user,
        ),
        explicit_id=explicit_id,
    )
    await _rekey(store, session, agent_id=agent.agent_id, key=next_key)

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    reader = _TurnReader(store, session.id, after)
    redis = getattr(request.app.state, "redis", None)

    headers = {
        "X-Surogate-Session": str(session.id),
        "X-Surogate-Conversation-Action": resolved.action.value,
    }
    if resolved.reason:
        headers["X-Surogate-Conversation-Fork-Reason"] = resolved.reason

    if parsed.stream:
        return StreamingResponse(
            _stream_turn(
                reader=reader, redis=redis, session_id=session.id,
                completion_id=completion_id, model=model, created=created,
                include_usage=parsed.include_usage,
            ),
            media_type="text/event-stream",
            headers={
                **headers,
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        events = await _wait_for_answer(
            reader, redis=redis, session_id=session.id,
            budget=NON_STREAMING_BUDGET_SECONDS,
        )
    except OpenAIRequestError as error:
        return _error_response(error)

    if reader.failure is not None:
        return _error_response(_upstream_error(
            f"The agent failed this turn: {reader.failure}",
            code="agent_turn_failed",
        ))

    content, reasoning = _answer_text(events)
    if not content:
        # Never a 200 with an empty string: a caller cannot tell that from a
        # deliberate empty answer, and would record it as the agent's reply.
        return _error_response(_upstream_error(
            "The agent completed this turn without producing an answer.",
            code="agent_empty_response",
        ))

    for key, value in headers.items():
        response.headers[key] = value

    return build_chat_response(
        completion_id=completion_id,
        model=model,
        content=content,
        created=created,
        usage=reader.usage,
        finish_reason=reader.finish_reason,
        reasoning=reasoning or None,
    )


async def _stream_turn(
    *,
    reader: _TurnReader,
    redis,
    session_id: UUID,
    completion_id: str,
    model: str,
    created: int,
    include_usage: bool,
) -> AsyncIterator[str]:
    """Translate the session's event stream into OpenAI chunks.

    Deliberately not layered over the SSE route: that stream closes after 300
    seconds and expects the client to reconnect, which an OpenAI client will
    never do.  Reading the log directly keeps one HTTP response open for the
    whole turn, however long it runs.

    Every exit path emits a terminal frame and ``[DONE]``.  A stream that
    stops without them reads to the client as a truncated response.
    """
    frame = {"completion_id": completion_id, "model": model, "created": created}
    yield sse_data(build_role_chunk(**frame))

    pubsub = None
    if redis is not None:
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(f"surogates:session:{session_id}")
        except Exception:
            pubsub = None

    deadline = time.monotonic() + STREAMING_BUDGET_SECONDS
    last_emit = time.monotonic()
    finish_reason = "stop"
    emitted_any = False
    collected: list[Any] = []

    try:
        while True:
            events = await reader.poll()
            collected.extend(events)
            for event in events:
                if event.type == EventType.LLM_DELTA.value:
                    data = event.data or {}
                    text = data.get("content")
                    if text:
                        emitted_any = True
                        last_emit = time.monotonic()
                        yield sse_data(build_chunk(**frame, content=str(text)))
                    thought = data.get("reasoning")
                    if thought:
                        last_emit = time.monotonic()
                        yield sse_data(build_chunk(**frame, reasoning=str(thought)))

            if reader.finished:
                finish_reason = reader.finish_reason
                break

            if time.monotonic() >= deadline:
                finish_reason = "length"
                break

            if pubsub is not None:
                try:
                    await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=_POLL_INTERVAL,
                    )
                except Exception:
                    pubsub = None
            else:
                await asyncio.sleep(_POLL_INTERVAL)

            # An agent can be silent for minutes inside one turn (a long tool
            # call, a browser step). Without this the proxy in front of us
            # drops an otherwise healthy connection.
            if time.monotonic() - last_emit >= _KEEPALIVE_INTERVAL:
                last_emit = time.monotonic()
                yield ": keepalive\n\n"

        if not emitted_any:
            # A turn whose visible text never arrived as deltas (a replayed
            # backlog drops them by design) still has to reach the client, so
            # fall back to the final response event.
            content, _ = _answer_text(collected)
            if content:
                yield sse_data(build_chunk(**frame, content=content))

        if reader.failure is not None:
            yield sse_data(build_chunk(
                **frame,
                content=f"\n\n[the agent failed this turn: {reader.failure}]",
            ))

        yield sse_data(build_final_chunk(**frame, finish_reason=finish_reason))
        if include_usage:
            yield sse_data(build_usage_chunk(**frame, usage=reader.usage))
    except asyncio.CancelledError:
        # The client hung up. The agent keeps working — a disconnect can be
        # transient, and the turn's result stays readable in the session.
        logger.info(
            "OpenAI stream cancelled by the client for session %s", session_id,
        )
        raise
    except Exception:
        logger.exception("OpenAI stream failed for session %s", session_id)
        yield sse_data(build_final_chunk(**frame, finish_reason="stop"))
    finally:
        if pubsub is not None:
            try:
                await pubsub.aclose()
            except Exception:
                logger.debug("pubsub close failed", exc_info=True)
        yield sse_data(DONE_SENTINEL)
