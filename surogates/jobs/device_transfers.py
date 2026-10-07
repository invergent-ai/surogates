"""Deletes device transfers nothing will read again, and the user's own requests once done.

See ``surogates.devices.operations.reap_transfers`` and ``reap_requests``.

Runs forever on an interval in every worker, as ``jobs.board_maintenance``
does; passes from several workers at once delete the same rows harmlessly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from surogates.devices.operations import reap_requests, reap_transfers

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_INTERVAL_SECONDS = 300.0


async def run_transfer_reaper_loop(
    session_factory: Any,
    *,
    interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
) -> None:
    """Reap device transfers and finished requests until cancelled."""
    while True:
        try:
            reaped = await reap_transfers(session_factory)
            if reaped:
                logger.info("device transfers reaped: %d", reaped)
            requests = await reap_requests(session_factory)
            if requests:
                logger.info("device request rows reaped: %d", requests)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("device transfer reaper failed")
        await asyncio.sleep(interval_seconds)
