"""A write's data too large for one frame: kept with its operation, and sent to the computer in chunks."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Insert

from surogates.db.models import DeviceOperation, DeviceTransfer
from surogates.devices.link import TRANSFER_WINDOW
from surogates.devices.operations import CANCELLED_OUTCOME, DeviceOperations, OperationConflict, OperationRequest
from surogates.devices.store import REVOKED_OUTCOME
from surogates.devices.workspace import CHUNK_BYTES

from .test_device_transfers import stored, transfers_of
from .test_devices import (  # noqa: F401  (fixtures)
    _fields,
    _first_op,
    api,
    eventually,
    laptop_rig,
    link_url,
    linked,
    receive,
    request_for,
    send,
    stop,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

DATA = os.urandom(CHUNK_BYTES * 5 // 2)  # two chunks and a half
BIG = os.urandom(CHUNK_BYTES * 6 + 5)  # seven chunks, the last one short


def named(data: bytes) -> dict:
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def write_request(rig, data: bytes = DATA, *, like: OperationRequest | None = None) -> OperationRequest:
    """A write of *data* to big.bin; with *like*, under that request's invocation and ordinal."""
    fields = _fields(like or request_for(rig.device_id, rig.root))
    return OperationRequest(**{
        **fields,
        "kind": "write",
        "args": {"key": f"{rig.folder}/big.bin", "transfer": named(data)},
        "payload": data,
    })


async def open_count(rig, count: int) -> bool:
    return len(await rig.ops.pending(rig.device_id, 1)) == count


async def test_a_write_too_large_for_a_frame_is_kept_with_its_operation(laptop_rig, session_factory):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(write_request(rig)))
    await eventually(lambda: open_count(rig, 1))
    [op] = await rig.ops.pending(rig.device_id, 1)
    # The args name the data by its content; the data is beside them, whole.
    assert op.args == {"key": f"{rig.folder}/big.bin", "transfer": named(DATA)}
    assert await stored(session_factory, str(op.id)) == DATA
    async with session_factory() as db:
        transfer = (await db.execute(
            select(DeviceTransfer).where(DeviceTransfer.operation_id == op.id)
        )).scalar_one()
    assert (transfer.size, transfer.received, transfer.sha256) == (len(DATA), len(DATA), named(DATA)["sha256"])
    await stop(waiting)


async def test_a_write_whose_data_cannot_be_kept_is_not_recorded(laptop_rig, engine, session_factory, redis_client):
    rig = laptop_rig

    class Failing(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if isinstance(statement, Insert) and statement.table.name == "device_transfer_chunks":
                raise RuntimeError("the disk is full")
            return await super().execute(statement, *args, **kwargs)

    ops = DeviceOperations(async_sessionmaker(engine, class_=Failing, expire_on_commit=False), redis_client)
    request = write_request(rig)
    with pytest.raises(RuntimeError, match="the disk is full"):
        await ops.run(request)
    # The operation and its data commit together: no operation is left for the link to send without them.
    async with session_factory() as db:
        assert (await db.execute(
            select(func.count()).select_from(DeviceOperation)
            .where(DeviceOperation.invocation_id == request.invocation_id)
        )).scalar_one() == 0
    assert await transfers_of(session_factory, rig.device_id) == 0


async def test_a_resumed_write_asks_for_the_same_operation_and_its_data_is_kept_once(laptop_rig, session_factory):
    rig = laptop_rig
    first = write_request(rig)
    waiting = asyncio.create_task(rig.ops.run(first))
    await eventually(lambda: open_count(rig, 1))
    # A worker resuming the call makes the same bytes again: the same digest, so it joins.
    again = asyncio.create_task(rig.ops.run(write_request(rig, bytes(DATA), like=first)))
    await asyncio.sleep(0.2)
    assert not again.done()
    assert await transfers_of(session_factory, rig.device_id) == 1
    [op] = await rig.ops.pending(rig.device_id, 1)
    assert await stored(session_factory, str(op.id)) == DATA
    # Other bytes under the same call and ordinal are another request.
    with pytest.raises(OperationConflict):
        await rig.ops.run(write_request(rig, os.urandom(len(DATA)), like=first))
    await stop(again)
    await stop(waiting)


async def test_a_write_for_a_revoked_device_is_answered_so_and_keeps_no_data(laptop_rig, api, session_factory):
    rig = laptop_rig
    response = await api.client.delete(f"/v1/devices/{rig.device_id}", headers=api.auth())
    assert response.status_code == 204
    assert await asyncio.wait_for(rig.ops.run(write_request(rig)), 5.0) == REVOKED_OUTCOME
    # Recorded as answered: its data would never be sent.
    assert await transfers_of(session_factory, rig.device_id) == 0


def joined(frames: list[dict]) -> bytes:
    return b"".join(base64.b64decode(frame["data"], validate=True) for frame in frames)


async def answer(ws, op: dict) -> dict:
    """Answer a write as the app does once its data is whole and written."""
    await send(ws, {"type": "op_result", "id": op["id"], "digest": op["digest"], "outcome": {"ok": None}})
    return await receive(ws)


async def test_a_writes_data_comes_after_its_op_at_most_four_chunks_ahead_of_the_acknowledgements(
    laptop_rig, link_url,
):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(write_request(rig, BIG)))
    async with linked(link_url, rig.token) as (ws, _):
        op = await _first_op(ws)
        assert op["args"] == {"key": f"{rig.folder}/big.bin", "transfer": named(BIG)}
        frames = [await receive(ws) for _ in range(TRANSFER_WINDOW)]
        assert [(f["type"], f["id"], f["seq"]) for f in frames] == [("chunk", op["id"], seq) for seq in range(4)]
        # Nothing more until the app acknowledges, and a ping is answered meanwhile.
        await send(ws, {"type": "ping"})
        assert await receive(ws) == {"type": "pong"}
        await send(ws, {"type": "chunk_ack", "id": op["id"], "seq": 3})
        frames += [await receive(ws) for _ in range(3)]
        assert [f["seq"] for f in frames] == list(range(7))
        assert joined(frames) == BIG
        await send(ws, {"type": "chunk_ack", "id": op["id"], "seq": 6})
        assert await answer(ws, op) == {"type": "op_ack", "id": op["id"]}
    assert await asyncio.wait_for(waiting, 5.0) == {"ok": None}


