"""Which computer a session works on, and whether that computer has accepted it.

A session that works on a folder of the user's computer names its device in
``config["execution"]``, stamped by the server when the session is created and
copied to every session created under it.  Its binding is the root session's
first device operation, ``bind``: the computer answers it once its user has
confirmed the folder there.  Until then the session takes no messages and its
device runs nothing for it.

A project's thread may work in a copy of the folder of its own instead, which
its computer keeps: the server says so in the same stamp
(``execution.history``), its bind names the copy, and the computer's answer
must say it keeps that copy, or the thread has nowhere to work.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from surogates.db.models import DeviceOperation

# The binding's kind, and the invocation it is recorded under: a tool call's
# invocation id never takes this form, and its ordinals start at 1.
BIND = "bind"

# The same for a deleted root's retirement: its computer forgets the folder.
RETIRE = "retire"

# The kinds only a project thread's own turn asks its computer for
# (surogates.devices.history): a snapshot of its copy or the copy put back,
# a step of its folder's history, a landing's look at the folder and its
# writes into it.
THREAD_KINDS = frozenset({"checkpoint", "history", "land"})

# The sandbox keys of the session on the user's computer this task works for,
# set by AgentHarness.wake (see surogates.devices.sandbox).  Here, not there,
# so the workspace fallback can read it without importing the device journal.
device_owners: ContextVar[frozenset[str]] = ContextVar("surogates_device_owners", default=frozenset())


def device_of(config: dict[str, Any] | None) -> UUID | None:
    """The device a session works on, or None for a session in the cloud."""
    execution = (config or {}).get("execution")
    if not isinstance(execution, dict) or execution.get("kind") != "device":
        return None
    return UUID(execution["device_id"])


def copy_of(config: dict[str, Any] | None) -> UUID | None:
    """The thread whose copy of the folder a session works in on its computer.

    None for a session in the cloud, and for one whose computer bound it to
    the folder itself.  Stamped with the session's device, so a session
    created under a thread names the thread's copy, and its bind carries the
    same (``DeviceOperations.bind``): the computer then never gives the
    thread the folder itself.
    """
    if device_of(config) is None:
        return None
    history = config["execution"].get("history")
    return UUID(history["thread"]) if isinstance(history, dict) else None


def is_binding_root(session_id: UUID, config: dict[str, Any] | None) -> bool:
    """Whether a session holds a folder binding of its own: a root, or a
    project's thread, which is its own sandbox root.  A session created
    under another names that one, and works in its folder."""
    root = (config or {}).get("sandbox_root_session_id")
    return not root or root == str(session_id)


@dataclass(frozen=True, slots=True)
class Binding:
    state: Literal["pending", "bound", "failed"]
    # Why a failed binding failed, as the computer or the server put it.
    message: str | None = None


# Why a thread bound with a copy of its own has nowhere to work: its computer
# did not say it keeps the copy, as an app older than copies binds the folder
# itself and says yes.  A thread never works in the folder itself.
KEEPS_NO_COPY = "This computer keeps no copy of the folder for a thread: update Surogate Desktop"


async def binding_of(db: AsyncSession, root_session_id: UUID) -> Binding:
    """Whether the computer accepted the root session's folder.

    A bind that names the thread's copy is accepted only where the computer
    answers that it keeps that copy, ``{"history": {"thread": <id>}}``.
    """
    # Not filtered by device: a bind row is recorded only after the root was
    # confirmed to name that device, and the server never changes a session's
    # ``execution``, so the root's one bind row is always its device's.
    row = (await db.execute(
        select(DeviceOperation.args, DeviceOperation.outcome).where(
            DeviceOperation.calling_session_id == root_session_id,
            DeviceOperation.invocation_id == BIND,
            DeviceOperation.ordinal == 0,
            DeviceOperation.kind == BIND,
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
        copy = row.args.get("history")
        if copy is not None and outcome["ok"] != {"history": copy}:
            return Binding("failed", KEEPS_NO_COPY)
        return Binding("bound")
    error = outcome.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    return Binding("failed", str(message or "The computer refused this folder"))
