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
import ipaddress
import json
import socket
import logging
import time
import uuid
from typing import Any, AsyncIterator
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from surogates.api.routes._commerce_turn import runtime_commerce_payload
from surogates.channels.constants import API_CHANNEL
from surogates.channels.openai_conversation import (
    CONVERSATION_HEADER,
    ConversationScope,
    ReconcileAction,
    conversation_key,
    idempotency_key_for,
    normalise_explicit_id,
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

#: Maps a terminal session event onto an OpenAI ``finish_reason``.
#:
#: Only ``session.complete`` means "the agent answered". The others are real
#: outcomes a client must be able to tell apart from a finished turn, and the
#: OpenAI vocabulary has no word for them — ``content_filter`` is the closest
#: honest signal for a turn the platform stopped, and it is what clients
#: already render as "this did not finish normally".
_FINISH_REASONS = {
    EventType.SESSION_COMPLETE.value: "stop",
    EventType.SESSION_FAIL.value: "content_filter",
    EventType.SESSION_STOPPED.value: "content_filter",
    EventType.SESSION_PAUSE.value: "content_filter",
}

#: A paused session is the agent waiting on ``ask_user_question`` — it has a
#: question for the caller, not an answer. The API channel cannot render an
#: interactive prompt, so the question is surfaced as the turn's content
#: rather than reported as an empty completion.
_PAUSE_EVENT = EventType.SESSION_PAUSE.value


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



def _is_public_address(host: str) -> bool:
    """Whether *host* resolves only to public, routable addresses.

    A caller-supplied ``image_url`` is fetched by the server, from inside the
    cluster. Without this the endpoint is an SSRF primitive: a third party
    holding a customer API key could aim it at internal services, the
    Kubernetes API, or the cloud metadata endpoint, and read the outcome from
    the error it gets back.

    Fails closed — an unresolvable host is refused rather than attempted.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return False
    return True


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
        # No redirects: a 302 to 169.254.169.254 is how a front-door host
        # check gets bypassed.
        async with httpx.AsyncClient(
            timeout=_IMAGE_FETCH_TIMEOUT, follow_redirects=False,
        ) as client:
            results = await asyncio.gather(
                *(_fetch_image(client, part) for part in remote),
                return_exceptions=True,
            )
        for part, result in zip(remote, results):
            if isinstance(result, OpenAIRequestError):
                raise result
            if isinstance(result, BaseException):
                from surogates.channels.openai_shape import _ALLOWED_IMAGE_MIMES

                raise OpenAIRequestError(
                    _IMAGE_FETCH_REFUSED.format(
                        url=part.url,
                        kinds=", ".join(sorted(_ALLOWED_IMAGE_MIMES)),
                    ),
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


#: One message for every remote-fetch failure. Distinct messages (refused
#: host vs connect error vs wrong content-type vs too large) would let a
#: caller use this endpoint as a port and host scanner — the difference
#: between the replies IS the scan result.
_IMAGE_FETCH_REFUSED = (
    "Could not fetch the image at {url}. Remote images must be publicly "
    "reachable and one of: {kinds}."
)


async def _fetch_image(
    client: httpx.AsyncClient, part: ImagePart,
) -> tuple[str, str]:
    """Fetch one remote image, returning ``(mime_type, base64)``.

    The URL is caller-supplied and this runs inside the cluster, so the host
    is checked against public address space first and redirects are not
    followed — a redirect is the standard way to smuggle an internal target
    past a front-door check.
    """
    from surogates.channels.openai_shape import _ALLOWED_IMAGE_MIMES

    refused = OpenAIRequestError(
        _IMAGE_FETCH_REFUSED.format(
            url=part.url, kinds=", ".join(sorted(_ALLOWED_IMAGE_MIMES)),
        ),
        param="messages",
    )

    parsed = urlsplit(str(part.url))
    if not parsed.hostname or not await asyncio.to_thread(
        _is_public_address, parsed.hostname,
    ):
        raise refused

    try:
        response = await client.get(str(part.url))
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise refused from exc

    mime = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if mime not in _ALLOWED_IMAGE_MIMES:
        raise refused
    body = response.content
    if len(body) > _MAX_IMAGE_BYTES:
        raise refused
    return mime, base64.b64encode(body).decode("ascii")


# ---------------------------------------------------------------------------
# session resolution
# ---------------------------------------------------------------------------


class _ResolvedTurn:
    """The session this request runs in, and how it got there."""

    __slots__ = ("session", "action", "reason", "conversation_key", "displaces")

    def __init__(
        self, session, action: ReconcileAction, reason: str | None, key: str,
        displaces=None,
    ):
        self.session = session
        self.action = action
        self.reason = reason
        self.conversation_key = key
        #: The session that held this conversation's key before a fork. It
        #: must release the key before the new session can take it — a pinned
        #: conversation derives the same key on both sides of a fork.
        self.displaces = displaces


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
    # ``key_turns`` is the caller's own user text, before any system message
    # was folded into the first turn — a client that injects a live timestamp
    # would otherwise re-key on every request and never continue a session.
    key = conversation_key(
        parsed.key_turns[:-1], scope=scope, explicit_id=explicit_id,
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
        prompt=parsed.prompt,
    )

    if (
        decision.action in (ReconcileAction.APPEND, ReconcileAction.ATTACH)
        and existing is not None
    ):
        return _ResolvedTurn(existing, decision.action, None, key)

    # A new session is created WITHOUT a conversation key, always.
    #
    # The key names the state a request replies to, and an opening request
    # replies to nothing — so every opening request in a scope derives the
    # SAME key. Stamping it at creation made the second such request collide
    # on the unique index, and the collision handler then joined it to the
    # first request's session: unrelated conversations merged into one, and
    # the agent answered each of them with all the others in context.
    #
    # ``_rekey`` below assigns the real key once the turn has run, which is
    # also the point at which the key becomes unambiguous.
    session = await _create_seeded_session(
        request=request,
        store=store,
        tenant=tenant,
        agent=agent,
        parsed=parsed,
        service_account_id=service_account_id,
        seed_turns=decision.seed_turns,
    )
    return _ResolvedTurn(session, decision.action, decision.reason, key, existing)


async def _create_seeded_session(
    *,
    request: Request,
    store: SessionStore,
    tenant: TenantContext,
    agent: AgentRuntimeContext,
    parsed: ParsedChatRequest,
    service_account_id: UUID,
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
    )

    for index, turn in enumerate(seed_turns):
        if turn.role == "user":
            # ``prior_image_counts`` is aligned with the caller's history, and
            # the seeds ARE that history, so the index carries over.
            image_count = (
                parsed.prior_image_counts[index]
                if index < len(parsed.prior_image_counts)
                else 0
            )
            await store.emit_event(
                session.id,
                EventType.USER_MESSAGE,
                {
                    "content": seed_text_for(turn, image_count),
                    "synthetic": "seed",
                },
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


async def _rekey(
    store: SessionStore,
    session,
    *,
    agent_id: str,
    key: str,
    displaces=None,
) -> None:
    """Point the conversation key at this session for the caller's next turn.

    The key names the state a request is REPLYING to, so on a successful turn
    it advances by one; on a failed one it stays where the caller will look on
    a retry, so the retry ATTACHes to this session instead of asking the agent
    the same question again.

    *displaces* is the session that currently holds *key* and must give it up
    first — a pinned conversation (``X-Surogate-Conversation``) derives the
    SAME key before and after a fork, so without releasing the old holder the
    unique index refuses the move, the header keeps resolving to the stale
    session, and every following turn forks and re-seeds a fresh one.
    """
    target = idempotency_key_for(agent_id, key)
    try:
        if displaces is not None and displaces.id != session.id:
            await store.clear_session_idempotency_key(displaces.id)
        moved = await store.set_session_idempotency_key(session.id, target)
    except Exception:
        logger.warning(
            "Could not advance the conversation key for session %s; the next "
            "turn will fork instead of continuing",
            session.id,
            exc_info=True,
        )
        return
    if not moved:
        # Returned False, not raised: another session already holds this key.
        # Logged at warning because the visible symptom — a conversation that
        # silently restarts every turn — is otherwise unattributable.
        logger.warning(
            "Conversation key already held by another session; session %s "
            "will not be reachable by derivation and the next turn will fork",
            session.id,
        )


# ---------------------------------------------------------------------------
# running a turn
# ---------------------------------------------------------------------------



async def _cursor_before_last_user_message(
    store: SessionStore, session_id: UUID,
) -> int:
    """The event id just before the session's most recent user message.

    Used when attaching to a turn already in flight: reading from here
    replays that turn from its start, so a retry sees the same answer the
    first attempt is producing.
    """
    events = await store.get_events(session_id, types=[EventType.USER_MESSAGE])
    real = [e for e in events if not (e.data or {}).get("synthetic")]
    return (real[-1].id - 1) if real else 0


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
        self.paused = False
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
                self.paused = event.type == _PAUSE_EVENT
                if event.type == EventType.SESSION_COMPLETE.value:
                    self.usage = usage_from_cost_summary(data.get("cost_summary"))
                elif event.type == EventType.SESSION_FAIL.value:
                    self.failure = str(data.get("error") or "the agent failed the turn")
                break
        return out



def _pending_question(events: list[Any]) -> str:
    """The question the agent paused on, as plain text.

    ``ask_user_question`` reaches interactive channels as a rendered prompt.
    The API channel has none, so the question is returned as the turn's
    content and the caller answers it with their next message.
    """
    for event in reversed(events):
        if event.type != EventType.TOOL_CALL.value:
            continue
        data = event.data or {}
        if data.get("name") != "ask_user_question":
            continue
        arguments = data.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        if not isinstance(arguments, dict):
            continue
        question = str(arguments.get("question") or "").strip()
        if question:
            options = arguments.get("options")
            if isinstance(options, list) and options:
                rendered = "\n".join(f"- {o}" for o in options)
                return f"{question}\n\n{rendered}"
            return question
    return ""


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
    pubsub_healthy = True
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
            if pubsub is not None and pubsub_healthy:
                try:
                    await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=_POLL_INTERVAL,
                    )
                    continue
                except Exception:
                    # Fall back to polling, but keep the handle so ``finally``
                    # can still close it — rebinding it to None here leaked
                    # the connection for the process lifetime.
                    pubsub_healthy = False
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
    # Normalised here so "pinned" and the key are decided by one answer: a
    # blank or over-long header falls back to derived keying, and treating it
    # as pinned anyway would reconcile against a key it did not produce.
    explicit_id = normalise_explicit_id(request.headers.get(CONVERSATION_HEADER))

    try:
        resolved = await _resolve_session(
            request=request, store=store, tenant=tenant, agent=agent,
            parsed=parsed, service_account_id=service_account_id,
            explicit_id=explicit_id,
        )
    except OpenAIRequestError as error:
        return _error_response(error)

    session = resolved.session

    # No per-end-user allowance gate here, deliberately.
    #
    # That gate answers "has this END USER of the agent paid": the sessions
    # route applies it to ``web`` turns carrying a real ``user_id``, and the
    # website embed applies it on a buyer's behalf. An API key is neither. It
    # is the OPERATOR's own credential for their own agent — the same shape as
    # the ``studio`` channel, which the platform also leaves ungated.
    #
    # Billing still happens, by the same route as every operator's own usage:
    # the proxy debits the project wallet on each LLM call, and the worker
    # records the turn's cost. Gating on a per-user allowance instead blocked
    # every key on a monetized agent with ``402 allowance_exhausted`` — the
    # operator being asked to buy from themselves.
    #
    # Selling API access to a third party needs a buyer identity on the key,
    # which nothing mints yet; that is the follow-up, and it belongs on the
    # commerce path with the website embed, not here.

    if resolved.action is ReconcileAction.ATTACH:
        # A retry of a turn already running. Read from just before the message
        # the first attempt emitted, so this response carries the same answer
        # rather than asking the agent the same question twice.
        after = await _cursor_before_last_user_message(store, session.id)
    else:
        after = await _start_turn(
            request=request, store=store, session=session,
            parsed=parsed, images=images,
        )

    next_key = conversation_key(
        parsed.key_turns,
        scope=ConversationScope(
            service_account_id=str(service_account_id), end_user=parsed.user,
        ),
        explicit_id=explicit_id,
    )
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
                on_finished=lambda: _rekey(
                    store, session, agent_id=agent.agent_id, key=next_key,
                    displaces=resolved.displaces,
                ),
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

    # The key advances on every completed turn, failure included.
    #
    # An SDK retry of a 5xx would attach instead of re-running if the key
    # stayed where the retry looks — but only for a turn that HAS history: an
    # opening request is never resolved to an existing session (every one in a
    # scope derives the same key), so a first turn's retry cannot attach
    # whatever we do here. Keeping the key back for the turns where it would
    # help costs the conversation for all of them: the client's next request
    # carries one more turn, derives the advanced key, and finds nothing.
    #
    # Re-running a FAILED turn costs one more failed turn. Stranding the
    # conversation costs the session — its memory, workspace and browser — and
    # does so permanently. The turn already in flight is a different case and
    # keeps its key: see the timeout path, which deliberately does not rekey.
    await _rekey(
        store, session, agent_id=agent.agent_id, key=next_key,
        displaces=resolved.displaces,
    )

    if reader.failure is not None:
        return _error_response(_upstream_error(
            f"The agent failed this turn: {reader.failure}",
            code="agent_turn_failed",
        ))

    content, reasoning = _answer_text(events)
    if not content and reader.paused:
        # The agent is blocked on ask_user_question. This channel cannot
        # render an interactive prompt, so its question IS the turn's answer;
        # reporting "no answer" would hide a question the caller can act on.
        content = _pending_question(events)
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
    on_finished: Any = None,
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

    pubsub_healthy = True
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

            if pubsub is not None and pubsub_healthy:
                try:
                    await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=_POLL_INTERVAL,
                    )
                except Exception:
                    # Keep the handle for ``finally``; only stop using it.
                    pubsub_healthy = False
            else:
                await asyncio.sleep(_POLL_INTERVAL)

            # An agent can be silent for minutes inside one turn (a long tool
            # call, a browser step). Without this the proxy in front of us
            # drops an otherwise healthy connection.
            if time.monotonic() - last_emit >= _KEEPALIVE_INTERVAL:
                last_emit = time.monotonic()
                yield ": keepalive\n\n"

        if on_finished is not None:
            await on_finished()

        if not emitted_any:
            # A turn whose visible text never arrived as deltas (a replayed
            # backlog drops them by design) still has to reach the client, so
            # fall back to the final response event.
            content, _ = _answer_text(collected)
            if not content and reader.paused:
                # Same rule as the buffered path: a paused agent has a
                # QUESTION for the caller, not an answer. Without this the
                # identical turn returns the question with stream=false and an
                # empty message with stream=true.
                content = _pending_question(collected)
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
        # ``[DONE]`` is yielded on the normal path only. Yielding it from a
        # ``finally`` looks tidier but breaks on disconnect: Starlette closes
        # the generator with ``GeneratorExit`` (a BaseException, so it slips
        # past ``except Exception``), the ``finally`` yields into a closing
        # generator, and every dropped connection logs
        # "async generator ignored GeneratorExit".
        yield sse_data(DONE_SENTINEL)
    except asyncio.CancelledError:
        # The client hung up. The agent keeps working — a disconnect can be
        # transient, and the turn's result stays readable in the session.
        logger.info(
            "OpenAI stream cancelled by the client for session %s", session_id,
        )
        raise
    except Exception:
        logger.exception("OpenAI stream failed for session %s", session_id)
        yield sse_data(build_final_chunk(**frame, finish_reason="content_filter"))
        yield sse_data(DONE_SENTINEL)
    finally:
        if pubsub is not None:
            try:
                await pubsub.aclose()
            except Exception:
                logger.debug("pubsub close failed", exc_info=True)