async def test_a_connection_that_ends_mid_transfer_sends_the_write_and_its_data_again_from_chunk_0(
    laptop_rig, link_url,
):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(write_request(rig)))
    async with linked(link_url, rig.token) as (ws, _):
        op = await _first_op(ws)
        assert (await receive(ws))["seq"] == 0
    async with linked(link_url, rig.token) as (ws, _):
        again = await _first_op(ws)
        assert again["id"] == op["id"]
        frames = [await receive(ws) for _ in range(3)]
        assert [f["seq"] for f in frames] == [0, 1, 2]
        assert joined(frames) == DATA
        assert await answer(ws, again) == {"type": "op_ack", "id": op["id"]}
    assert await asyncio.wait_for(waiting, 5.0) == {"ok": None}


async def test_a_write_stopped_mid_transfer_sends_no_more_of_its_data(laptop_rig, link_url):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(write_request(rig, BIG)))
    async with linked(link_url, rig.token) as (ws, _):
        op = await _first_op(ws)
        assert [(await receive(ws))["seq"] for _ in range(TRANSFER_WINDOW)] == [0, 1, 2, 3]
        await rig.ops.cancel([rig.root])
        assert await receive(ws) == {"type": "cancel", "id": op["id"]}
        await send(ws, {"type": "chunk_ack", "id": op["id"], "seq": 3})
        await send(ws, {"type": "ping"})
        # No chunk before the pong: the transfer stopped.
        assert await receive(ws) == {"type": "pong"}
    assert await asyncio.wait_for(waiting, 5.0) == CANCELLED_OUTCOME


async def test_a_writes_data_goes_only_to_its_device_under_its_current_credentials(laptop_rig, api):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(write_request(rig)))
    await eventually(lambda: open_count(rig, 1))
    [op] = await rig.ops.pending(rig.device_id, 1)
    assert await rig.ops.outgoing_chunk(rig.device_id, 1, op.id, 0) == DATA[:CHUNK_BYTES]
    assert await rig.ops.outgoing_chunk(uuid.uuid4(), 1, op.id, 0) is None
    # Rotated: a connection still open under the old credentials gets no more of it.
    response = await api.client.post(f"/v1/devices/{rig.device_id}/reauthorize", headers=api.auth())
    assert response.status_code == 200, response.text
    assert await rig.ops.outgoing_chunk(rig.device_id, 1, op.id, 1) is None
    assert await rig.ops.outgoing_chunk(rig.device_id, 2, op.id, 1) == DATA[CHUNK_BYTES:2 * CHUNK_BYTES]
    await stop(waiting)
