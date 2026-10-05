"""A write's data too large for one frame: kept with its operation, and sent to the computer in chunks."""

from __future__ import annotations

import asyncio
import hashlib
import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Insert

from surogates.db.models import DeviceOperation, DeviceTransfer
from surogates.devices.operations import DeviceOperations, OperationConflict, OperationRequest
from surogates.devices.store import REVOKED_OUTCOME
from surogates.devices.workspace import CHUNK_BYTES

from .test_device_transfers import stored, transfers_of
from .test_devices import (  # noqa: F401  (fixtures)
    _fields,
    api,
    eventually,
    laptop_rig,
    link_url,
    request_for,
    stop,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

DATA = os.urandom(CHUNK_BYTES * 5 // 2)  # two chunks and a half


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
