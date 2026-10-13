"""The worker job that lands saves from the document editor and closes stale sessions."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from surogates.config import Settings
from surogates.office.reaper import reap
from surogates.office.saves import land_pending_saves

logger = logging.getLogger(__name__)

REAP_EVERY = 30  # passes, about a minute apart at the default interval


async def run_office_loop(*, session_factory: Any, session_store: Any, storage: Any, redis: Any, settings: Settings,
                          interval_seconds: float = 2.0) -> None:
    office = settings.office
    if not office.enabled:
        return
    passes = 0
    while True:
        try:
            await land_pending_saves(session_factory=session_factory, session_store=session_store,
                                     storage=storage, redis=redis, office=office)
            if passes % REAP_EVERY == 0:
                await reap(session_factory=session_factory, session_store=session_store, office=office,
                           storage=storage)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("office saves pass failed")
        passes += 1
        await asyncio.sleep(interval_seconds)
