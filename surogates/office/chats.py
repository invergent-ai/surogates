"""The chats an open document belongs to."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from surogates.session.store import SessionNotFoundError, SessionStore


async def live_session(store: SessionStore, session_ids: Iterable[UUID]):
    """The first of *session_ids* that still exists and is not archived, or None."""
    for session_id in session_ids:
        try:
            session = await store.get_session(UUID(str(session_id)))
        except SessionNotFoundError:
            continue
        if session.status != "archived":
            return session
    return None
