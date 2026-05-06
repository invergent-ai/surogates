"""PostgreSQL-backed session store — the system's durable core.

Every mutation to sessions, events, leases, and cursors goes through this
module.  Uses SQLAlchemy ORM for CRUD operations and raw SQL only where
atomic upserts or conditional updates are required (leases, cursors,
counter increments).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)
from uuid import UUID

from sqlalchemy import and_, not_, select, text, update, delete, func, or_

from surogates.db.models import (
    Event as EventRow,
    Session as SessionRow,
    SessionCursor,
    SessionLease as LeaseRow,
)
from surogates.session.events import EventType
from surogates.session.models import Event, Session, SessionLease

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class SessionNotFoundError(Exception):
    """Raised when a session lookup finds no matching row."""


class LeaseNotHeldError(Exception):
    """Raised when a lease operation fails because the caller does not hold it."""


# Events that should be delivered to messaging channels (Slack, Teams, etc.).
# Web channel reads from the events table directly via SSE.
_DELIVERABLE_EVENTS = frozenset({
    EventType.LLM_RESPONSE,
})


class SessionStore:
    """Async, PostgreSQL-backed store for sessions, events, leases, and cursors.

    Uses SQLAlchemy ORM models from ``surogates.db.models``.  All public
    methods acquire their own connection from the pool.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Any | None = None,
    ) -> None:
        self._sf = session_factory
        self._redis = redis
        # Session channel cache: session_id → (channel, config).
        # Populated lazily when a deliverable event is emitted.
        self._channel_cache: dict[UUID, tuple[str, dict]] = {}

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    async def create_session(
        self,
        *,
        session_id: UUID | None = None,
        user_id: UUID | None,
        org_id: UUID,
        agent_id: str,
        channel: str = "web",
        model: str | None = None,
        config: dict | None = None,
        parent_id: UUID | None = None,
        service_account_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> Session:
        """Create a new session and return its Pydantic representation.

        If *session_id* is provided it is used as the primary key;
        otherwise a random UUID is generated.

        *agent_id* identifies the agent this session belongs to (the agent
        is the server-side identity this worker instance serves, sourced
        from ``Settings.agent_id``).

        Exactly one of *user_id* or *service_account_id* should be set —
        the first for interactive users, the second for service-account
        (``channel="api"``) sessions.  Callers are responsible for
        maintaining that invariant; the store does not enforce it.

        *idempotency_key* is optional; when supplied, a unique-constraint
        violation on ``(org_id, idempotency_key)`` lets callers detect
        retries of the same logical request.
        """
        row = SessionRow(
            id=session_id or uuid.uuid4(),
            user_id=user_id,
            org_id=org_id,
            agent_id=agent_id,
            channel=channel,
            status="active",
            model=model,
            config=config or {},
            parent_id=parent_id,
            service_account_id=service_account_id,
            idempotency_key=idempotency_key,
        )
        async with self._sf() as db:
            db.add(row)
            # Initialise the cursor row so get_harness_cursor never 404s.
            db.add(SessionCursor(session_id=row.id, harness_cursor=0))
            await db.commit()
            await db.refresh(row)
        return Session.model_validate(row)

    async def get_session_by_idempotency_key(
        self,
        org_id: UUID,
        idempotency_key: str,
    ) -> Session | None:
        """Return the existing session for *(org_id, idempotency_key)*, if any.

        Used by the fire-and-forget prompt API to short-circuit retries
        without creating duplicate sessions.
        """
        async with self._sf() as db:
            result = await db.execute(
                select(SessionRow).where(
                    SessionRow.org_id == org_id,
                    SessionRow.idempotency_key == idempotency_key,
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return Session.model_validate(row)

    async def get_session(self, session_id: UUID) -> Session:
        """Fetch a single session by ID.  Raises ``SessionNotFoundError``."""
        async with self._sf() as db:
            result = await db.execute(
                select(SessionRow).where(SessionRow.id == session_id)
            )
            row = result.scalar_one_or_none()
        if row is None:
            raise SessionNotFoundError(f"session {session_id} not found")
        return Session.model_validate(row)

    async def update_session_status(self, session_id: UUID, status: str) -> None:
        """Set a session's status and touch ``updated_at``."""
        async with self._sf() as db:
            result = await db.execute(
                update(SessionRow)
                .where(SessionRow.id == session_id)
                .values(status=status, updated_at=func.now())
            )
            if result.rowcount == 0:
                raise SessionNotFoundError(f"session {session_id} not found")
            await db.commit()

    async def list_sessions(
        self,
        org_id: UUID,
        user_id: UUID | None,
        agent_id: str,
        *,
        service_account_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Session]:
        """Return top-level sessions for a principal within an org, newest first.

        Delegation children (``parent_id IS NOT NULL``) are excluded --
        they belong under their parent in the session tree, not as
        siblings in the main sidebar list.  The tree endpoint
        (:func:`get_session_tree`) surfaces them when the parent is
        opened.
        """
        if (user_id is None) == (service_account_id is None):
            raise ValueError("list_sessions requires exactly one principal id")
        principal_filter = (
            SessionRow.service_account_id == service_account_id
            if service_account_id is not None
            else SessionRow.user_id == user_id
        )
        async with self._sf() as db:
            result = await db.execute(
                select(SessionRow)
                .where(
                    SessionRow.org_id == org_id,
                    principal_filter,
                    SessionRow.agent_id == agent_id,
                    SessionRow.status != "archived",
                    SessionRow.parent_id.is_(None),
                )
                .order_by(SessionRow.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = result.scalars().all()
        return [Session.model_validate(r) for r in rows]

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------

    async def emit_event(
        self,
        session_id: UUID,
        event_type: EventType,
        data: dict,
    ) -> int:
        """Append an event and atomically update session counters.

        Returns the newly assigned event id (``BIGSERIAL``).

        Trace context is read automatically from the :mod:`surogates.trace`
        contextvar.  If no trace is active the ``trace_id`` / ``span_id``
        columns are left ``NULL``.

        Counter updates use raw SQL for atomic increment expressions
        (``message_count = message_count + 1``) which can't be expressed
        cleanly in ORM ``update().values()``.
        """
        from surogates.trace import get_trace

        trace = get_trace()
        row = EventRow(
            session_id=session_id,
            type=event_type.value,
            data=data,
            trace_id=trace.trace_id if trace else None,
            span_id=trace.span_id if trace else None,
        )
        counter_clause = _build_counter_update_clause(event_type, data)

        async with self._sf() as db:
            db.add(row)
            await db.flush()  # assigns row.id via BIGSERIAL
            event_id: int = row.id

            # Atomic counter update (raw SQL — ORM can't do col = col + 1).
            await db.execute(
                text(f"UPDATE sessions SET {counter_clause} WHERE id = :id"),  # noqa: S608
                {"id": session_id},
            )
            await db.commit()

        # Notify SSE subscribers via Redis pub/sub (best-effort).
        if self._redis is not None:
            try:
                await self._redis.publish(
                    f"surogates:session:{session_id}",
                    f"{event_id}:{event_type.value}",
                )
            except Exception:
                pass

        # Channel delivery: enqueue deliverable events to the outbox
        # for non-web channels (Slack, Teams, Telegram, etc.).
        if event_type in _DELIVERABLE_EVENTS:
            await self._enqueue_channel_delivery(session_id, event_id, event_type, data)

        return event_id

    async def _enqueue_channel_delivery(
        self,
        session_id: UUID,
        event_id: int,
        event_type: EventType,
        data: dict,
    ) -> None:
        """Enqueue a deliverable event to the outbox for non-web channels.

        Looks up the session's channel and config (cached after first lookup).
        Web sessions are skipped — they use SSE polling, not the outbox.
        """
        try:
            # Look up session channel (cached).
            if session_id not in self._channel_cache:
                async with self._sf() as db:
                    row = await db.get(SessionRow, session_id)
                    if row:
                        self._channel_cache[session_id] = (
                            row.channel or "web",
                            row.config or {},
                        )
                    else:
                        return

            channel, config = self._channel_cache[session_id]

            # Web sessions use SSE — no outbox delivery needed.
            if channel == "web":
                return

            # Build channel-specific destination from session config.
            destination: dict[str, Any] = {}
            if channel == "slack":
                destination = {
                    "channel_id": config.get("slack_channel_id", ""),
                    "thread_ts": config.get("slack_thread_ts"),
                    "team_id": config.get("slack_team_id", ""),
                }
            elif channel == "teams":
                destination = {
                    "conversation_id": config.get("teams_conversation_id", ""),
                    "activity_id": config.get("teams_activity_id"),
                }
            elif channel == "telegram":
                destination = {
                    "chat_id": config.get("telegram_chat_id", ""),
                    "reply_to_message_id": config.get("telegram_reply_to"),
                }
            else:
                destination = {"session_id": str(session_id)}

            # Build payload: extract the user-facing content from the event.
            payload: dict[str, Any] = {}
            if event_type == EventType.LLM_RESPONSE:
                msg = data.get("message", {})
                content = msg.get("content", "") if isinstance(msg, dict) else ""
                if content:
                    payload["content"] = content

            if not payload.get("content"):
                return  # Nothing to deliver (e.g., tool-call-only LLM_RESPONSE).

            # Enqueue to the delivery outbox.
            from surogates.db.models import DeliveryOutbox
            dedupe_key = f"{channel}:{event_id}"

            async with self._sf() as db:
                outbox = DeliveryOutbox(
                    session_id=session_id,
                    event_id=event_id,
                    channel=channel,
                    destination=destination,
                    payload=payload,
                    dedupe_key=dedupe_key,
                    status="pending",
                )
                db.add(outbox)
                try:
                    await db.commit()
                except Exception:
                    # Dedupe constraint violation — already enqueued.
                    await db.rollback()

        except Exception as exc:
            logger.debug(
                "Channel delivery enqueue failed for session %s: %s",
                session_id, exc,
            )

    async def get_event_by_id(
        self,
        session_id: UUID,
        event_id: int,
    ) -> Event | None:
        """Return a single event by id, scoped to the given session.

        Returns ``None`` if no event with that id exists for the session —
        used by callers that need to validate an event reference (e.g. the
        feedback endpoint verifying a user rated a real ``expert.result``).
        """
        stmt = select(EventRow).where(
            EventRow.session_id == session_id, EventRow.id == event_id,
        )
        async with self._sf() as db:
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()
        return Event.model_validate(row) if row is not None else None

    async def find_skill_invocations(
        self,
        org_id: UUID,
        skill_name: str,
        *,
        since: datetime | None = None,
    ) -> list[Event]:
        """Return every ``skill.invoked`` event for *skill_name* in *org_id*.

        Ordered by event id ascending.  Used by the training-data
        collector's bootstrap path — each invocation is a labeled
        trajectory: "what the base LLM does when this skill fires."
        Filters via the ``events.org_id`` denormalized column so the
        query never joins ``sessions``.
        """
        stmt = (
            select(EventRow)
            .where(
                EventRow.org_id == org_id,
                EventRow.type == EventType.SKILL_INVOKED.value,
                EventRow.data["skill"].astext == skill_name,
            )
            .order_by(EventRow.id.asc())
        )
        if since is not None:
            stmt = stmt.where(EventRow.created_at >= since)
        async with self._sf() as db:
            result = await db.execute(stmt)
            rows = result.scalars().all()
        return [Event.model_validate(r) for r in rows]

    async def session_has_taint(self, session_id: UUID) -> bool:
        """True if the session has any quality-taint event.

        Matches the taint flags in ``v_training_candidates``:
        ``policy.denied``, ``harness.crash``, ``saga.compensate``, or
        ``expert.override``.  The training-data collector skips tainted
        sessions when ``exclude_tainted=True``.
        """
        stmt = text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM events"
            "  WHERE session_id = :sid"
            "    AND type IN ('policy.denied', 'harness.crash', "
            "                 'saga.compensate', 'expert.override')"
            ")"
        )
        async with self._sf() as db:
            result = await db.execute(stmt, {"sid": session_id})
            return bool(result.scalar())

    async def find_feedback_on_event(
        self,
        session_id: UUID,
        target_event_id: int,
        *,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> Event | None:
        """Return an existing feedback event emitted by the given principal
        on ``target_event_id`` for this session.

        Exactly one of ``user_id`` or ``service_account_id`` must be set
        — the first for human raters (web / Slack feedback), the second
        for automated judges submitting through the API channel.

        Covers all three feedback event types — ``EXPERT_ENDORSE``,
        ``EXPERT_OVERRIDE`` and ``USER_FEEDBACK`` — so the endpoint
        enforces per-principal idempotency regardless of what kind of
        assistant turn is being rated.
        """
        feedback_types = [
            EventType.EXPERT_ENDORSE.value,
            EventType.EXPERT_OVERRIDE.value,
            EventType.USER_FEEDBACK.value,
        ]
        if user_id is not None:
            principal_clause = (
                EventRow.data["rated_by_user_id"].astext == str(user_id)
            )
        elif service_account_id is not None:
            principal_clause = (
                EventRow.data["rated_by_service_account_id"].astext
                == str(service_account_id)
            )
        else:
            raise ValueError(
                "find_feedback_on_event requires user_id or service_account_id"
            )
        stmt = (
            select(EventRow)
            .where(
                EventRow.session_id == session_id,
                EventRow.type.in_(feedback_types),
                EventRow.data["target_event_id"].astext == str(target_event_id),
                principal_clause,
            )
            .limit(1)
        )
        async with self._sf() as db:
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()
        return Event.model_validate(row) if row is not None else None

    async def get_events(
        self,
        session_id: UUID,
        *,
        after: int | None = None,
        limit: int | None = None,
        types: list[EventType] | None = None,
        exclude_types: list[EventType] | None = None,
    ) -> list[Event]:
        """Read events from the append-only log, ordered by id ascending.

        ``types`` restricts to the given event types; ``exclude_types`` drops
        them (used by the SSE endpoint to skip llm.delta during replay —
        pushing per-token chunks through the wire for a long conversation
        makes the initial snapshot take many seconds).
        """
        stmt = select(EventRow).where(EventRow.session_id == session_id)

        if after is not None:
            stmt = stmt.where(EventRow.id > after)
        if types:
            stmt = stmt.where(EventRow.type.in_([t.value for t in types]))
        if exclude_types:
            stmt = stmt.where(
                EventRow.type.notin_([t.value for t in exclude_types])
            )

        stmt = stmt.order_by(EventRow.id.asc())

        if limit is not None:
            stmt = stmt.limit(limit)

        async with self._sf() as db:
            result = await db.execute(stmt)
            rows = result.scalars().all()
        return [Event.model_validate(r) for r in rows]

    # ------------------------------------------------------------------
    # Lease management (raw SQL — atomic upsert required)
    # ------------------------------------------------------------------

    async def try_acquire_lease(
        self,
        session_id: UUID,
        owner_id: str,
        ttl_seconds: int = 30,
    ) -> SessionLease | None:
        """Attempt to acquire an exclusive harness lease.

        Uses ``INSERT ... ON CONFLICT DO UPDATE ... WHERE expires_at < now()``
        so expired leases are atomically stolen.  Returns ``None`` if a
        valid (non-expired) lease is held by another owner.
        """
        token = uuid.uuid4()
        async with self._sf() as db:
            result = await db.execute(
                text(
                    """
                    INSERT INTO session_leases (session_id, owner_id, lease_token, expires_at)
                    VALUES (:session_id, :owner_id, :token, now() + make_interval(secs => :ttl))
                    ON CONFLICT (session_id) DO UPDATE
                        SET owner_id   = EXCLUDED.owner_id,
                            lease_token = EXCLUDED.lease_token,
                            expires_at  = EXCLUDED.expires_at,
                            updated_at  = now()
                        WHERE session_leases.expires_at < now()
                    RETURNING *
                    """
                ),
                {
                    "session_id": session_id,
                    "owner_id": owner_id,
                    "token": token,
                    "ttl": ttl_seconds,
                },
            )
            row = result.mappings().one_or_none()
            await db.commit()
        if row is None:
            return None
        return SessionLease.model_validate(dict(row))

    async def renew_lease(
        self,
        session_id: UUID,
        lease_token: UUID,
        ttl_seconds: int = 30,
    ) -> None:
        """Extend the lease expiry.  Raises ``LeaseNotHeldError`` on mismatch."""
        async with self._sf() as db:
            result = await db.execute(
                text(
                    """
                    UPDATE session_leases
                    SET expires_at = now() + make_interval(secs => :ttl),
                        updated_at = now()
                    WHERE session_id = :session_id
                      AND lease_token = :token
                    """
                ),
                {
                    "session_id": session_id,
                    "token": lease_token,
                    "ttl": ttl_seconds,
                },
            )
            if result.rowcount == 0:
                raise LeaseNotHeldError(
                    f"lease for session {session_id} not held by token {lease_token}"
                )
            await db.commit()

    async def release_lease(
        self,
        session_id: UUID,
        lease_token: UUID,
    ) -> None:
        """Release the harness lease.  Raises ``LeaseNotHeldError`` on mismatch."""
        async with self._sf() as db:
            result = await db.execute(
                delete(LeaseRow).where(
                    LeaseRow.session_id == session_id,
                    LeaseRow.lease_token == lease_token,
                )
            )
            if result.rowcount == 0:
                raise LeaseNotHeldError(
                    f"lease for session {session_id} not held by token {lease_token}"
                )
            await db.commit()

    # ------------------------------------------------------------------
    # Cursor management (raw SQL — atomic upsert + lease check)
    # ------------------------------------------------------------------

    async def get_harness_cursor(self, session_id: UUID) -> int:
        """Return the last fully-processed event id for the session."""
        async with self._sf() as db:
            result = await db.execute(
                select(SessionCursor.harness_cursor).where(
                    SessionCursor.session_id == session_id
                )
            )
            value = result.scalar_one_or_none()
        return int(value) if value is not None else 0

    async def advance_harness_cursor(
        self,
        session_id: UUID,
        through_event_id: int,
        lease_token: UUID,
    ) -> None:
        """Advance the durable cursor.  Only succeeds if the caller holds the lease."""
        async with self._sf() as db:
            # Verify lease ownership (SELECT FOR UPDATE).
            lease_row = (
                await db.execute(
                    text(
                        "SELECT lease_token FROM session_leases "
                        "WHERE session_id = :sid FOR UPDATE"
                    ),
                    {"sid": session_id},
                )
            ).mappings().one_or_none()

            if lease_row is None or lease_row["lease_token"] != lease_token:
                raise LeaseNotHeldError(
                    f"cannot advance cursor for session {session_id}: lease not held"
                )

            await db.execute(
                text(
                    """
                    INSERT INTO session_cursors (session_id, harness_cursor, updated_at)
                    VALUES (:sid, :cursor, now())
                    ON CONFLICT (session_id) DO UPDATE
                        SET harness_cursor = EXCLUDED.harness_cursor,
                            updated_at     = now()
                        WHERE session_cursors.harness_cursor < EXCLUDED.harness_cursor
                    """
                ),
                {"sid": session_id, "cursor": through_event_id},
            )
            await db.commit()

    async def get_pending_events(self, session_id: UUID) -> list[Event]:
        """Return events the harness has not yet processed."""
        async with self._sf() as db:
            result = await db.execute(
                text(
                    """
                    SELECT e.* FROM events e
                    LEFT JOIN session_cursors c ON c.session_id = e.session_id
                    WHERE e.session_id = :sid
                      AND e.id > COALESCE(c.harness_cursor, 0)
                    ORDER BY e.id ASC
                    """
                ),
                {"sid": session_id},
            )
            rows = result.mappings().all()
        return [Event.model_validate(dict(r)) for r in rows]

    # ------------------------------------------------------------------
    # Session reset (idle auto-reset)
    # ------------------------------------------------------------------

    async def find_idle_sessions(
        self,
        idle_minutes: int,
        agent_id: str,
        *,
        daily_at_hour: int | None = None,
        mode: str = "idle",
        limit: int = 200,
    ) -> list[Session]:
        """Find sessions that should be reset based on the reset policy.

        Returns sessions whose ``updated_at`` exceeds the idle threshold
        and/or crossed a daily boundary, depending on *mode*.

        Only sessions in ``active`` or ``idle`` status are considered.
        Sessions with an active lease (not yet expired) are excluded —
        they are currently being processed by a worker.

        Results are capped at *limit* (default 200) to bound memory and
        processing time per cron run.  Remaining sessions are picked up
        on the next run.
        """
        conditions: list[str] = []

        if mode in ("idle", "both"):
            conditions.append(
                "s.updated_at < now() - make_interval(mins => :idle_minutes)"
            )

        if mode in ("daily", "both") and daily_at_hour is not None:
            conditions.append(
                """s.updated_at < (
                    CASE WHEN EXTRACT(HOUR FROM now()) >= :at_hour
                         THEN date_trunc('day', now()) + make_interval(hours => :at_hour)
                         ELSE date_trunc('day', now()) - interval '1 day'
                              + make_interval(hours => :at_hour)
                    END
                )"""
            )

        if not conditions:
            return []

        if mode == "both":
            where_clause = "(" + " OR ".join(conditions) + ")"
        else:
            where_clause = conditions[0]

        query = f"""
            SELECT s.* FROM sessions s
            LEFT JOIN session_leases l
                ON l.session_id = s.id AND l.expires_at > now()
            WHERE s.status IN ('active', 'idle')
              AND s.agent_id = :agent_id
              AND l.session_id IS NULL
              AND {where_clause}
            ORDER BY s.updated_at ASC
            LIMIT :lim
        """

        params: dict[str, Any] = {
            "idle_minutes": idle_minutes,
            "agent_id": agent_id,
            "lim": limit,
        }
        if daily_at_hour is not None:
            params["at_hour"] = daily_at_hour

        async with self._sf() as db:
            result = await db.execute(text(query), params)
            rows = result.mappings().all()
        return [Session.model_validate(dict(r)) for r in rows]

    async def release_stale_lease(self, session_id: UUID) -> bool:
        """Delete a session's lease row only if it has already expired.

        Unlike :meth:`release_lease`, this does not require the original
        lease token — used by recovery sweepers that are cleaning up
        after a dead worker.  The ``expires_at < now()`` guard prevents
        accidentally kicking a legitimately-held lease if the sweeper
        runs concurrently with a fresh wake().  Returns True if a row
        was deleted.
        """
        async with self._sf() as db:
            result = await db.execute(
                text(
                    "DELETE FROM session_leases "
                    "WHERE session_id = :sid AND expires_at < now()"
                ),
                {"sid": session_id},
            )
            await db.commit()
            return result.rowcount > 0

    async def find_orphaned_sessions(
        self,
        *,
        stale_seconds: int,
        agent_id: str | None = None,
        limit: int = 200,
    ) -> list[Session]:
        """Find sessions abandoned by a dead worker.

        Returns sessions that are still ``active`` but whose lease either
        expired or never existed, AND whose last event landed more than
        ``stale_seconds`` ago.  These are the telltale signs of a worker
        that was hard-killed mid-turn (SIGKILL, OOM, debugger stop, pod
        eviction) — the harness's ``except`` / ``finally`` blocks never
        ran, so no ``HARNESS_CRASH`` or ``SESSION_FAIL`` landed in the
        event log and the session sits looking "running" to the UI
        forever.

        Sessions can legitimately remain ``active`` between turns.  That
        includes website visitors, web sessions before idle-reset flushes
        them, and API sessions owned by service accounts.  A stale,
        leaseless active session is therefore an orphan only when the
        latest event does not show a clean turn/session end.  A
        ``llm.response`` is clean only when it has no tool calls; if the
        worker was killed after the model asked for tools but before tool
        execution, recovery must still re-enqueue the session.

        The ``stale_seconds`` threshold must exceed the LLM's longest
        plausible inter-chunk gap (streaming thinking can pause for
        tens of seconds on reasoning models) to avoid racing with
        genuinely slow turns — callers should pick a value well above
        both the lease TTL and the worker's stream-stale timeout.

        Scoped to a single agent when ``agent_id`` is given so each
        agent's sweeper runs independently; omit to scan platform-wide.

        ``sessions.updated_at`` is the staleness signal:
        :func:`emit_event` bumps it on every event (see
        ``_build_counter_update_clause``), so an idle ``updated_at``
        narrows the candidate set without a LATERAL scan; the
        latest-event-type check then runs only on that small set.
        """
        # Correlated scalar subqueries: latest event for the session
        # under test.  Used to distinguish idle-between-turns sessions
        # from genuinely abandoned mid-turn sessions.
        latest_event_type = (
            select(EventRow.type)
            .where(EventRow.session_id == SessionRow.id)
            .order_by(EventRow.id.desc())
            .limit(1)
            .correlate(SessionRow)
            .scalar_subquery()
        )
        latest_event_data = (
            select(EventRow.data)
            .where(EventRow.session_id == SessionRow.id)
            .order_by(EventRow.id.desc())
            .limit(1)
            .correlate(SessionRow)
            .scalar_subquery()
        )
        session_end_event_types = (
            "session.done",
            "session.complete",
            "session.fail",
            "harness.crash",
        )
        latest_llm_response_is_clean = and_(
            latest_event_type == "llm.response",
            func.jsonb_array_length(
                func.coalesce(
                    latest_event_data["message"]["tool_calls"],
                    text("'[]'::jsonb"),
                )
            ) == 0,
        )
        latest_event_ended_work = or_(
            latest_event_type.in_(session_end_event_types),
            latest_llm_response_is_clean,
        )
        stmt = (
            select(SessionRow)
            .outerjoin(
                LeaseRow,
                (LeaseRow.session_id == SessionRow.id)
                & (LeaseRow.expires_at > func.now()),
            )
            .where(
                SessionRow.status == "active",
                LeaseRow.session_id.is_(None),
                SessionRow.updated_at
                < func.now() - text(f"make_interval(secs => {int(stale_seconds)})"),
                or_(
                    latest_event_type.is_(None),
                    not_(latest_event_ended_work),
                ),
            )
            .order_by(SessionRow.updated_at.asc())
            .limit(limit)
        )
        if agent_id:
            stmt = stmt.where(SessionRow.agent_id == agent_id)

        async with self._sf() as db:
            result = await db.execute(stmt)
            rows = result.scalars().all()
        return [Session.model_validate(r) for r in rows]

    async def reset_session(
        self,
        session_id: UUID,
        *,
        reason: str = "idle",
    ) -> None:
        """Mark a session as idle-reset.

        The session's events, counters, and cursor are left untouched —
        the user can come back and continue at any time.  A stale lease
        is cleaned up and a ``SESSION_RESET`` event is appended.
        """
        async with self._sf() as db:
            await db.execute(
                text("DELETE FROM session_leases WHERE session_id = :sid"),
                {"sid": session_id},
            )
            await db.commit()

        await self.emit_event(
            session_id,
            EventType.SESSION_RESET,
            {"reason": reason},
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_counter_update_clause(event_type: EventType, data: dict) -> str:
    """Return the SET fragment for atomic counter updates on event emission."""
    parts: list[str] = ["updated_at = now()"]

    if event_type == EventType.USER_MESSAGE:
        parts.append("message_count = message_count + 1")
    elif event_type == EventType.TOOL_CALL:
        parts.append("tool_call_count = tool_call_count + 1")

    if event_type == EventType.LLM_RESPONSE:
        input_tokens = int(data.get("input_tokens", 0))
        output_tokens = int(data.get("output_tokens", 0))
        cost = Decimal(str(data.get("cost_usd", 0)))
        if input_tokens:
            parts.append(f"input_tokens = input_tokens + {input_tokens}")
        if output_tokens:
            parts.append(f"output_tokens = output_tokens + {output_tokens}")
        if cost:
            parts.append(f"estimated_cost_usd = estimated_cost_usd + {cost}")

    return ", ".join(parts)
