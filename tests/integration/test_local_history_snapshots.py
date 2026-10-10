"""Every step of a project's thread bound with a copy of its own starts from a snapshot of the copy, and a Stop puts the copy back.

Each scene runs a worker's real wake on the tests' computer, whose folder's
history is the real one: the turn's open, its steps, a Stop by the pause route
as a person stops a thread, and the undo that follows.  Only the model is a
script.  Around each, the user's folder is read entry by entry, and the copy
by its names, modes, bytes and, for a file the turn did not rewrite, its time.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from surogates.devices.workspace import DeviceOperationError

from .test_devices import api, link_url  # noqa: F401  (api and link_url are fixtures)
from .test_local_history_threads import asked as request_of
from .test_local_history_threads import bound_with_copy, recorded
from .test_local_threads import journal

pytestmark = pytest.mark.asyncio(loop_scope="session")


# -- the journal --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["paused", "failed"])
async def test_a_stopped_threads_snapshot_is_refused_as_new_work_and_its_undo_still_reaches_its_computer(api, status):
    device, _, thread = await bound_with_copy(api)
    await api.app.state.session_store.update_session_status(thread.id, status)
    # A snapshot is a step's, and no step of a stopped turn starts: none is recorded, so none waits on after the Stop.
    with pytest.raises(DeviceOperationError, match="This session was stopped"):
        await journal(api)._record(request_of(device, thread, "checkpoint", "take", invocation="checkpoint:0:0:call_1"))
    assert await journal(api).pending(UUID(device["id"]), 1) == []
    # Its put-back is the Stop's own.
    await recorded(api, request_of(device, thread, "checkpoint", "restore", invocation=f"checkpoint:0:restore:{'a' * 40}"))
