"""Reclaim the workspaces of deleted sessions.

Session workspaces share one bucket and are keyed by session id, under the
session's own key prefix (``{storage_key_prefix}/{session_id}/...``).
Deleting a session archives its row and deletes its prefix, so this sweeper
is the backstop for the prefixes that survive that: a ``delete_prefix`` that
failed part-way, a worker killed mid-cleanup.

It is deliberately narrow, because what it deletes cannot be brought back.  A
prefix is removed only when a folder of the bucket is exactly where an
archived session's own config puts its workspace, and no session still
holding its workspace names that session as its sandbox root, the folder
is not inside another session's workspace, and no session's key prefix lies
at or under it.  A folder with
no session row is left alone: a key prefix can itself start with a UUID, so
such a folder may hold other sessions' files.  Anything else in the bucket,
the managed-channel ``boundaries/`` workspaces included, is left alone, and a
session the user still has is never touched no matter how old or how long
finished it is.

The K8s CronJob that invokes this script is ``runtime-cleanup``; it runs
every 6 hours.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

from sqlalchemy import select

from surogates.db.models import Session
from surogates.session.workspace_claims import roots_still_used

logger = logging.getLogger(__name__)


# Session ids per ``IN`` clause when asking which prefixes are still claimed.
_ID_CHUNK = 500

# How many folders a session's key prefix may be deep (``{project_id}/{agent_id}`` is two).
_MAX_PREFIX_DEPTH = 3

# What the delete route leaves behind. Rows are never removed, so this is
# the only signal that a workspace was meant to go.
_DELETED_STATUS = "archived"


def _candidate_folders(keys: list[str]) -> dict[uuid.UUID, set[str]]:
    """Each UUID that names a folder in *keys*, with what is above that folder."""
    found: dict[uuid.UUID, set[str]] = {}
    for key in keys:
        folders = key.split("/")[:-1]
        for depth, name in enumerate(folders[:_MAX_PREFIX_DEPTH + 1]):
            try:
                session_id = uuid.UUID(name)
            except ValueError:
                # Not a session workspace (``boundaries/…`` and friends).
                continue
            if str(session_id) == name:
                found.setdefault(session_id, set()).add("/".join(folders[:depth]))
    return found


async def _deleted_workspaces(
    session_factory: Any, found: dict[uuid.UUID, set[str]], bucket: str,
) -> list[str]:
    """The prefixes in *found* that are exactly the workspace of a deleted session no other session still uses."""
    ordered = sorted(found)
    prefixes: list[str] = []
    async with session_factory() as db:
        for start in range(0, len(ordered), _ID_CHUNK):
            chunk = ordered[start:start + _ID_CHUNK]
            rows = await db.execute(
                select(
                    Session.id,
                    Session.config["storage_key_prefix"].as_string(),
                    Session.config["storage_bucket"].as_string(),
                ).where(
                    Session.id.in_(chunk),
                    Session.status == _DELETED_STATUS,
                    # A project's sessions are archived without being deleted.
                    Session.config["workspace_boundary"].as_string().is_(None),
                ),
            )
            still_used = await roots_still_used(db, chunk)
            for session_id, key_prefix, its_bucket in rows.all():
                if session_id in still_used or (its_bucket and its_bucket != bucket):
                    continue
                above = (key_prefix or "").strip("/")
                if above in found[session_id]:
                    prefixes.append(f"{above}/{session_id}/" if above else f"{session_id}/")
        return sorted(await _clear_of_other_sessions(db, prefixes))


async def _clear_of_other_sessions(db: Any, prefixes: list[str]) -> list[str]:
    """Those of *prefixes* neither inside another session's workspace nor holding any session's key prefix."""
    if not prefixes:
        return []
    stamped = (await db.execute(select(Session.config["storage_key_prefix"].as_string()).distinct())).scalars().all()
    key_prefixes = {value.strip("/") for value in stamped if value and value.strip("/")}
    above: set[uuid.UUID] = set()
    for prefix in prefixes:
        for name in prefix.strip("/").split("/")[:-1]:
            with contextlib.suppress(ValueError):
                above.add(uuid.UUID(name))
    sessions_above: set[str] = set()
    ordered = sorted(above)
    for start in range(0, len(ordered), _ID_CHUNK):
        rows = await db.execute(select(Session.id).where(Session.id.in_(ordered[start:start + _ID_CHUNK])))
        sessions_above.update(str(session_id) for session_id in rows.scalars().all())
    return [
        prefix for prefix in prefixes
        if not sessions_above.intersection(prefix.strip("/").split("/")[:-1])
        and not any(f"{key_prefix}/".startswith(prefix) for key_prefix in key_prefixes)
    ]


async def sweep_orphan_workspaces(
    *,
    storage: Any,
    session_factory: Any,
    bucket: str,
) -> dict[str, int]:
    """Delete the workspaces of deleted sessions that are still in the bucket.

    Returns ``{"scanned", "orphans", "objects", "errors"}``.  A prefix
    that fails to delete is counted and skipped; one bad prefix does not
    abandon the rest of the sweep.
    """
    found = _candidate_folders(await storage.list_keys(bucket))
    if not found:
        return {"scanned": 0, "orphans": 0, "objects": 0, "errors": 0}

    orphans = await _deleted_workspaces(session_factory, found, bucket)
    objects = 0
    errors = 0
    for prefix in orphans:
        try:
            objects += await storage.delete_prefix(bucket, prefix)
        except Exception:  # noqa: BLE001 — one prefix must not stop the sweep
            errors += 1
            logger.error(
                "platform_cleanup failed to delete workspace %s",
                prefix, exc_info=True,
            )
    outcome = {
        "scanned": len(found),
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
