"""SSE event streaming and polling endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from surogates.api.session_guards import (
    require_device_access,
    require_session_visible,
    require_user_writable_session,
)
from surogates.channels.constants import session_queue_priority
from surogates.config import enqueue_session, load_settings
from surogates.harness.outcomes import build_defined_outcome_from_event
from surogates.session.events import EventType
from surogates.session.live import (
    DeltaGate,
    LiveFrame,
    Notification,
    decode_message,
    next_message,
    session_channel,
)
from surogates.session.models import Event
from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.workstreams import is_project_master, master_refusal
from surogates.workstreams.threads import reopen_if_thread

logger = logging.getLogger(__name__)

router = APIRouter()

# Terminal session statuses -- when the session enters one of these states the
# SSE stream sends a final event and closes.
# Only truly terminal statuses close the SSE stream. "failed" is excluded
# because users can retry by sending a new message (which resets to active).
_TERMINAL_STATUSES = frozenset({"completed", "archived"})
# What ends a watching stream (``watch=1``). A chat is ``completed`` between its
# turns, and a watcher waits through that for the next turn.
_WATCH_TERMINAL_STATUSES = frozenset({"archived"})

# Events after which a live stream reads all there is at once, to end itself
# without waiting for a quiet channel when the session's turn is over.
_TURN_END_TYPES = frozenset(t.value for t in (
    EventType.SESSION_COMPLETE, EventType.SESSION_FAIL, EventType.SESSION_PAUSE, EventType.SESSION_STOPPED,
))

# Maximum time (seconds) an SSE connection stays open before the server
# closes it gracefully.  Clients are expected to reconnect.
_MAX_STREAM_DURATION = 300

# Interval (seconds) between polls when no new events are found.
_POLL_INTERVAL = 0.5  # Fallback poll; Redis pub/sub is the primary notification

# Idle keepalive cadence. An SSE comment is sent at least this often when
# no events flow, so a long idle stretch (a coordinator waiting on
# executors) never trips a proxy's idle-connection timeout (~100s).
_KEEPALIVE_INTERVAL = 15.0


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class PollEventsResponse(BaseModel):
    events: list[Event]
    has_more: bool


class DefineOutcomeRubric(BaseModel):
    type: str
    content: str | None = None
    file_id: str | None = None


class SessionEventIn(BaseModel):
    type: str
    description: str | None = None
    rubric: DefineOutcomeRubric | None = None
    max_iterations: int | None = None


class SendEventsRequest(BaseModel):
    events: list[SessionEventIn]


class SentSessionEvent(BaseModel):
    type: str
    event_id: int
    outcome_id: str | None = None
    processed_at: str


class SendEventsResponse(BaseModel):
    events: list[SentSessionEvent]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_session_store(request: Request) -> SessionStore:
    store: SessionStore | None = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store not available.",
        )
    return store


def _last_event_id_cursor(header: str) -> int | None:
    """The cursor a ``Last-Event-ID`` header resumes from, or None.

    A persisted event's frame carries its id, ``N``.  A live ``llm.delta``
    frame is written to no log and carries ``-N``, N being the id of the
    persisted event sent before it: a stream that dropped on one resumes
    after event N, as it would have on that event's own frame.
    """
    digits = header[1:] if header.startswith("-") else header
    return int(digits) if digits.isdigit() else None


def _require_service_account_api_route(
    request: Request,
    tenant: TenantContext,
) -> None:
    """For ``/v1/api/*`` aliases, require a service-account principal."""
    if (
        request.url.path.startswith("/v1/api/")
        and tenant.service_account_id is None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires a service-account token.",
        )


async def _verify_session_access(
    request: Request, store: SessionStore, session_id: UUID, tenant: TenantContext
) -> None:
    """Raise 404 if the session does not exist or does not belong to the tenant.

    Session-scoped service-account JWTs are additionally restricted to
    the single session they were minted for — see
    :meth:`TenantContext.covers_session`.  Hidden multi-era web sessions
    ("multi session" capability off) 404 too.
    """
    try:
        session = await store.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    if not tenant.owns_session(session.org_id, session_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    await require_session_visible(request, session, tenant=tenant)


def _rubric_text_or_422(rubric: DefineOutcomeRubric | None) -> str:
    if (
        rubric is None
        or rubric.type != "text"
        or not (rubric.content or "").strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="user.define_outcome requires rubric {type: 'text', content: ...}.",
        )
    return rubric.content.strip()


# ---------------------------------------------------------------------------
# Event ingestion endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/api/sessions/{session_id}/events",
    response_model=SendEventsResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@router.post(
    "/sessions/{session_id}/events",
    response_model=SendEventsResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_session_events(
    session_id: UUID,
    body: SendEventsRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SendEventsResponse:
    """Send control-plane events to a session.

    This is intentionally narrow. The first accepted event is
    ``user.define_outcome``; arbitrary client event ingestion can be added
    later with separate validation and authorization.
    """
    _require_service_account_api_route(request, tenant)
    store = _get_session_store(request)
    await _verify_session_access(request, store, session_id, tenant)
    session = await store.get_session(session_id)
    require_user_writable_session(session, tenant)
    await require_device_access(request, session, tenant)

    sent: list[SentSessionEvent] = []
    for event in body.events:
        if event.type != EventType.USER_DEFINE_OUTCOME.value:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported event type: {event.type}",
            )
        # The web composer sends "/goal <text>" as this event, around the
        # harness's slash gate, so a master refuses it here.
        if is_project_master(session.config):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=master_refusal("goal"),
            )

        rubric_text = _rubric_text_or_422(event.rubric)
        try:
            state, processed_at = build_defined_outcome_from_event(
                description=event.description or "",
                rubric=rubric_text,
                max_iterations=event.max_iterations,
                settings=load_settings().outcomes,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

        # Out of Resolved before the goal is heard, as a typed message is.
        await reopen_if_thread(session, app_state=request.app.state)
        event_id = await store.emit_event(
            session_id,
            EventType.USER_DEFINE_OUTCOME,
            {
                "description": state.description,
                "rubric": {"type": "text", "content": state.rubric},
                "max_iterations": state.max_iterations,
                "outcome_id": state.id,
                "processed_at": processed_at,
            },
        )
        await store.update_session_config_key(
            session_id,
            "outcome",
            state.to_config(),
        )
        await store.emit_synthetic_user_message(
            session_id,
            content=state.description,
            synthetic="outcome_kickoff",
            metadata={"outcome_id": state.id},
        )

        if session.status in {"failed", "paused", "completed"}:
            await store.update_session_status(session_id, "active")
            await store.emit_event(session_id, EventType.SESSION_RESUME, {})

        redis = getattr(request.app.state, "redis", None)
        if redis is not None:
            await enqueue_session(
                redis,
                org_id=str(session.org_id),
                agent_id=session.agent_id,
                session_id=session_id,
                priority=session_queue_priority(session),
            )

        sent.append(
            SentSessionEvent(
                type=EventType.USER_DEFINE_OUTCOME.value,
                event_id=event_id,
                outcome_id=state.id,
                processed_at=processed_at,
            )
        )

    return SendEventsResponse(events=sent)


# ---------------------------------------------------------------------------
# SSE streaming endpoint
# ---------------------------------------------------------------------------


async def session_event_stream(
    request: Request,
    store: SessionStore,
    session_id: UUID,
    *,
    after: int,
    watch: bool,
) -> AsyncIterator[dict]:
    """The frames of *session_id*'s event stream, for ``EventSourceResponse``.

    It replays every event after the cursor *after* without ``llm.delta``,
    says ``stream.live`` once that backlog is sent, then sends each new event
    as it is written.  ``after=-1`` starts at the session's newest event.
    Without *watch*, a ``completed`` session ends the stream; with it, only
    ``archived``, a session gone, or the stream's time running out does.
    Redis comes from ``request.app.state``; without it, the stream polls.
    A ``Last-Event-ID`` header past *after* moves the cursor up to it.

    Live, a worker whose deltas go over Redis (``delta_transport: redis``)
    sends the ``llm.delta`` frames of the replies being written on the
    session's channel only (:mod:`surogates.session.live`), and this stream
    passes them on.  A stream without Redis has none and shows each reply
    whole at its ``llm.response``; a stream opened mid-reply has the part
    written after it opened.  Their ``id`` is ``-N``, N the id of the last
    event the stream sent before them: never a persisted event's id, and a
    reconnect with it resumes after event N.  The channel is read in order
    and the log only up to the event a notification names, so every frame
    of a model call goes out before the ``llm.response`` that ends it, and
    none after it.  Deltas a worker writes to the log are events like any
    other.

    ``session_event_stream`` does no access check: each caller checks first.
    """
    # Try to get Redis for pub/sub notifications (faster than polling).
    redis = getattr(request.app.state, "redis", None)
    # A browser's EventSource reconnects to the URL it first opened, whose
    # ``after`` is stale by then; the id it last received rides along as
    # ``Last-Event-ID``, and resuming there replays nothing twice.
    resume_at = _last_event_id_cursor(request.headers.get("last-event-id", ""))
    if resume_at is not None:
        after = max(after, resume_at)
    # A live frame's id, -N, given back as the cursor: after event N.  A
    # frame's id -1 (sent before the session's first event) given back as
    # ``after`` still means "from now"; ``Last-Event-ID: -1`` does not.
    if after < -1:
        after = -after
    # after=-1: from the session's newest event on, with nothing to replay.
    from_now = after < 0
    cursor = await asyncio.shield(store.last_event_id(session_id)) if from_now else after
    # Replay leaves out the llm.delta rows a worker writes to the log: per-token
    # chunks that existed only to animate live streaming; the corresponding
    # llm.response carries the final content.  For a long conversation they
    # can outnumber everything else 10:1, and pushing them through SSE
    # during replay stretches the "streaming in" feel across seconds.  We
    # filter at the SQL level (not just at yield time) so the rows never
    # leave the database.  Once we catch up (first empty fetch), we switch
    # to live mode.
    in_replay = not from_now
    terminal = _WATCH_TERMINAL_STATUSES if watch else _TERMINAL_STATUSES
    # Wide replay batch so catching up on a long conversation takes 1-2
    # DB round-trips instead of hundreds.  Live polling uses the narrow
    # batch since there's rarely more than a handful of pending events.
    REPLAY_LIMIT = 5000
    LIVE_LIMIT = 50
    # Which live frames are still news: none of a call whose end this
    # stream has already sent.
    gate = DeltaGate()

    # Subscribe to Redis channel for this session (if available).
    pubsub = None
    if redis is not None:
        try:
            pubsub = redis.pubsub()
            if watch:
                # By pattern, of the exact name (a UUID has no glob
                # characters): the agent counts a chat's exact subscribers
                # as its live viewers, and leaves its check-ins and
                # completions out of the inbox while one is there. A watch
                # is no viewer.
                await pubsub.psubscribe(session_channel(session_id))
                # redis-py sends the subscription without waiting for Redis
                # to take it, and a watch waits long on the channel after
                # its first read: a publish in between would be heard of
                # only at the keepalive. So the first read waits for
                # Redis's reply.
                await pubsub.get_message(timeout=_POLL_INTERVAL)
            else:
                await pubsub.subscribe(session_channel(session_id))
        except Exception:
            pubsub = None

    try:
        # Send an immediate comment to establish the SSE connection
        # (browsers show the request as "pending" until first byte).
        yield {"comment": "connected"}
        if watch:
            yield {"id": str(cursor), "event": "stream.start", "data": "{}"}
        if from_now:
            # Nothing to replay: everything on this stream happens now.
            yield {"event": "stream.live", "data": "{}"}
        # Last time any byte was sent to the client. A research/mission
        # coordinator goes idle for minutes between wakes (dispatch ->
        # wait for executors -> harvest); without a periodic keepalive
        # the proxy (Cloudflare / ingress, ~100s idle cap) silently
        # drops the connection and the live thread freezes until reload.
        loop = asyncio.get_event_loop()
        last_emit = loop.time()
        # Wall clock, not a count of waits: a busy turn sends many frames,
        # each ends a wait at once, and counting waits closed a live
        # stream after a few hundred tokens.
        deadline = last_emit + _MAX_STREAM_DURATION
        # Whether the log is read before the next wait, and up to which
        # event (None: all of it).  A live frame asks for no read: frames
        # cost the database nothing.
        must_read, read_until = True, None
        # Live frames heard while waiting out a terminal status, sent at the
        # next wait.
        held: list[LiveFrame] = []
        # The ``after`` of the last frame the stream read the log up to
        # before sending it (once per call).
        caught_up_to = -1
        last_disconnect_check = float("-inf")

        while loop.time() < deadline:
            # Check if client disconnected: before each read of the log, and
            # at most every poll interval while frames stream.
            now = loop.time()
            if must_read or now - last_disconnect_check >= _POLL_INTERVAL:
                last_disconnect_check = now
                if await request.is_disconnected():
                    return

            if must_read:
                # Shield DB calls from cancellation so an in-flight query
                # completes cleanly and the asyncpg connection returns to the
                # pool instead of being terminated mid-statement (which SQLA
                # logs as a noisy invalidation trace).
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
                        store.get_events(
                            session_id, after=cursor, until=read_until, limit=LIVE_LIMIT,
                        )
                    )

                ended = False
                for event in events:
                    yield {
                        "id": str(event.id),
                        "event": event.type,
                        "data": json.dumps(event.data, default=str),
                    }
                    if event.id is not None:
                        cursor = event.id
                        gate.saw(event.type, event.id, synthetic=bool((event.data or {}).get("synthetic")))
                    ended = ended or event.type in _TURN_END_TYPES
                if events:
                    last_emit = loop.time()
                if read_until is not None and not in_replay:
                    # A read up to a notification's event: what is after it
                    # waits for its own notification.  Only a full page means
                    # more up to the bound; a turn that ended is checked for
                    # the end of the stream at once, by a read of all there is.
                    if len(events) >= LIVE_LIMIT:
                        continue
                    must_read, read_until = ended, None
                    continue
                if events:
                    continue

                must_read, read_until = False, None
                if in_replay:
                    # The backlog is all sent: what follows happens now. The SDK opens a document only on a live edit.
                    yield {"event": "stream.live", "data": "{}"}
                in_replay = False
                # No new events -- check if the session has terminated.
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

                if session.status in terminal:
                    # Race guard: a POST /messages currently in flight
                    # commits SESSION_RESUME and publishes on
                    # ``surogates:session:{id}``. We subscribed above so
                    # any publish issued after that point is queued. Wait
                    # briefly before declaring the session done, then
                    # re-check status — the POST may have flipped it
                    # back to active.
                    #
                    # Wait for a real publish or the deadline: redis-py
                    # queues the subscribe-confirm message, which
                    # ``next_message`` skips, or the grace window would
                    # collapse to zero on the very first call after
                    # subscribe.  A live frame heard here is kept for the
                    # next wait.
                    if pubsub is not None:
                        try:
                            message = await next_message(pubsub, _POLL_INTERVAL)
                        except Exception:
                            message = None
                        if isinstance(message, LiveFrame):
                            held.append(message)
                        elif message is not None:
                            must_read = True
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

                    if session.status in terminal:
                        # A reader behind the channel may still have events to
                        # send: they go out before the end.
                        late = await asyncio.shield(
                            store.get_events(
                                session_id, after=cursor, limit=1,
                                # a finished session's old delta rows past a replay are not news
                                exclude_types=[EventType.LLM_DELTA],
                            )
                        )
                        if late:
                            must_read = True
                            continue
                        yield {
                            "event": "session.done",
                            "data": json.dumps(
                                {"reason": session.status, "status": session.status}
                            ),
                            "retry": 0,  # tell EventSource not to reconnect
                        }
                        return
                    # Status flipped to active during grace window -- loop
                    # back so the next iteration fetches the new events.
                    must_read = True
                    continue

            # Wait for the channel, or fall back to polling.
            if held:
                message = held.pop(0)
            elif pubsub is None:
                await asyncio.sleep(_POLL_INTERVAL)
                message = None
            else:
                now = loop.time()
                # A watch sits through a chat's idle hours: it reads the
                # chat again only when a publish wakes it (every
                # emit_event publishes after its commit), or at the
                # keepalive, or as the stream ends.  Any other stream
                # reads the log every poll interval when the channel is
                # quiet, as the fallback for a notification lost.
                wait = (
                    min(last_emit + _KEEPALIVE_INTERVAL - now, deadline - now)
                    if watch else _POLL_INTERVAL
                )
                try:
                    message = await next_message(pubsub, max(wait, 0.0))
                except Exception:
                    # A subscription that fails waits as a stream without
                    # Redis does, a poll interval, then reads on.
                    await asyncio.sleep(_POLL_INTERVAL)
                    message = None

            if isinstance(message, Notification):
                # Several notifications waiting in a row make one read, up to
                # the last of them; a live frame among them waits for it.
                upto = message.event_id
                while pubsub is not None and not held:
                    try:
                        queued = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0)
                    except Exception:
                        break
                    if queued is None:
                        break
                    following = decode_message(queued)
                    if isinstance(following, Notification):
                        upto = max(upto, following.event_id)
                    elif isinstance(following, LiveFrame):
                        held.append(following)
                    else:
                        upto = None
                        break
                if upto is None:
                    must_read, read_until = True, None
                elif upto > cursor:
                    # Read the log up to the event the notification names: what
                    # is written after it waits for its own notification, so a
                    # call's live frames, published before it, go out first.
                    must_read = True
                    read_until = max(read_until or 0, upto)

            elif isinstance(message, LiveFrame):
                if message.after > cursor and message.after != caught_up_to:
                    # The events before the frame's call (a notification lost,
                    # or not read yet) go out before it.
                    caught_up_to = message.after
                    held.insert(0, message)
                    must_read = True
                    read_until = max(read_until or 0, message.after)
                elif gate.admits(message):
                    yield {"id": f"-{cursor}", "event": "llm.delta", "data": message.data_json}
                    last_emit = loop.time()
            else:
                # Quiet channel (poll fallback), no channel, or a message of
                # another shape: read everything there is.
                must_read, read_until = True, None

            # Keep the connection warm through proxy idle timeouts.
            now = loop.time()
            if now - last_emit >= _KEEPALIVE_INTERVAL:
                yield {"comment": "keepalive"}
                last_emit = now

        # Stream duration exceeded -- close gracefully.
        yield {
            "event": "stream.timeout",
            "data": json.dumps({"reason": "max_duration_exceeded"}),
        }
    except asyncio.CancelledError:
        # Client disconnected — exit cleanly without letting the
        # cancellation propagate through SQLAlchemy's connection pool.
        return
    finally:
        if pubsub is not None:
            try:
                # As it subscribed: a watch by pattern, any other stream by name.
                if watch:
                    await pubsub.punsubscribe()
                else:
                    await pubsub.unsubscribe()
                await pubsub.aclose()
            except Exception:
                pass


@router.get("/api/sessions/{session_id}/events")
@router.get("/sessions/{session_id}/events")
async def stream_events(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    after: int = 0,
    watch: bool = False,
) -> EventSourceResponse:
    """Stream session events via Server-Sent Events.

    The client provides ``after`` (the last event ID it received) and the
    server yields all subsequent events as they appear.  ``after=-1`` starts
    at the session's newest event, with none of its past; any other negative
    ``after`` is a live frame's id, ``-N``, and resumes after event N.  When the session
    reaches a terminal status, a ``session.done`` event is emitted and the
    stream closes.

    ``watch=1`` follows the session across its turns, as Surogate Desktop
    follows the chat its window shows: only ``archived``, or a session gone,
    ends it, and ``completed`` between two turns does not. Its first event,
    ``stream.start``, carries the cursor it starts from as its id, so that a
    watcher that reconnects, at ``stream.timeout`` or after a drop, takes up
    there with ``after=<that id>``. A watch waits on the session's channel, and
    reads the session only when a publish wakes it or at the keepalive. With no
    channel, as without Redis or when its subscription fails, it falls back to
    the poll every stream has, a read each ``_POLL_INTERVAL``.
    """
    _require_service_account_api_route(request, tenant)
    store = _get_session_store(request)

    # Single DB call for access check + terminal status check.  Shielded so
    # a fast client reconnect (which cancels this request) doesn't terminate
    # the asyncpg connection mid-query.
    try:
        session_check = await asyncio.shield(store.get_session(session_id))
    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    if not tenant.owns_session(session_check.org_id, session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    await require_session_visible(request, session_check, tenant=tenant)

    # Pre-loop fast-close for terminal sessions used to live here, but it
    # raced with concurrent POST /messages: a message landing between the
    # ``after`` check and the close meant the SESSION_RESUME pubsub publish
    # had no subscriber, and the new turn never reached the client.
    # ``session_event_stream`` now subscribes to pubsub first and applies a brief
    # grace window before declaring a terminal session done, so the single
    # path is correct for both fresh and historical opens.

    return EventSourceResponse(session_event_stream(request, store, session_id, after=after, watch=watch))


# ---------------------------------------------------------------------------
# One-shot polling endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/sessions/{session_id}/events/poll",
    response_model=PollEventsResponse,
)
async def poll_events(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    after: int = 0,
    limit: int = 50,
) -> PollEventsResponse:
    """Fetch events in a single request (non-streaming alternative to SSE)."""
    store = _get_session_store(request)
    await _verify_session_access(request, store, session_id, tenant)

    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    # Fetch one extra to determine ``has_more``.
    events = await store.get_events(session_id, after=after, limit=limit + 1)

    has_more = len(events) > limit
    if has_more:
        events = events[:limit]

    return PollEventsResponse(events=events, has_more=has_more)
