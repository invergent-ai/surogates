"""Whether a session workspace is still in use, asked before it is deleted.

A delegated child works in its root's workspace, so a prefix can have several
sessions.  It goes only with the last of them.  Whenever that cannot be told,
it stays: a prefix left behind can be swept later, a deleted one cannot be
brought back.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select

from surogates.db.models import Session as SessionRow

logger = logging.getLogger(__name__)


def holds_its_workspace() -> Any:
    """The rows that still hold their workspace: any but a deleted session.

    ``archived`` is what the delete route leaves.  A project's sessions are
    also archived without being deleted, and they carry a boundary.
    """
    return or_(SessionRow.status != "archived", SessionRow.config["workspace_boundary"].as_string().isnot(None))


async def workspace_still_used(
    session_factory: Any, *, org_id: UUID, root_id: UUID | str, deleting: Iterable[UUID],
) -> bool:
    """Whether a session of *org_id* outside *deleting* uses the workspace of *root_id*; True when it cannot be told."""
    try:
        root = UUID(str(root_id))
        async with session_factory() as db:
            # The bucket is shared: a root this org cannot be shown to own is not its to delete.
            owner = await db.scalar(select(SessionRow.org_id).where(SessionRow.id == root))
            if owner != org_id:
                logger.warning("Workspace %s is not shown to belong to the deleted session's org: keeping it", root_id)
                return True
            found = await db.scalar(
                select(SessionRow.id).where(
                    SessionRow.org_id == org_id,
                    SessionRow.id.not_in(list(deleting)),
                    holds_its_workspace(),
                    or_(SessionRow.id == root, SessionRow.config["sandbox_root_session_id"].as_string() == str(root)),
                ).limit(1)
            )
    except Exception as exc:  # noqa: BLE001 — whatever went wrong, the files stay
        logger.warning("Could not tell whether workspace %s is still used (%s): keeping it", root_id, type(exc).__name__)
        return True
    return found is not None


async def roots_still_used(db: Any, candidates: list[UUID]) -> set[UUID]:
    """Those of *candidates* that a session still holding its workspace names as its sandbox root."""
    named = SessionRow.config["sandbox_root_session_id"].as_string()
    rows = await db.execute(
        select(named).where(named.in_([str(candidate) for candidate in candidates]), holds_its_workspace()).distinct()
    )
    return {UUID(value) for value in rows.scalars().all()}
