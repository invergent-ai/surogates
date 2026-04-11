"""Session bucket cleanup job.

Deletes orphaned ``session-*`` storage buckets that have no matching
active session in the database.  Runs as a K8s CronJob to catch buckets
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

logger = logging.getLogger(__name__)

SESSION_BUCKET_PREFIX = "session-"


async def cleanup_orphaned_buckets(dry_run: bool = False) -> int:
    """Delete session buckets with no matching active session.

    Returns the number of buckets deleted (or that would be deleted
    in dry-run mode).
    """
    settings = load_settings()
    storage = create_backend(settings)

    bucket_names = await storage.list_buckets(prefix=SESSION_BUCKET_PREFIX)

    if not bucket_names:
        logger.info("No session buckets found.")
        return 0

    logger.info("Found %d session bucket(s).", len(bucket_names))

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

    # Delete orphaned buckets.
    deleted = 0
    for bucket_name in bucket_names:
        session_id = bucket_name[len(SESSION_BUCKET_PREFIX):]
        if session_id in active_session_ids:
            logger.debug("Bucket %s belongs to active session, skipping.", bucket_name)
            continue

        if dry_run:
            logger.info("[DRY RUN] Would delete bucket: %s", bucket_name)
        else:
            logger.info("Deleting orphaned bucket: %s", bucket_name)
            try:
                await storage.delete_bucket(bucket_name)
            except Exception:
                logger.error("Failed to delete bucket %s", bucket_name, exc_info=True)
                continue
        deleted += 1

    logger.info(
        "%s %d orphaned session bucket(s).",
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
