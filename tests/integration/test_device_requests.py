"""Operations asked for outside a tool call: the user's own requests on a chat's files."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

import surogates.devices.operations as operations_module
from surogates.db.models import DeviceOperation
from surogates.devices.operations import (
    REQUEST_PREFIX,
    DeviceOperations,
    OperationConflict,
    OperationRequest,
    TooManyRequests,
    reap_requests,
)
from surogates.devices.workspace import MAX_PAYLOAD_BYTES, DeviceOperationError
from surogates.session.store import SessionStore
from surogates.tools.workspace_io import LocalWorkspaceIO

from .test_devices import (  # noqa: F401  (fixtures)
    _fields,
    _first_op,
    another_bound_root,
    api,
    bound_device,
    complete_pending,
    eventually,
    has_pending,
    laptop_rig,
    link_url,
    linked,
    receive,
    request_for,
    send,
    stop,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def asked(device_id: UUID, root: UUID) -> OperationRequest:
    """A request of the user's on *root*'s folder, as a file route makes one."""
    return OperationRequest(**{**_fields(request_for(device_id, root)), "invocation_id": f"{REQUEST_PREFIX}{uuid.uuid4().hex}"})


async def test_a_paused_chats_folder_takes_requests_but_not_tool_calls(api, session_factory, redis_client, tmp_path):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    await SessionStore(session_factory).update_session_status(root, "paused")
    ops = DeviceOperations(session_factory, redis_client)
    with pytest.raises(DeviceOperationError, match="This session was stopped"):
        await asyncio.wait_for(ops.run(request_for(device_id, root)), 2.0)
    waiting = asyncio.create_task(ops.run(asked(device_id, root)))
    await eventually(lambda: has_pending(ops, device_id))
    assert await complete_pending(ops, device_id, LocalWorkspaceIO(str(tmp_path))) == 1
    assert await asyncio.wait_for(waiting, 2.0) == {"ok": True}


async def test_a_deleted_chats_folder_takes_no_request(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    await SessionStore(session_factory).update_session_status(root, "archived")
    ops = DeviceOperations(session_factory, redis_client)
    with pytest.raises(DeviceOperationError, match="This session was stopped"):
        await asyncio.wait_for(ops.run(asked(UUID(issued["id"]), root)), 2.0)


async def test_a_request_is_not_refused_on_a_full_computer(api, session_factory, redis_client, monkeypatch):
    monkeypatch.setattr(operations_module, "PARKED_SESSIONS_PER_DEVICE", 1)
    issued, first = await bound_device(api)
    device_id = UUID(issued["id"])
    second = await another_bound_root(api, device_id)
    ops = DeviceOperations(session_factory, redis_client)
    parked = asyncio.create_task(ops.run(request_for(device_id, first)))
    await eventually(lambda: has_pending(ops, device_id))
    looking = asyncio.create_task(ops.run(asked(device_id, second)))

    async def recorded() -> bool:
        if looking.done():
            looking.result()  # a refusal surfaces here
        return len(await ops.pending(device_id, 1)) == 2

    await eventually(recorded)
    for task in (parked, looking):
        await stop(task)


async def test_a_user_browsing_files_never_gets_the_agents_tool_call_refused(api, session_factory, redis_client, monkeypatch):
    monkeypatch.setattr(operations_module, "PARKED_SESSIONS_PER_DEVICE", 1)
    issued, first = await bound_device(api)
    device_id = UUID(issued["id"])
    second = await another_bound_root(api, device_id)
    ops = DeviceOperations(session_factory, redis_client)
    # A request parks nothing on a worker, so it takes no tool call's place.
    looking = asyncio.create_task(ops.run(asked(device_id, first), keep_open=True))
    await eventually(lambda: has_pending(ops, device_id))
    calling = asyncio.create_task(ops.run(request_for(device_id, second)))

    async def recorded() -> bool:
        if calling.done():
            calling.result()  # a refusal surfaces here
        return len(await ops.pending(device_id, 1)) == 2

    await eventually(recorded)
    for task in (looking, calling):
        await stop(task)


async def test_a_request_kept_open_outlives_its_wait_and_is_joined_later(api, session_factory, redis_client, tmp_path):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    request = asked(device_id, root)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await ops.run(request, keep_open=True)
    # Still the computer's to answer, as a change waiting for its user's approval is.
    assert [op.invocation_id for op in await ops.pending(device_id, 1)] == [request.invocation_id]
    assert await complete_pending(ops, device_id, LocalWorkspaceIO(str(tmp_path))) == 1
    # The same request again gets what the computer did, and runs nothing twice.
    assert await asyncio.wait_for(ops.run(request, keep_open=True), 2.0) == {"ok": True}


async def test_a_request_not_kept_open_is_cancelled_when_its_wait_ends(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await ops.run(asked(device_id, root))
    assert await ops.pending(device_id, 1) == []


async def test_a_session_may_have_only_so_many_requests_open(api, session_factory, redis_client, monkeypatch):
    monkeypatch.setattr(operations_module, "OPEN_REQUESTS_PER_SESSION", 1)
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    first = asked(device_id, root)
    waiting = asyncio.create_task(ops.run(first, keep_open=True))
    await eventually(lambda: has_pending(ops, device_id))
    with pytest.raises(TooManyRequests, match="Too much of this chat is waiting for Flavius's ThinkPad"):
        await asyncio.wait_for(ops.run(asked(device_id, root)), 2.0)
    # The open one goes on: the same request again, and its next step.
    again = asyncio.create_task(ops.run(first, keep_open=True))
    next_step = asyncio.create_task(ops.run(OperationRequest(**{**_fields(first), "ordinal": 2}), keep_open=True))

    async def both_recorded() -> bool:
        for task in (again, next_step):
            if task.done():
                task.result()  # a refusal surfaces here
        return len(await ops.pending(device_id, 1)) == 2

    await eventually(both_recorded)
    for task in (waiting, again, next_step):
        await stop(task)


async def test_a_burst_of_new_requests_opens_no_more_than_the_cap(api, session_factory, redis_client, monkeypatch):
    monkeypatch.setattr(operations_module, "OPEN_REQUESTS_PER_SESSION", 2)
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    # Uploads, as a message's attachments go all at once: each holds its count open while its data is stored.
    data = os.urandom(4 * 1024 * 1024)
    transfer = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    uploads = [
        OperationRequest(**{
            **_fields(asked(device_id, root)), "kind": "write",
            "args": {"key": f"/folder/{n}.bin", "transfer": transfer}, "payload": data,
        })
        for n in range(10)
    ]
    burst = [asyncio.create_task(ops.run(upload, keep_open=True)) for upload in uploads]

    async def settled() -> bool:
        return sum(task.done() for task in burst) == 8

    await eventually(settled, timeout=10.0)
    assert all(isinstance(task.exception(), TooManyRequests) for task in burst if task.done())
    assert len(await ops.pending(device_id, 1)) == 2
    for task in burst:
        await stop(task)


async def test_pausing_a_chat_leaves_its_users_requests_open_and_deleting_it_cancels_them(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    change = asked(device_id, root)
    waiting = asyncio.create_task(ops.run(change, keep_open=True))
    agents = asyncio.create_task(ops.run(request_for(device_id, root)))

    async def both_open() -> bool:
        return len(await ops.pending(device_id, 1)) == 2

    await eventually(both_open)
    # Pausing stops the agent's work only.
    assert await ops.cancel([root]) == 1
    assert [op.invocation_id for op in await ops.pending(device_id, 1)] == [change.invocation_id]
    # Deleting ends the changes it waits on too.
    assert await ops.cancel([root], bindings=True) == 1
    assert await ops.pending(device_id, 1) == []
    await stop(waiting)
    await asyncio.gather(agents, return_exceptions=True)


async def test_a_changes_claim_is_never_sent_and_refuses_another_change_under_its_id(api, session_factory, redis_client):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    invocation = f"{REQUEST_PREFIX}{uuid.uuid4().hex}"

    def claim(change: str) -> OperationRequest:
        return OperationRequest(
            device_id=device_id, root_session_id=root, calling_session_id=root, invocation_id=invocation,
            ordinal=0, kind="request", args={"change": change},
        )

    await ops.claim(claim("upload one"))
    await ops.claim(claim("upload one"))  # the same change sent again
    with pytest.raises(OperationConflict):
        await ops.claim(claim("upload two"))
    assert await ops.pending(device_id, 1) == []


async def test_a_finished_requests_rows_go_an_hour_after_it_closed(api, session_factory, redis_client, tmp_path):
    issued, root = await bound_device(api)
    device_id = UUID(issued["id"])
    ops = DeviceOperations(session_factory, redis_client)
    old, recent, tool_call = asked(device_id, root), asked(device_id, root), request_for(device_id, root)
    for request in (old, recent, tool_call):
        done = asyncio.create_task(ops.run(request))
        await eventually(lambda: has_pending(ops, device_id))
        assert await complete_pending(ops, device_id, LocalWorkspaceIO(str(tmp_path))) == 1
        await asyncio.wait_for(done, 2.0)
    still_open = asked(device_id, root)
    waiting = asyncio.create_task(ops.run(still_open, keep_open=True))
    await eventually(lambda: has_pending(ops, device_id))
    async with session_factory() as db:
        await db.execute(
            update(DeviceOperation)
            .where(DeviceOperation.invocation_id.in_([old.invocation_id, tool_call.invocation_id]))
            .values(completed_at=func.now() - timedelta(hours=2))
        )
        await db.commit()
    assert await reap_requests(session_factory) >= 1
    async with session_factory() as db:
        left = set((await db.execute(
            select(DeviceOperation.invocation_id)
            .where(DeviceOperation.calling_session_id == root, DeviceOperation.invocation_id != "bind")
        )).scalars())
    # A tool call's rows stay for its replay; a request still open, or closed within the hour, keeps its own.
    assert left == {recent.invocation_id, still_open.invocation_id, tool_call.invocation_id}
    await stop(waiting)


async def _answer(ws) -> dict:
    """The next frame that is not a cancel: a stopped request's cancel may come first."""
    while (frame := await receive(ws))["type"] == "cancel":
        pass
    return frame


