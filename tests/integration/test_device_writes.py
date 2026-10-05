"""A write's data too large for one frame: kept with its operation, and sent to the computer in chunks."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import tracemalloc
import uuid
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Insert

from surogates.db.models import Device, DeviceOperation, DeviceTransfer, DeviceTransferChunk
from surogates.devices.link import TRANSFER_WINDOW
from surogates.devices.operations import (
    CANCELLED_OUTCOME,
    DeviceOperations,
    OperationConflict,
    OperationRequest,
    _keep_payload,
    reap_transfers,
)
from surogates.devices.store import REVOKED_OUTCOME
from surogates.devices.workspace import CHUNK_BYTES
from surogates.session.store import SessionStore
from surogates.tools.builtin import file_ops
from surogates.tools.registry import ToolSchema

from .test_device_transfers import StoppingStore, big_text, stored, transfers_of
from .test_devices import (  # noqa: F401  (fixtures)
    _fields,
    _first_op,
    api,
    builtin_tools,
    device_io,
    eventually,
    laptop_rig,
    link_url,
    linked,
    receive,
    request_for,
    resume_call,
    send,
    stop,
    take_over,
    tool_call,
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
    # No connection sends it in.
    assert transfer.holder == ""
    # The chunks the link sends as they are: seq from 0, every one CHUNK_BYTES but the last.
    async with session_factory() as db:
        layout = (await db.execute(
            select(DeviceTransferChunk.seq, func.length(DeviceTransferChunk.data))
            .where(DeviceTransferChunk.operation_id == op.id).order_by(DeviceTransferChunk.seq)
        )).all()
    assert [tuple(row) for row in layout] == [(0, CHUNK_BYTES), (1, CHUNK_BYTES), (2, CHUNK_BYTES // 2)]
    await stop(waiting)


async def test_a_writes_data_is_kept_without_a_second_copy_of_it():
    """Its chunks are views of the payload: recording 50 MiB holds no second 50 MiB in the worker."""
    rows: list[dict] = []

    class Recording:
        async def execute(self, statement, parameters=None):
            rows.extend(parameters or [])

    transfer = named(BIG)
    tracemalloc.start()
    try:
        await _keep_payload(Recording(), uuid.uuid4(), transfer, BIG)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert b"".join(row["data"] for row in rows) == BIG
    assert peak < CHUNK_BYTES, peak


@pytest.mark.parametrize("which", ["no data", "no transfer", "another size", "a null transfer"])
async def test_a_write_whose_data_and_transfer_do_not_come_together_is_refused_before_it_is_recorded(
    laptop_rig, session_factory, which,
):
    rig = laptop_rig
    request = write_request(rig)
    if which == "no data":
        # The link would send its op, and the computer wait for data that never comes.
        request = replace(request, payload=None)
    elif which == "no transfer":
        request = replace(request, args={"key": request.args["key"]})
    elif which == "a null transfer":
        # Named all the same: the link would queue it for data that is not there.
        request = replace(request, args={"key": request.args["key"], "transfer": None}, payload=None)
    else:
        request = replace(request, payload=DATA[:-1])
    with pytest.raises(ValueError, match="come together"):
        await asyncio.wait_for(rig.ops.run(request), 5.0)
    async with session_factory() as db:
        assert (await db.execute(
            select(func.count()).select_from(DeviceOperation)
            .where(DeviceOperation.invocation_id == request.invocation_id)
        )).scalar_one() == 0
    assert await transfers_of(session_factory, rig.device_id) == 0


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
        # Bounded: were the data dropped, the write would be recorded and wait for its computer.
        await asyncio.wait_for(ops.run(request), 5.0)
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
    # A worker resuming the call makes the same bytes again, a copy of its own: the same digest, so it joins.
    again = asyncio.create_task(rig.ops.run(write_request(rig, bytes(bytearray(DATA)), like=first)))
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


async def test_a_writes_data_goes_no_more_once_its_device_is_revoked_or_the_write_closed(laptop_rig, session_factory):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(write_request(rig)))
    await eventually(lambda: open_count(rig, 1))
    [op] = await rig.ops.pending(rig.device_id, 1)
    # Revoked in the row alone, the write left open: a revocation also closes it, which is the next check.
    for revoked_at, expected in [(func.now(), None), (None, DATA[:CHUNK_BYTES])]:
        async with session_factory() as db:
            await db.execute(update(Device).where(Device.id == rig.device_id).values(revoked_at=revoked_at))
            await db.commit()
        assert await rig.ops.outgoing_chunk(rig.device_id, 1, op.id, 0) == expected
    await rig.ops.cancel([rig.root])
    assert await rig.ops.outgoing_chunk(rig.device_id, 1, op.id, 0) is None
    await stop(waiting)


async def test_the_reference_laptop_takes_a_write_too_large_for_a_frame_in_chunks(laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    wio = device_io(rig.ops, rig.device_id, rig.root, rig.folder)
    await asyncio.wait_for(wio.write(str(rig.folder / "big.bin"), BIG), 10.0)
    assert (rig.folder / "big.bin").read_bytes() == BIG
    [operation_id] = rig.laptop.outcomes
    assert rig.laptop.chunks_received == [(operation_id, seq) for seq in range(7)]


async def test_a_large_read_and_large_writes_go_at_once_on_one_connection(laptop_rig):
    rig = laptop_rig
    up, most = os.urandom(CHUNK_BYTES * 3 + 11), os.urandom(CHUNK_BYTES * 20)
    (rig.folder / "up.bin").write_bytes(up)
    await rig.laptop.connect()
    reading, *writing = [device_io(rig.ops, rig.device_id, rig.root, rig.folder) for _ in range(3)]
    first = asyncio.create_task(writing[0].write(str(rig.folder / "a.bin"), most))

    async def under_way() -> None:
        while not rig.laptop.chunks_received:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(under_way(), 10.0)
    # The read's data goes up while the first write's 20 chunks come down, and a second write waits behind them.
    read, *_ = await asyncio.wait_for(asyncio.gather(
        reading.read(str(rig.folder / "up.bin")),
        first,
        writing[1].write(str(rig.folder / "b.bin"), BIG),
    ), 15.0)
    assert read == up
    assert (rig.folder / "a.bin").read_bytes() == most
    assert (rig.folder / "b.bin").read_bytes() == BIG
    # One connection throughout: the laptop never reconnects by itself.
    assert rig.laptop.connected


async def test_write_file_and_research_notes_over_1_mib_land_on_the_computer(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    store, tools = SessionStore(session_factory), builtin_tools()
    io_ = {"redis_client": redis_client, "session_factory": session_factory}
    content = big_text()
    written = await tool_call(rig, store, tools, "call_1", "write_file", {"path": "app.log", "content": content}, **io_)
    assert json.loads(written["content"])["status"] == "ok", written
    assert (rig.folder / "app.log").read_text() == content
    for n in range(4):
        added = await tool_call(rig, store, tools, f"call_{n + 2}", "research_memory", {
            "action": "add", "url": f"https://example.org/{n}", "title": f"T{n}", "summary": "s" * 400_000,
        }, **io_)
        assert json.loads(added["content"])["success"] is True, added
    assert (rig.folder / ".research" / "memory.jsonl").stat().st_size > 1024 * 1024


def big_file(folder) -> tuple[dict, str]:
    """A 5 MiB file in *folder*, and the patch that changes its last line but one, with what it makes."""
    lines = [f"row {n:07d} of a large file\n" for n in range(200_000)]
    (folder / "big.txt").write_text("".join(lines))
    lines[199_998] = "ROW 0199998 OF a large file\n"
    args = {"mode": "replace", "path": "big.txt", "old_string": "row 0199998 of", "new_string": "ROW 0199998 OF"}
    return args, "".join(lines)


async def test_a_patch_on_a_5_mib_file_lands_on_the_computer(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    args, patched = big_file(rig.folder)
    result = await tool_call(
        rig, SessionStore(session_factory), builtin_tools(), "call_1", "patch", args,
        redis_client=redis_client, session_factory=session_factory,
    )
    assert json.loads(result["content"])["status"] == "ok", result
    assert (rig.folder / "big.txt").read_text() == patched


async def test_a_worker_stopped_after_the_computer_wrote_a_5_mib_patch_resumes_without_writing_again(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    await rig.laptop.connect()
    args, patched = big_file(rig.folder)
    store, tools = SessionStore(session_factory), builtin_tools()
    io_ = {"redis_client": redis_client, "session_factory": session_factory}
    with pytest.raises(asyncio.CancelledError):
        await tool_call(rig, StoppingStore(session_factory), tools, "call_1", "patch", args, **io_)
    assert rig.laptop.ran.count("write") == 1 and (rig.folder / "big.txt").read_text() == patched
    ran, received = len(rig.laptop.ran), len(rig.laptop.chunks_received)
    file_ops._read_tracker.clear()
    await take_over(store, rig)

    resumed = await resume_call(rig, store, tools, "call_1", "patch", args, **io_)

    assert json.loads(resumed["content"])["status"] == "ok", resumed
    # The read came back from the journal, the write's recorded outcome too: the computer did nothing again.
    assert (len(rig.laptop.ran), len(rig.laptop.chunks_received)) == (ran, received)
    assert (rig.folder / "big.txt").read_text() == patched


def reporting(report: str):
    """The built-in tools and big_report, a tool whose result is *report*, as a cloud tool's can be."""
    tools = builtin_tools()
    tools.register(
        "big_report",
        ToolSchema(name="big_report", description="a report", parameters={"type": "object", "properties": {}}),
        handler=AsyncMock(return_value=report),
        max_result_size=4 * 1024 * 1024,
    )
    return tools


