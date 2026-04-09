"""Session module — the append-only event log and its supporting primitives."""

from __future__ import annotations

from surogates.session.events import EventType
from surogates.session.models import Event, Session, SessionLease
from surogates.session.store import (
    LeaseNotHeldError,
    SessionNotFoundError,
    SessionStore,
)

__all__ = [
    "Event",
    "EventType",
    "LeaseNotHeldError",
    "Session",
    "SessionLease",
    "SessionNotFoundError",
    "SessionStore",
]
