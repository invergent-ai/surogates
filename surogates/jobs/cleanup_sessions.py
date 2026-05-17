"""Session workspace cleanup job.

Deletes orphaned session workspace prefixes that have no matching active
session in the database.  Runs as a K8s CronJob to catch workspace objects
that were not cleaned up due to API server crashes or failed deletions.

Usage::

    python -m surogates.jobs.cleanup_sessions

Or via the CLI entry point::

    surogates cleanup-sessions [--dry-run]
"""

from __future__ import annotations

import asyncio
import logging
import sys
from uuid import UUID

from surogates.config import load_settings
from surogates.db.engine import async_engine_from_settings, async_session_factory
from surogates.storage.backend import create_backend
from surogates.storage.keys import prefixed
from surogates.storage.tenant import agent_session_bucket, session_workspace_prefix

logger = logging.getLogger(__name__)

_SESSIONS_DIR = "sessions/"


async def cleanup_orphaned_session_prefixes(
    storage,
    *,
    bucket: str,
    storage_key_prefix: str,
    active_session_ids: set[str],
    dry_run: bool = False,
) -> int:
    """Delete session prefixes in the agent bucket with no active DB row.

    Scopes the listing to the agent's ``storage_key_prefix`` so the
    cleanup is safe to run against a shared bucket: it only touches
    keys under ``{storage_key_prefix}/sessions/``.
    """
    bucket = agent_session_bucket(bucket)
    list_prefix = prefixed(_SESSIONS_DIR, storage_key_prefix)
    keys = await storage.list_keys(bucket, prefix=list_prefix)
    # Extract the session_id segment that follows ``sessions/`` in each key.
    session_ids: set[str] = set()
    for key in keys:
        relative = key[len(storage_key_prefix.rstrip("/")) + 1:] if storage_key_prefix else key
        if not relative.startswith(_SESSIONS_DIR):
            continue
        parts = relative.split("/", 2)
        if len(parts) >= 2:
            session_ids.add(parts[1])

    deleted = 0
    for session_id in sorted(session_ids):
        if session_id in active_session_ids:
            logger.debug(
                "Session prefix %s belongs to active session, skipping.",
                prefixed(session_workspace_prefix(session_id), storage_key_prefix),
            )
            continue

        prefix = prefixed(session_workspace_prefix(session_id), storage_key_prefix)
        if dry_run:
            logger.info("[DRY RUN] Would delete workspace prefix: %s", prefix)
        else:
            logger.info("Deleting orphaned workspace prefix: %s", prefix)
            await storage.delete_prefix(bucket, prefix)
        deleted += 1

    return deleted


async def cleanup_orphaned_buckets(dry_run: bool = False) -> int:
    """Delete session workspace prefixes with no matching active session.

    Returns the number of buckets deleted (or that would be deleted
    in dry-run mode).
    """
    settings = load_settings()
    storage = create_backend(settings)

    # Check which session IDs are still active in the database.
    engine = async_engine_from_settings(settings.db)
    factory = async_session_factory(engine)

    active_session_ids: set[str] = set()
    async with factory() as db_session:
        from sqlalchemy import select, text
        result = await db_session.execute(
            text("SELECT id FROM sessions WHERE status != 'archived'")
        )
        for row in result:
            active_session_ids.add(str(row[0]))

    await engine.dispose()

    try:
        deleted = await cleanup_orphaned_session_prefixes(
            storage,
            bucket=settings.storage.bucket,
            storage_key_prefix=settings.storage.key_prefix,
            active_session_ids=active_session_ids,
            dry_run=dry_run,
        )
    except Exception:
        logger.error("Failed to clean up orphaned session prefixes", exc_info=True)
        return 0

    logger.info(
        "%s %d orphaned session prefix(es).",
        "Would delete" if dry_run else "Deleted",
        deleted,
    )
    return deleted


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry_run = "--dry-run" in sys.argv
    count = asyncio.run(cleanup_orphaned_buckets(dry_run=dry_run))
    sys.exit(0 if count >= 0 else 1)


if __name__ == "__main__":
    main()
