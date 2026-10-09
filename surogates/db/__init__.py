"""Database layer -- models, engine, and session helpers."""

from __future__ import annotations

# Imported for what it sets on every engine of the process: see the module.
from surogates.db import many_rows  # noqa: F401
from surogates.db.engine import (
    async_engine_from_settings,
    async_session_factory,
    run_migrations,
)
from surogates.db.models import (
    Base,
    ChannelIdentity,
    Credential,
    DeliveryCursor,
    DeliveryOutbox,
    Event,
    McpServer,
    Org,
    Session,
    SessionCursor,
    SessionLease,
    ScheduledSession,
    Skill,
    User,
)

__all__ = [
    # Engine / session helpers
    "async_engine_from_settings",
    "async_session_factory",
    "run_migrations",
    # ORM base
    "Base",
    # Models
    "ChannelIdentity",
    "Credential",
    "DeliveryCursor",
    "DeliveryOutbox",
    "Event",
    "McpServer",
    "Org",
    "Session",
    "SessionCursor",
    "SessionLease",
    "ScheduledSession",
    "Skill",
    "User",
]
