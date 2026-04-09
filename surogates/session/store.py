"""PostgreSQL-backed session store — the system's durable core.

Every mutation to sessions, events, leases, and cursors goes through this
module.  All writes use explicit transactions so that event emission and
counter updates are atomic.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text

from surogates.session.events import EventType
from surogates.session.models import Event, Session, SessionLease

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class SessionNotFoundError(Exception):
    """Raised when a session lookup finds no matching row."""


class LeaseNotHeldError(Exception):
    """Raised when a lease operation fails because the caller does not hold it."""


# ---------------------------------------------------------------------------
# Counter update rules: which event types bump which session counters.
# ---------------------------------------------------------------------------

_COUNTER_SQL_FRAGMENTS: dict[EventType, str] = {
    EventType.USER_MESSAGE: "message_count = message_count + 1",
    EventType.TOOL_CALL: "tool_call_count = tool_call_count + 1",
}

_TOKEN_BEARING_TYPES: frozenset[EventType] = frozenset(
    {EventType.LLM_RESPONSE},
)


def _build_counter_update_clause(event_type: EventType, data: dict) -> str:
    """Return the SET fragment for atomic counter updates on event emission."""
    parts: list[str] = ["updated_at = now()"]

    static = _COUNTER_SQL_FRAGMENTS.get(event_type)
    if static is not None:
        parts.append(static)

    if event_type in _TOKEN_BEARING_TYPES:
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


class SessionStore:
    """Async, PostgreSQL-backed store for sessions, events, leases, and cursors.

    Requires an ``async_sessionmaker`` bound to an ``asyncpg`` engine.  All
    public methods acquire their own connection from the pool via ``async with
    self._session_factory() as db``.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    async def create_session(
        self,
        *,
        user_id: UUID,
        org_id: UUID,
        channel: str = "web",
        model: str | None = None,
        config: dict | None = None,
        parent_id: UUID | None = None,
    ) -> Session:
        """Create a new session and return its Pydantic representation."""
        session_id = uuid.uuid4()
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    text(
                        """
                        INSERT INTO sessions
                            (id, user_id, org_id, channel, status, model, config, parent_id)
                        VALUES
                            (:id, :user_id, :org_id, :channel, 'active', :model, CAST(:config AS jsonb), :parent_id)
                        RETURNING *
                        """
                    ),
                    {
                        "id": session_id,
                        "user_id": user_id,
                        "org_id": org_id,
                        "channel": channel,
                        "model": model,
                        "config": _json_dumps(config or {}),
                        "parent_id": parent_id,
                    },
                )
            ).mappings().one()
            # Initialise the cursor row so get_harness_cursor never 404s.
            await db.execute(
                text(
                    "INSERT INTO session_cursors (session_id) VALUES (:sid)"
                ),
                {"sid": session_id},
            )
            await db.commit()
        return Session.model_validate(dict(row))

    async def get_session(self, session_id: UUID) -> Session:
        """Fetch a single session by ID.  Raises ``SessionNotFoundError``."""
        async with self._session_factory() as db:
            result = await db.execute(
                text("SELECT * FROM sessions WHERE id = :id"),
                {"id": session_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            raise SessionNotFoundError(f"session {session_id} not found")
        return Session.model_validate(dict(row))

    async def update_session_status(self, session_id: UUID, status: str) -> None:
        """Set a session's status and touch ``updated_at``."""
        async with self._session_factory() as db:
            result = await db.execute(
                text(
                    """
                    UPDATE sessions
                    SET status = :status, updated_at = now()
                    WHERE id = :id
                    """
                ),
                {"id": session_id, "status": status},
            )
            if result.rowcount == 0:
                raise SessionNotFoundError(f"session {session_id} not found")
            await db.commit()

    async def list_sessions(
        self,
        org_id: UUID,
        user_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Session]:
        """Return sessions for a user within an org, newest first."""
        async with self._session_factory() as db:
            result = await db.execute(
                text(
                    """
                    SELECT * FROM sessions
                    WHERE org_id = :org_id AND user_id = :user_id
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {
                    "org_id": org_id,
                    "user_id": user_id,
                    "limit": limit,
                    "offset": offset,
                },
            )
            rows = result.mappings().all()
        return [Session.model_validate(dict(r)) for r in rows]

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
        """
        counter_clause = _build_counter_update_clause(event_type, data)
        async with self._session_factory() as db:
            # 1. INSERT the event row.
            event_row = (
                await db.execute(
                    text(
                        """
                        INSERT INTO events (session_id, type, data)
                        VALUES (:session_id, :type, CAST(:data AS jsonb))
                        RETURNING id
                        """
                    ),
                    {
                        "session_id": session_id,
                        "type": event_type.value,
                        "data": _json_dumps(data),
                    },
                )
            ).mappings().one()
            event_id: int = event_row["id"]

            # 2+3. UPDATE session counters + updated_at.
            await db.execute(
                text(
                    f"UPDATE sessions SET {counter_clause} WHERE id = :id"  # noqa: S608
                ),
                {"id": session_id},
            )
            await db.commit()
        return event_id

    async def get_events(
        self,
        session_id: UUID,
        *,
        after: int | None = None,
        limit: int | None = None,
        types: list[EventType] | None = None,
    ) -> list[Event]:
        """Read events from the append-only log, ordered by id ascending."""
        clauses = ["session_id = :session_id"]
        params: dict = {"session_id": session_id}

        if after is not None:
            clauses.append("id > :after")
            params["after"] = after

        if types:
            clauses.append("type = ANY(:types)")
            params["types"] = [t.value for t in types]

        where = " AND ".join(clauses)
        query = f"SELECT * FROM events WHERE {where} ORDER BY id ASC"  # noqa: S608

        if limit is not None:
            query += " LIMIT :limit"
            params["limit"] = limit

        async with self._session_factory() as db:
            result = await db.execute(text(query), params)
            rows = result.mappings().all()
        return [Event.model_validate(dict(r)) for r in rows]

    # ------------------------------------------------------------------
    # Lease management
    # ------------------------------------------------------------------

    async def try_acquire_lease(
        self,
        session_id: UUID,
        owner_id: str,
        ttl_seconds: int = 30,
    ) -> SessionLease | None:
        """Attempt to acquire an exclusive harness lease for *session_id*.

        Uses an ``INSERT ... ON CONFLICT DO UPDATE ... WHERE expires_at < now()``
        pattern so that expired leases are atomically stolen.  Returns ``None``
        if a valid (non-expired) lease is held by another owner.
        """
        token = uuid.uuid4()
        async with self._session_factory() as db:
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
        """Extend the lease expiry.  Raises ``LeaseNotHeldError`` if the token doesn't match."""
        async with self._session_factory() as db:
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
        async with self._session_factory() as db:
            result = await db.execute(
                text(
                    """
                    DELETE FROM session_leases
                    WHERE session_id = :session_id
                      AND lease_token = :token
                    """
                ),
                {"session_id": session_id, "token": lease_token},
            )
            if result.rowcount == 0:
                raise LeaseNotHeldError(
                    f"lease for session {session_id} not held by token {lease_token}"
                )
            await db.commit()

    # ------------------------------------------------------------------
    # Cursor management
    # ------------------------------------------------------------------

    async def get_harness_cursor(self, session_id: UUID) -> int:
        """Return the last fully-processed event id for the session."""
        async with self._session_factory() as db:
            result = await db.execute(
                text(
                    "SELECT harness_cursor FROM session_cursors WHERE session_id = :sid"
                ),
                {"sid": session_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            return 0
        return int(row["harness_cursor"])

    async def advance_harness_cursor(
        self,
        session_id: UUID,
        through_event_id: int,
        lease_token: UUID,
    ) -> None:
        """Advance the durable cursor.  Only succeeds if the caller holds the lease.

        The lease check and cursor update happen in a single transaction to
        prevent races.
        """
        async with self._session_factory() as db:
            # Verify lease ownership first (SELECT FOR UPDATE to lock the row).
            lease_row = (
                await db.execute(
                    text(
                        """
                        SELECT lease_token FROM session_leases
                        WHERE session_id = :sid
                        FOR UPDATE
                        """
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
        """Return events that the harness has not yet processed.

        Equivalent to ``get_events(session_id, after=get_harness_cursor(session_id))``,
        but executed as a single round-trip.
        """
        async with self._session_factory() as db:
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _json_dumps(obj: dict) -> str:
    """Serialize a dict to a JSON string for use with ``::jsonb`` casts."""
    import json

    return json.dumps(obj, default=str)