async def test_a_reply_to_a_request_reaped_meanwhile_is_acknowledged_and_keeps_the_link_up(
    laptop_rig, link_url, session_factory, caplog,
):
    rig = laptop_rig
    caplog.set_level(logging.INFO, logger="surogates.devices.link")
    async with linked(link_url, rig.token) as (ws, _):
        waiting = asyncio.create_task(rig.ops.run(asked(rig.device_id, rig.root)))
        op = await _first_op(ws)
        # Its caller stopped waiting, and an hour has passed since: its rows are gone.
        await stop(waiting)
        async with session_factory() as db:
            await db.execute(
                update(DeviceOperation).where(DeviceOperation.id == UUID(op["id"]))
                .values(completed_at=func.now() - timedelta(hours=2))
            )
            await db.commit()
        assert await reap_requests(session_factory) >= 1
        # The computer, back after a night away, sends what it holds: a result header, then a result.
        transfer = {"size": MAX_PAYLOAD_BYTES + 1, "sha256": "0" * 64}
        await send(ws, {"type": "op_result", "id": op["id"], "digest": op["digest"], "outcome": {"ok": {"transfer": transfer}}})
        assert await _answer(ws) == {"type": "unwanted", "id": op["id"]}
        await send(ws, {"type": "op_result", "id": op["id"], "digest": op["digest"], "outcome": {"ok": True}})
        assert await _answer(ws) == {"type": "op_ack", "id": op["id"]}
        await send(ws, {"type": "ping"})
        assert await _answer(ws) == {"type": "pong"}
    assert f"operation {op['id']}, which the journal no longer has" in caplog.text
