"""Coordination-group formation at spawn time.

Rule (spec §5): on every spawn, the parent self-assigns
``context_group_id = str(parent.id)`` if absent (persisted, and mirrored
into the parent's live config dict so the current wake's board hook sees
it), and the child inherits the parent's group id.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def ensure_group_and_inherit(
    *,
    parent_session: Any,
    session_store: Any,
    child_config: dict[str, Any],
    live_parent_config: dict[str, Any] | None = None,
) -> str:
    """Ensure the parent is a group member; stamp the child config.

    ``live_parent_config`` is the in-memory config dict of the CALLING
    harness (the ``session_config`` tool kwarg).  Mutating it makes the
    parent's board read-path activate in the same wake; tool visibility
    still refreshes on the next wake (tool filter is computed per wake).
    Spawn paths that run outside the parent's wake (task dispatcher
    retries) pass ``None``.

    Concurrent first-spawns race benignly: both writers persist the
    identical value under the store's row lock.

    Returns the group id (str).
    """
    parent_config = parent_session.config or {}
    group_id = parent_config.get("context_group_id")
    if not group_id:
        group_id = str(parent_session.id)
        await session_store.update_session_config_key(
            parent_session.id, "context_group_id", group_id,
        )
        if live_parent_config is not None:
            live_parent_config["context_group_id"] = group_id
        logger.info(
            "board: session %s formed coordination group %s",
            parent_session.id, group_id,
        )
    child_config["context_group_id"] = str(group_id)
    return str(group_id)
