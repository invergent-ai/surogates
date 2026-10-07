"""Where a session's files are: the one resolver the api and the harness ask.

A cloud session's files are its workspace in object storage, under the prefix
its workspace identity names: its recorded root's, else its own (unlike
``sandbox_session_key``, which falls back to the parent), within its
conversation's boundary for a managed channel.  A session on a folder of the
user's computer reaches its files only through that computer: each call is a
device operation, journaled under one request outside any tool call.  Nothing
reads or writes the cloud workspace of a session on a computer; its storage
fields are kept for ``create_child_session`` alone.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select

from surogates.db.models import Device
from surogates.devices.binding import device_of
from surogates.devices.operations import REQUEST_PREFIX, DeviceOperations, JournalRunner, OperationRequest
from surogates.devices.presence import DevicePresence
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.sandbox.pool import sandbox_session_key
from surogates.session.attachment_ingest import workspace_root_id
from surogates.storage.tenant import boundary_workspace_prefix
from surogates.tools.workspace_io import StorageWorkspaceIO, WorkspaceFiles

logger = logging.getLogger(__name__)

# How long the harness waits for a local folder's computer outside a tool
# call: what it reads or writes there for itself (the folder's project
# context, the turn's files, an artifact promoted from a reply) is best
# effort, and a turn waits on it no longer than this.
HARNESS_WITHIN_S = 10.0


class ComputerAway(Exception):
    """The session's files are on a computer that is offline, or that no longer has local access."""

    def __init__(self, name: str, *, revoked: bool) -> None:
        self.name = name
        self.revoked = revoked
        super().__init__(
            f"Local access to {name} was revoked" if revoked else f"The files are on {name}, which is offline",
        )


def gave_up_level(exc: BaseException) -> int:
    """The level a harness request outside a tool call logs giving up at.

    Info for a computer that is offline, or silent past ``HARNESS_WITHIN_S``:
    that is expected.  Warning for anything else.
    """
    return logging.INFO if isinstance(exc, (ComputerAway, TimeoutError)) else logging.WARNING


@dataclass(frozen=True, slots=True)
class DeviceAccess:
    """What ``surogates.api.session_guards.require_device_access`` returns once it let a caller in.

    It names the session the caller may reach, and whether the check found
    its folder accepted on its computer (a cloud chat has none to wait for).
    :func:`session_files` takes a change only with it, and only bound: a
    change's claim checks no session, binding or caller, so the check must
    come first, and a chat whose folder is not set up has nowhere to change.
    """

    session_id: UUID
    bound: bool


@asynccontextmanager
async def session_files(
    session: Any,
    *,
    storage: Any,
    session_factory: Any,
    redis: Any,
    request_id: str | None = None,
    change: str | None = None,
    access: DeviceAccess | None = None,
) -> AsyncIterator[WorkspaceFiles]:
    """*session*'s files, for one request.

    On a computer, its calls are one request, ``request:<request_id>`` (a fresh
    one when None), and each waits for the computer as long as its caller lets
    it.  A read its caller stops waiting for is cancelled.  A *change* names
    what it does, as a digest: an operation of it its caller stopped waiting for
    stays open on the computer, for the same request to join again, and its
    ordinal 0 records the digest, so the same request id sent with another
    change is refused (``OperationConflict``).  A change comes with the
    *access* ``require_device_access`` gave its caller to this session, or it
    raises RuntimeError before anything is recorded.  Once the request ends,
    what it read in transfers is marked consumed, for the transfer reaper.
    Raises :class:`ComputerAway` at once when the computer is offline or its
    access ended: a change it left open is then cancelled, since its caller is
    told it failed.  A read under the same id cancels nothing.
    """
    if change is not None and (access is None or access.session_id != session.id or not access.bound):
        # A route that forgot the check would claim a change for anyone in the org, or on a folder not set up.
        raise RuntimeError("A change reaches a session's files only once require_device_access let its caller in")
    device_id = device_of(session.config)
    if device_id is None:
        yield StorageWorkspaceIO(
            storage,
            bucket=session.config["storage_bucket"],
            prefix=boundary_workspace_prefix(session.config, session, workspace_root_id(session)),
        )
        return
    invocation = f"{REQUEST_PREFIX}{request_id or uuid.uuid4().hex}"
    operations = DeviceOperations(session_factory, redis)
    async with session_factory() as db:
        device = (await db.execute(
            select(Device.name, Device.revoked_at).where(Device.id == device_id)
        )).one_or_none()
    if device is None or device.revoked_at is not None:
        raise ComputerAway(device.name if device is not None else "your computer", revoked=True)
    root = UUID(sandbox_session_key(session))
    if change is not None:
        # Before the computer is asked after: the same request id sent with another change is
        # refused, online or not, and cancels nothing of the first.
        await operations.claim(OperationRequest(
            device_id=device_id,
            root_session_id=root,
            calling_session_id=session.id,
            invocation_id=invocation,
            ordinal=0,
            kind="request",
            args={"change": change},
        ))
    if device_id not in await DevicePresence(redis).online([device_id]):
        # A change's own caller, told it failed, cancels what it left waiting; a read leaves nothing open.
        if change is not None:
            # The app answers what it still asks about not run, once its link ends.  The one
            # exception: a change its user allowed just before is running, and may still land.
            await operations.cancel_invocation(session.id, invocation)
        raise ComputerAway(device.name, revoked=False)
    runner = JournalRunner(
        operations,
        device_id=device_id,
        root_session_id=root,
        calling_session_id=session.id,
        invocation_id=invocation,
        keep_open=change is not None,
    )
    try:
        yield DeviceWorkspaceIO(runner, root=session.config["workspace_path"], identity=f"device:{device_id}")
    finally:
        try:
            await runner.consumed()
        except Exception:
            # Best effort: a read left unmarked goes as an orphan instead.
            logger.warning("could not mark what a request read as consumed", exc_info=True)
