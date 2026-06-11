"""Coordination-board read path for AgentHarness.

Append-only durable delivery (spec §7): the first iteration that sees a
non-empty board appends one full-snapshot ``board.update`` event; later
iterations append compact deltas for rows whose ``seq`` moved past the
session's cursor.  Events append at the END of history — never inserted
mid-list — so the provider prefix cache and event replay stay stable
(same idiom as AdvisorMixin).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from surogates.board.render import render_board, render_delta
from surogates.board.store import BoardStore
from surogates.config import get_board_settings
from surogates.session.events import EventType

logger = logging.getLogger(__name__)


class BoardMixin:
    async def maybe_emit_board_update(
        self,
        session: Any,
        messages: list[dict],
        cursor: int | None,
    ) -> int | None:
        """Top-of-iteration board hook.  Returns the (possibly advanced)
        cursor; appends at most one message + one durable event.

        Never raises: a board failure must not break the agent loop.
        """
        try:
            return await self._board_update_inner(session, messages, cursor)
        except Exception:
            logger.exception(
                "board: update hook failed for session %s (continuing)",
                session.id,
            )
            return cursor

    async def _board_update_inner(
        self,
        session: Any,
        messages: list[dict],
        cursor: int | None,
    ) -> int | None:
        config = session.config or {}
        raw_group = config.get("context_group_id")
        if not raw_group:
            return cursor

        try:
            group_id = UUID(str(raw_group))
        except ValueError:
            logger.warning(
                "board: session %s has malformed context_group_id %r",
                session.id, raw_group,
            )
            return cursor

        settings = get_board_settings()
        board = BoardStore(self._session_factory)
        now = datetime.now(timezone.utc)

        if cursor is None:
            notes = await board.active_notes(group_id)
            content = render_board(
                notes, max_tokens=settings.snapshot_window_tokens, now=now,
            )
            if not content:
                # Board still empty: stay unjoined, re-check next iteration.
                return None
            new_cursor = await board.max_seq(group_id)
            kind = "snapshot"
        else:
            changed = await board.changes_since(group_id, cursor)
            if not changed:
                return cursor
            content = render_delta(
                changed, max_chars=settings.delta_max_chars, now=now,
            )
            new_cursor = max(n.seq for n in changed)
            kind = "delta"

        await self._store.emit_event(
            session.id,
            EventType.BOARD_UPDATE,
            {
                "group_id": str(group_id),
                "kind": kind,
                "cursor_from": cursor,
                "cursor_to": new_cursor,
                "content": content,
            },
        )
        messages.append({"role": "user", "content": content})
        await self._store.update_session_config_key(
            session.id, "board_cursor", new_cursor,
        )
        return new_cursor
