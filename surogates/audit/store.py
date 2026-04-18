"""Tenant-scoped audit log persistence.

Writes to the ``audit_log`` table.  External audit consumers read the
table directly — this module is write-only.  Kept minimal on purpose:
every emission site builds its ``data`` payload via
:mod:`surogates.audit.events` helpers and calls :meth:`AuditStore.emit`.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.audit.types import AuditType
from surogates.db.models import AuditLog

logger = logging.getLogger(__name__)


class AuditStore:
    """Append-only writer for the ``audit_log`` table.

    One instance is shared by all emitters (auth providers, MCP
    governance, credential vault).  Emission failures are logged but
    never propagated — audit logging must not break the user-facing
    flow it observes.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._sf = session_factory

    async def emit(
        self,
        *,
        org_id: UUID,
        type: AuditType,
        data: dict[str, Any],
        user_id: UUID | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> int | None:
        """Append an audit log entry.

        Returns the newly assigned row id on success, or ``None`` if
        persistence failed (e.g. DB unavailable).  Failures are logged
        at ``error`` level but never raised — the caller's business
        logic must continue regardless.
        """
        row = AuditLog(
            org_id=org_id,
            user_id=user_id,
            type=type.value,
            data=data,
            trace_id=trace_id,
            span_id=span_id,
        )
        try:
            async with self._sf() as db:
                db.add(row)
                await db.flush()
                row_id = row.id
                await db.commit()
            return row_id
        except Exception:
            logger.exception(
                "Failed to emit audit event %s for org %s",
                type.value, org_id,
            )
            return None
