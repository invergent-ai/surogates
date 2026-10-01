"""Telling a session's viewers when its local work waits for its computer.

``device.waiting {device_id, device_name, reason}`` goes out when the first
of a session's operations finds its computer away, and ``device.resumed
{device_id}`` once none of them does.  The count is per calling session and
lives in this process: a session's turn runs in one worker at a time.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from surogates.db.models import Device
from surogates.session.events import EventType

if TYPE_CHECKING:
    from surogates.devices.operations import OperationRequest

logger = logging.getLogger(__name__)

# Calling session -> how many of its operations wait on an absent computer.
_away: Counter[UUID] = Counter()


class DeviceWaitNotice:
    def __init__(self, store: Any, session_factory: Any) -> None:
        self._store = store
        self._sf = session_factory

    async def away(self, request: OperationRequest) -> None:
        """One more of the session's operations finds its computer away."""
        _away[request.calling_session_id] += 1
        if _away[request.calling_session_id] == 1:
            await self._emit(request.calling_session_id, EventType.DEVICE_WAITING, {
                "device_id": str(request.device_id),
                "device_name": await self._name(request.device_id),
                "reason": "offline",
            })

    async def back(self, request: OperationRequest) -> None:
        """One of the session's operations no longer waits on an absent computer."""
        _away[request.calling_session_id] -= 1
        if _away[request.calling_session_id] <= 0:
            del _away[request.calling_session_id]
            await self._emit(request.calling_session_id, EventType.DEVICE_RESUMED, {
                "device_id": str(request.device_id),
            })

    async def _name(self, device_id: UUID) -> str:
        async with self._sf() as db:
            name = (await db.execute(select(Device.name).where(Device.id == device_id))).scalar_one_or_none()
        return name or "your computer"

    async def _emit(self, session_id: UUID, event_type: EventType, data: dict[str, Any]) -> None:
        # Telling viewers is best effort: it must never fail the wait.
        try:
            await self._store.emit_event(session_id, event_type, data)
        except Exception:
            logger.warning("could not tell session %s that %s", session_id, event_type.value, exc_info=True)
