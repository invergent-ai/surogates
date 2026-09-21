"""Reclaim session workspaces the session no longer has a claim on.

Session workspaces share one bucket and are keyed by session id
(``{session_id}/...``).  Deleting a session archives its row and deletes
its prefix, so this sweeper is the backstop for the prefixes that survive
that: a ``delete_prefix`` that failed part-way, a worker killed
mid-cleanup, a row that never got one.

It is deliberately narrow.  A prefix is removed only when its first path
segment parses as a UUID *and* that session is archived — the tombstone
the delete route writes — or has no row at all.  Anything else in the
bucket, the managed-channel ``boundaries/`` workspaces included, is left
alone, and a session the user still has is never touched no matter how
old or how long finished it is.

The K8s CronJob that invokes this script is ``runtime-cleanup``; it runs
every 6 hours.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import select

from surogates.db.models import Session

logger = logging.getLogger(__name__)


# Session ids per ``IN`` clause when asking which prefixes are still claimed.
_ID_CHUNK = 500

# What the delete route leaves behind. Rows are never removed, so this is
# the only signal that a workspace was meant to go.
_DELETED_STATUS = "archived"


def _candidate_session_ids(keys: list[str]) -> set[uuid.UUID]:
    """Session ids that own a top-level prefix in *keys*."""
    found: set[uuid.UUID] = set()
    for key in keys:
        head = key.split("/", 1)[0]
        try:
            found.add(uuid.UUID(head))
        except ValueError:
            # Not a session workspace (``boundaries/…`` and friends).
            continue
    return found


async def _claimed_session_ids(
    session_factory: Any, candidates: set[uuid.UUID],
) -> set[uuid.UUID]:
    """Subset of *candidates* whose session still claims its workspace."""
    ordered = sorted(candidates)
    claimed: set[uuid.UUID] = set()
    async with session_factory() as db:
        for start in range(0, len(ordered), _ID_CHUNK):
            chunk = ordered[start:start + _ID_CHUNK]
            rows = await db.execute(
                select(Session.id).where(
                    Session.id.in_(chunk),
                    Session.status != _DELETED_STATUS,
                ),
            )
            claimed.update(rows.scalars().all())
    return claimed


async def sweep_orphan_workspaces(
    *,
    storage: Any,
    session_factory: Any,
    bucket: str,
) -> dict[str, int]:
    """Delete the workspace prefixes no session claims any more.

    Returns ``{"scanned", "orphans", "objects", "errors"}``.  A prefix
    that fails to delete is counted and skipped; one bad prefix does not
    abandon the rest of the sweep.
    """
    candidates = _candidate_session_ids(await storage.list_keys(bucket))
    if not candidates:
        return {"scanned": 0, "orphans": 0, "objects": 0, "errors": 0}

    orphans = sorted(candidates - await _claimed_session_ids(
        session_factory, candidates,
    ))
    objects = 0
    errors = 0
    for session_id in orphans:
        try:
            objects += await storage.delete_prefix(bucket, f"{session_id}/")
        except Exception:  # noqa: BLE001 — one prefix must not stop the sweep
            errors += 1
            logger.error(
                "platform_cleanup failed to delete workspace %s",
                session_id, exc_info=True,
            )
    outcome = {
        "scanned": len(candidates),
        "orphans": len(orphans),
        "objects": objects,
        "errors": errors,
    }
    logger.info("platform_cleanup swept %s", outcome)
    return outcome


async def main() -> dict[str, int]:
    """CLI entry for the platform cleanup CronJob."""
    from surogates.config import load_settings
    from surogates.db.engine import (
        async_engine_from_settings, async_session_factory,
    )
    from surogates.storage.backend import create_backend
    from surogates.storage.tenant import agent_session_bucket

    settings = load_settings()
    engine = async_engine_from_settings(settings.db)
    try:
        return await sweep_orphan_workspaces(
            storage=create_backend(settings),
            session_factory=async_session_factory(engine),
            bucket=agent_session_bucket(settings.storage.bucket),
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":  # pragma: no cover - CLI path
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(main())