async def test_a_3_mib_tool_result_spills_onto_the_computer(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    report = big_text(70_000)
    assert len(report) > 3 * 1024 * 1024
    result = await tool_call(
        rig, SessionStore(session_factory), reporting(report), "call_1", "big_report", {},
        redis_client=redis_client, session_factory=session_factory,
    )
    assert "Full output saved to: .surogates-results/call_1.txt" in result["content"]
    assert (rig.folder / ".surogates-results" / "call_1.txt").read_text() == report


async def test_the_reaper_deletes_a_writes_data_once_it_is_closed_and_never_while_it_is_open(laptop_rig, session_factory):
    rig = laptop_rig
    async with session_factory() as db:
        await db.execute(delete(DeviceTransfer))
        await db.commit()
    stopped = asyncio.create_task(rig.ops.run(write_request(rig)))
    await eventually(lambda: open_count(rig, 1))
    [closed] = await rig.ops.pending(rig.device_id, 1)
    await rig.ops.cancel([rig.root])
    assert await asyncio.wait_for(stopped, 5.0) == CANCELLED_OUTCOME
    waiting = asyncio.create_task(rig.ops.run(write_request(rig)))
    await eventually(lambda: open_count(rig, 1))
    [still_open] = await rig.ops.pending(rig.device_id, 1)
    # Its computer away for a week, and marked consumed by a call that gave up on it: a read's rules would take it.
    async with session_factory() as db:
        await db.execute(update(DeviceTransfer).where(DeviceTransfer.operation_id == still_open.id).values(
            created_at=func.now() - timedelta(days=8), consumed_at=func.now() - timedelta(hours=25),
        ))
        await db.commit()

    assert await reap_transfers(session_factory) == 1

    async with session_factory() as db:
        assert set((await db.execute(select(DeviceTransfer.operation_id))).scalars()) == {still_open.id}
    assert await stored(session_factory, str(closed.id)) == b""
    assert await stored(session_factory, str(still_open.id)) == DATA
    await stop(waiting)


async def test_the_reaper_keeps_an_open_writes_data_while_its_computer_is_away_a_week(laptop_rig, session_factory):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(write_request(rig)))
    await eventually(lambda: open_count(rig, 1))
    [op] = await rig.ops.pending(rig.device_id, 1)
    # Nothing marks a waiting write's data consumed, so a read's orphan rule would take it.
    async with session_factory() as db:
        consumed = (await db.execute(
            update(DeviceTransfer).where(DeviceTransfer.operation_id == op.id)
            .values(created_at=func.now() - timedelta(days=8))
            .returning(DeviceTransfer.consumed_at)
        )).scalar_one()
        await db.commit()
    assert consumed is None

    await reap_transfers(session_factory)

    assert await stored(session_factory, str(op.id)) == DATA
    await stop(waiting)
