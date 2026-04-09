"""Database layer -- models, engine, and session helpers."""

from __future__ import annotations

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
    "Skill",
    "User",
]
