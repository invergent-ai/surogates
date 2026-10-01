"""Which computer a session works on, and whether that computer has accepted it.

A session that works on a folder of the user's computer names its device in
``config["execution"]``, stamped by the server when the session is created and
copied to every session created under it.  Its binding is the root session's
first device operation, ``bind``: the computer answers it once its user has
confirmed the folder there.  Until then the session takes no messages and its
device runs nothing for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from surogates.db.models import DeviceOperation

# The binding's kind, and the invocation it is recorded under: a tool call's
# invocation id never takes this form, and its ordinals start at 1.
BIND = "bind"


def device_of(config: dict[str, Any] | None) -> UUID | None:
    """The device a session works on, or None for a session in the cloud."""
    execution = (config or {}).get("execution")
    if not isinstance(execution, dict) or execution.get("kind") != "device":
        return None
    return UUID(execution["device_id"])


@dataclass(frozen=True, slots=True)
class Binding:
    state: Literal["pending", "bound", "failed"]
    # Why a failed binding failed, as the computer or the server put it.
    message: str | None = None


async def binding_of(db: AsyncSession, root_session_id: UUID) -> Binding:
    """Whether the computer accepted the root session's folder."""
    row = (await db.execute(
        select(DeviceOperation.outcome).where(
            DeviceOperation.calling_session_id == root_session_id,
            DeviceOperation.invocation_id == BIND,
            DeviceOperation.ordinal == 0,
        )
    )).one_or_none()
    if row is None:
        # The session was created but recording its binding failed: no
        # computer will ever answer it.  A client only sends once creation
        # returned, by which time a successful create has recorded it.
        return Binding("failed", "The computer was never asked to set it up")
    outcome = row.outcome
    if outcome is None:
        return Binding("pending")
    if "ok" in outcome:
        return Binding("bound")
    error = outcome.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    return Binding("failed", str(message or "The computer refused this folder"))
