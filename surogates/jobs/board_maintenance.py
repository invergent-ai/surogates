"""Coordination-board maintenance sweeper.

One pass = claim expiry + the three purge clauses from the spec (§9 of
docs/superpowers/specs/2026-06-11-coordination-board-design.md):
terminal-root groups, aged superseded/expired rows, orphaned groups.
Runs forever on an interval when started via
:func:`run_board_maintenance_loop` (same lifecycle as
``jobs.inbox_expire.run_expire_loop``).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from surogates.board.store import BoardStore
from surogates.config import get_board_settings

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_INTERVAL_SECONDS = 300.0


async def board_maintenance_pass(
    session_factory: Any,
    *,
    purge_after_days: int,
) -> dict[str, int]:
    """Run one maintenance pass; returns per-clause row counts."""
    board = BoardStore(session_factory)
    stats = {
        "claims_expired": await board.expire_due_claims(),
        "purged_terminal_root": await board.purge_terminal_root_groups(
            older_than_days=purge_after_days,
        ),
        "purged_stale_rows": await board.purge_stale_rows(
            older_than_days=purge_after_days,
        ),
        "purged_orphaned": await board.purge_orphaned_groups(),
    }
    if any(stats.values()):
        logger.info("board maintenance: %s", stats)
    return stats


async def run_board_maintenance_loop(
    session_factory: Any,
    *,
    interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
) -> None:
    """Run the board maintenance sweeper until cancelled."""
    settings = get_board_settings()
    while True:
        try:
            await board_maintenance_pass(
                session_factory,
                purge_after_days=settings.purge_after_days,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("board maintenance sweep failed")
        await asyncio.sleep(interval_seconds)
