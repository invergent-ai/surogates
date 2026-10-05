"""A read's result too large for one frame: its chunks over the real link, kept in Postgres."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
import re
from datetime import timedelta
from uuid import UUID

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Delete, Insert, Update

from surogates.db.models import DeviceOperation, DeviceTransfer, DeviceTransferChunk
from surogates.devices.link import DAMAGED_OUTCOME, TRANSFER_WINDOW
from surogates.devices.operations import CANCELLED_OUTCOME, DeviceOperations, OperationRequest, reap_transfers
from surogates.devices.presence import presence_key
from surogates.devices.sandbox import INTERRUPTED
from surogates.devices.workspace import CHUNK_BYTES
from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.tools.builtin import file_ops

from .test_devices import (  # noqa: F401  (fixtures)
    _fields,
    _first_op,
    api,
    bound_device,
    builtin_tools,
    close_code,
    device_io,
    eventually,
    forget_result,
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


def read_request(rig) -> OperationRequest:
    return OperationRequest(**{
        **_fields(request_for(rig.device_id, rig.root)),
        "kind": "read",
        "args": {"key": f"{rig.folder}/big.bin", "max_bytes": None},
    })


def named(data: bytes, sha256: str | None = None) -> dict:
    return {"ok": {"transfer": {"size": len(data), "sha256": sha256 or hashlib.sha256(data).hexdigest()}}}


def header(op: dict, data: bytes, sha256: str | None = None) -> dict:
    return {"type": "op_result", "id": op["id"], "digest": op["digest"], "outcome": named(data, sha256)}


def chunk(op: dict, data: bytes, seq: int) -> dict:
    piece = data[seq * CHUNK_BYTES:(seq + 1) * CHUNK_BYTES]
    return {"type": "chunk", "id": op["id"], "seq": seq, "data": base64.b64encode(piece).decode("ascii")}


async def send_transfer(ws, op: dict, data: bytes, sha256: str | None = None) -> list[dict]:
    """Send a result header and its chunks as the app does, keeping TRANSFER_WINDOW ahead; returns what came back."""
    await send(ws, header(op, data, sha256))
    count = -(-len(data) // CHUNK_BYTES)
    answers: list[dict] = []
    sent = acked = 0
    while not answers or answers[-1]["type"] not in ("op_ack", "unwanted"):
        while sent < count and sent - acked < TRANSFER_WINDOW:
            await send(ws, chunk(op, data, sent))
            sent += 1
        answers.append(await receive(ws))
        if answers[-1]["type"] == "chunk_ack":
            acked = answers[-1]["seq"] + 1
    return answers


async def stored(session_factory, operation_id: str) -> bytes:
    async with session_factory() as db:
        rows = (await db.execute(
            select(DeviceTransferChunk.data)
            .where(DeviceTransferChunk.operation_id == UUID(operation_id))
            .order_by(DeviceTransferChunk.seq)
        )).scalars().all()
    return b"".join(rows)


async def transfers_of(session_factory, device_id: UUID) -> int:
    async with session_factory() as db:
        return (await db.execute(
            select(func.count()).select_from(DeviceTransfer)
            .join(DeviceOperation, DeviceOperation.id == DeviceTransfer.operation_id)
            .where(DeviceOperation.device_id == device_id)
        )).scalar_one()


async def test_a_read_too_large_for_a_frame_arrives_in_chunks_and_completes_its_operation(
    laptop_rig, link_url, session_factory,
):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(read_request(rig)))
    async with linked(link_url, rig.token) as (ws, _):
        op = await _first_op(ws)
        answers = await send_transfer(ws, op, DATA)
    assert answers == [
        {"type": "chunk_ack", "id": op["id"], "seq": 0},
        {"type": "chunk_ack", "id": op["id"], "seq": 1},
        {"type": "op_ack", "id": op["id"]},
    ]
    # The outcome names the transfer; the data is in Postgres, whole.
    assert await asyncio.wait_for(waiting, 5.0) == named(DATA)
    assert await stored(session_factory, op["id"]) == DATA


async def test_a_header_for_a_stopped_operation_is_unwanted_and_the_link_goes_on(
    laptop_rig, link_url, session_factory,
):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(read_request(rig)))
    async with linked(link_url, rig.token) as (ws, _):
        op = await _first_op(ws)
        await rig.ops.cancel([rig.root])
        assert await asyncio.wait_for(waiting, 5.0) == CANCELLED_OUTCOME
        await send(ws, header(op, DATA))
        # Sent before the app heard: dropped, not refused.
        await send(ws, chunk(op, DATA, 0))
        answers = [await receive(ws)]
        while answers[-1]["type"] != "unwanted":
            answers.append(await receive(ws))
        await send(ws, {"type": "ping"})
        assert await receive(ws) == {"type": "pong"}
    assert {"type": "unwanted", "id": op["id"]} in answers
    assert await transfers_of(session_factory, rig.device_id) == 0


async def test_an_operation_stopped_mid_transfer_makes_its_next_chunk_unwanted(laptop_rig, link_url):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(read_request(rig)))
    async with linked(link_url, rig.token) as (ws, _):
        op = await _first_op(ws)
        await send(ws, header(op, DATA))
        await send(ws, chunk(op, DATA, 0))
        assert await receive(ws) == {"type": "chunk_ack", "id": op["id"], "seq": 0}
        await rig.ops.cancel([rig.root])
        await send(ws, chunk(op, DATA, 1))
        answers = [await receive(ws)]
        while answers[-1]["type"] != "unwanted":
            answers.append(await receive(ws))
    assert await asyncio.wait_for(waiting, 5.0) == CANCELLED_OUTCOME


async def test_data_that_does_not_match_its_sha256_is_recorded_as_an_error_and_acknowledged(
    laptop_rig, link_url, session_factory,
):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(read_request(rig)))
    async with linked(link_url, rig.token) as (ws, _):
        op = await _first_op(ws)
        answers = await send_transfer(ws, op, DATA, sha256="0" * 64)
    assert answers[-1] == {"type": "op_ack", "id": op["id"]}
    assert await asyncio.wait_for(waiting, 5.0) == DAMAGED_OUTCOME
    # Bytes known to be wrong are not kept.
    assert await stored(session_factory, op["id"]) == b""
    assert await transfers_of(session_factory, rig.device_id) == 0


async def test_a_transfer_the_link_closes_on_is_logged_with_the_close(laptop_rig, link_url, caplog):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(read_request(rig)))
    with caplog.at_level(logging.INFO, logger="surogates.devices.link"):
        async with linked(link_url, rig.token) as (ws, _):
            op = await _first_op(ws)
            await send(ws, header(op, DATA))
            await send(ws, chunk(op, DATA, 1))
            assert await close_code(ws) == 4400
    [line] = [
        record.getMessage() for record in caplog.records
        if record.name == "surogates.devices.link" and " transfer " in record.getMessage()
    ]
    assert re.fullmatch(
        rf"device {rig.device_id} transfer {op['id']} closed 4400 \(chunk out of order\): {len(DATA)} bytes, "
        r"\d+\.\d\d s from its header",
        line,
    ), line
    await stop(waiting)


async def test_a_transfer_cut_off_is_sent_again_whole_on_the_next_connection(
    laptop_rig, link_url, session_factory,
):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(read_request(rig)))
    async with linked(link_url, rig.token) as (ws, _):
        op = await _first_op(ws)
        await send(ws, header(op, DATA))
        await send(ws, chunk(op, DATA, 0))
        assert await receive(ws) == {"type": "chunk_ack", "id": op["id"], "seq": 0}
    async with linked(link_url, rig.token) as (ws, _):
        again = await _first_op(ws)
        assert again["id"] == op["id"]
        answers = await send_transfer(ws, again, DATA)
    assert answers[-1] == {"type": "op_ack", "id": op["id"]}
    assert await asyncio.wait_for(waiting, 5.0) == named(DATA)
    assert await stored(session_factory, op["id"]) == DATA


async def test_a_header_after_the_presence_key_expired_is_taken_on_the_live_connection(
    laptop_rig, link_url, redis_client,
):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(read_request(rig)))
    async with linked(link_url, rig.token) as (ws, _):
        op = await _first_op(ws)
        # A Redis restart or an eviction: no other connection holds the device.
        await redis_client.delete(presence_key(rig.device_id))
        await send(ws, header(op, DATA))
        await send(ws, chunk(op, DATA, 0))
        assert await receive(ws) == {"type": "chunk_ack", "id": op["id"], "seq": 0}
        assert await redis_client.exists(presence_key(rig.device_id))
    await stop(waiting)


async def test_a_device_keeps_one_transfer_half_sent(laptop_rig, session_factory):
    rig = laptop_rig
    first = asyncio.create_task(rig.ops.run(read_request(rig)))
    second = asyncio.create_task(rig.ops.run(read_request(rig)))
    await eventually(lambda: _pending_count(rig, 2))
    a, b = await rig.ops.pending(rig.device_id, 1)
    sha = hashlib.sha256(DATA).hexdigest()
    assert await rig.ops.start_transfer(rig.device_id, 1, "conn-1", a.id, a.digest, len(DATA), sha) == "started"
    assert await rig.ops.store_chunk(
        rig.device_id, 1, "conn-1", a.id, a.digest, 0, DATA[:CHUNK_BYTES], None,
    ) == "stored"
    # A later connection starts another: what the first left half-sent goes.
    assert await rig.ops.start_transfer(rig.device_id, 1, "conn-2", b.id, b.digest, len(DATA), sha) == "started"
    assert await transfers_of(session_factory, rig.device_id) == 1
    assert await rig.ops.store_chunk(
        rig.device_id, 1, "conn-1", a.id, a.digest, 1, DATA[CHUNK_BYTES:2 * CHUNK_BYTES], None,
    ) == "lost"
    assert await stored(session_factory, str(a.id)) == b""
    await stop(first)
    await stop(second)


async def _pending_count(rig, count: int) -> bool:
    return len(await rig.ops.pending(rig.device_id, 1)) == count


async def test_a_chunk_under_rotated_credentials_is_stale_and_stores_nothing(laptop_rig, api, session_factory):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(read_request(rig)))
    await eventually(lambda: _pending_count(rig, 1))
    [op] = await rig.ops.pending(rig.device_id, 1)
    sha = hashlib.sha256(DATA).hexdigest()
    assert await rig.ops.start_transfer(rig.device_id, 1, "conn", op.id, op.digest, len(DATA), sha) == "started"
    response = await api.client.post(f"/v1/devices/{rig.device_id}/reauthorize", headers=api.auth())
    assert response.status_code == 200, response.text
    assert await rig.ops.store_chunk(
        rig.device_id, 1, "conn", op.id, op.digest, 0, DATA[:CHUNK_BYTES], None,
    ) == "stale"
    assert await stored(session_factory, str(op.id)) == b""
    await stop(waiting)


async def test_a_transfer_for_an_operation_that_is_not_a_read_is_a_protocol_error(laptop_rig, link_url):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(request_for(rig.device_id, rig.root)))
    async with linked(link_url, rig.token) as (ws, _):
        op = await _first_op(ws)
        await send(ws, header(op, DATA))
        assert await close_code(ws) == 4400
    await stop(waiting)


def racing(engine, table: str, meanwhile, kinds: tuple[type, ...] = (Insert, Update)):
    """Sessions that run *meanwhile* once, just before their first statement of *kinds* on *table*:
    another transaction landing between a check and a write."""
    pending = [meanwhile]

    class Racing(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if pending and isinstance(statement, kinds) and statement.table.name == table:
                await pending.pop()()
            return await super().execute(statement, *args, **kwargs)

    return async_sessionmaker(engine, class_=Racing, expire_on_commit=False)


async def test_a_chunk_for_an_operation_stopped_and_reaped_meanwhile_is_unwanted(
    laptop_rig, engine, session_factory, redis_client,
):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(read_request(rig)))
    await eventually(lambda: _pending_count(rig, 1))
    [op] = await rig.ops.pending(rig.device_id, 1)
    sha = hashlib.sha256(DATA).hexdigest()
    assert await rig.ops.start_transfer(rig.device_id, 1, "conn", op.id, op.digest, len(DATA), sha) == "started"

    async def stopped_and_reaped() -> None:
        await rig.ops.cancel([rig.root])
        async with session_factory() as db:
            await db.execute(delete(DeviceTransfer).where(DeviceTransfer.operation_id == op.id))
            await db.commit()

    ops = DeviceOperations(racing(engine, "device_transfers", stopped_and_reaped), redis_client)
    # The live connection hears it is unwanted; "lost" would close it with 4409, for good.
    assert await ops.store_chunk(rig.device_id, 1, "conn", op.id, op.digest, 0, DATA[:CHUNK_BYTES], None) == "unwanted"
    assert await asyncio.wait_for(waiting, 5.0) == CANCELLED_OUTCOME


async def test_two_connections_starting_one_transfer_at_once_leave_it_to_one(
    laptop_rig, engine, session_factory, redis_client,
):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(read_request(rig)))
    await eventually(lambda: _pending_count(rig, 1))
    [op] = await rig.ops.pending(rig.device_id, 1)
    sha = hashlib.sha256(DATA).hexdigest()

    async def the_other_starts() -> None:
        assert await rig.ops.start_transfer(
            rig.device_id, 1, "conn-2", op.id, op.digest, len(DATA), sha,
        ) == "started"

    ops = DeviceOperations(racing(engine, "device_transfers", the_other_starts), redis_client)
    assert await ops.start_transfer(rig.device_id, 1, "conn-1", op.id, op.digest, len(DATA), sha) == "busy"
    assert await transfers_of(session_factory, rig.device_id) == 1
    assert await rig.ops.store_chunk(
        rig.device_id, 1, "conn-2", op.id, op.digest, 0, DATA[:CHUNK_BYTES], None,
    ) == "stored"
    await stop(waiting)


async def test_a_header_racing_the_last_chunk_of_its_transfer_leaves_the_data_whole(
    laptop_rig, engine, session_factory, redis_client,
):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(read_request(rig)))
    await eventually(lambda: _pending_count(rig, 1))
    [op] = await rig.ops.pending(rig.device_id, 1)
    sha = hashlib.sha256(DATA).hexdigest()
    assert await rig.ops.start_transfer(rig.device_id, 1, "conn-1", op.id, op.digest, len(DATA), sha) == "started"
    for seq in (0, 1):
        assert await rig.ops.store_chunk(
            rig.device_id, 1, "conn-1", op.id, op.digest, seq, DATA[seq * CHUNK_BYTES:(seq + 1) * CHUNK_BYTES], None,
        ) == "stored"

    async def the_last_chunk_lands() -> None:
        assert await rig.ops.store_chunk(
            rig.device_id, 1, "conn-1", op.id, op.digest, 2, DATA[2 * CHUNK_BYTES:], named(DATA),
        ) == "completed"

    ops = DeviceOperations(racing(engine, "device_transfers", the_last_chunk_lands, kinds=(Delete,)), redis_client)
    # The old connection's last chunk answered the operation: its data stays.
    assert await ops.start_transfer(rig.device_id, 1, "conn-2", op.id, op.digest, len(DATA), sha) == "busy"
    assert await asyncio.wait_for(waiting, 5.0) == named(DATA)
    assert await stored(session_factory, str(op.id)) == DATA


async def test_a_header_while_the_last_chunk_of_its_transfer_is_being_stored_waits_and_leaves_the_data_whole(
    laptop_rig, engine, session_factory, redis_client,
):
    rig = laptop_rig
    waiting = asyncio.create_task(rig.ops.run(read_request(rig)))
    await eventually(lambda: _pending_count(rig, 1))
    [op] = await rig.ops.pending(rig.device_id, 1)
    sha = hashlib.sha256(DATA).hexdigest()
    assert await rig.ops.start_transfer(rig.device_id, 1, "conn-1", op.id, op.digest, len(DATA), sha) == "started"
    for seq in (0, 1):
        assert await rig.ops.store_chunk(
            rig.device_id, 1, "conn-1", op.id, op.digest, seq, DATA[seq * CHUNK_BYTES:(seq + 1) * CHUNK_BYTES], None,
        ) == "stored"
    seen: dict = {}

    async def the_header_comes_meanwhile() -> None:
        seen["start"] = asyncio.create_task(
            rig.ops.start_transfer(rig.device_id, 1, "conn-2", op.id, op.digest, len(DATA), sha),
        )
        await asyncio.sleep(0.5)
        seen["blocked"] = not seen["start"].done()

    # The last chunk's transaction has moved its transfer whole and not yet answered the operation.
    ops = DeviceOperations(
        racing(engine, "device_operations", the_header_comes_meanwhile, kinds=(Update,)), redis_client,
    )
    assert await ops.store_chunk(
        rig.device_id, 1, "conn-1", op.id, op.digest, 2, DATA[2 * CHUNK_BYTES:], named(DATA),
    ) == "completed"
    # The header's DELETE waited for that transaction, then found the transfer whole and kept it.
    assert seen["blocked"]
    assert await asyncio.wait_for(seen["start"], 5.0) == "busy"
    assert await asyncio.wait_for(waiting, 5.0) == named(DATA)
    assert await stored(session_factory, str(op.id)) == DATA


async def test_the_reference_laptop_sends_a_read_too_large_for_a_frame_in_chunks(laptop_rig, session_factory):
    rig = laptop_rig
    await rig.laptop.connect()
    data = os.urandom(3 * CHUNK_BYTES + 5)
    (rig.folder / "big.bin").write_bytes(data)
    outcome = await asyncio.wait_for(rig.ops.run(read_request(rig)), 10.0)
    assert outcome == named(data)
    [operation_id] = rig.laptop.outcomes
    assert await stored(session_factory, operation_id) == data
    # Four chunks: three acknowledged, the last answered with the op_ack.
    assert rig.laptop.chunks_sent == [(operation_id, seq) for seq in range(4)]
    assert rig.laptop.frames.count("chunk_ack") == 3


async def test_a_read_too_large_for_a_frame_reaches_its_handler_whole(laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    data = os.urandom(3 * CHUNK_BYTES + 5)
    (rig.folder / "big.bin").write_bytes(data)
    wio = device_io(rig.ops, rig.device_id, rig.root, rig.folder)
    assert await asyncio.wait_for(wio.read(str(rig.folder / "big.bin")), 10.0) == data


def big_text(lines: int = 60_000) -> str:
    return "".join(f"line {n:06d} of a log too large for one frame\n" for n in range(lines))


async def consumed_of(session_factory, root: UUID, call_id: str) -> list:
    async with session_factory() as db:
        return list((await db.execute(
            select(DeviceTransfer.consumed_at)
            .join(DeviceOperation, DeviceOperation.id == DeviceTransfer.operation_id)
            .where(DeviceOperation.root_session_id == root, DeviceOperation.invocation_id.endswith(f":{call_id}"))
        )).scalars())


async def test_a_committed_tool_result_marks_what_it_read_consumed(laptop_rig, session_factory, redis_client):
    rig = laptop_rig
    await rig.laptop.connect()
    # A document: it is read whole, while a text file's page never needs a transfer.
    (rig.folder / "report.pdf").write_bytes(pdf(2 * 1024 * 1024))
    store = SessionStore(session_factory)
    read = await tool_call(
        rig, store, builtin_tools(), "call_1", "read_file", {"path": "report.pdf"},
        redis_client=redis_client, session_factory=session_factory,
    )
    assert PDF_TEXT in read["content"]
    [consumed] = await consumed_of(session_factory, rig.root, "call_1")
    assert consumed is not None


async def test_a_resumed_call_whose_result_is_no_longer_kept_is_reported_interrupted(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    await rig.laptop.connect()
    # A document: its read is the call's last operation, so only the payload being gone makes the replay diverge.
    (rig.folder / "report.pdf").write_bytes(pdf(2 * 1024 * 1024))
    store, tools = SessionStore(session_factory), builtin_tools()
    io = {"redis_client": redis_client, "session_factory": session_factory}
    await tool_call(rig, store, tools, "call_1", "read_file", {"path": "report.pdf"}, **io)
    await forget_result(store, session_factory, rig.root, "call_1")
    async with session_factory() as db:
        await db.execute(delete(DeviceTransfer))
        await db.commit()
    file_ops._read_tracker.clear()
    ran = len(rig.laptop.ran)
    await take_over(store, rig)

    resumed = await resume_call(rig, store, tools, "call_1", "read_file", {"path": "report.pdf"}, **io)

    assert resumed["content"] == INTERRUPTED
    assert len(rig.laptop.ran) == ran


async def answered(rig, ops: DeviceOperations, data: bytes = DATA, device_id: UUID | None = None, root=None) -> UUID:
    """A read answered with a whole transfer, stored as the link stores it."""
    device_id, root = device_id or rig.device_id, root or rig.root
    request = OperationRequest(**{**_fields(read_request(rig)), "device_id": device_id, "root_session_id": root,
                                  "calling_session_id": root})
    waiting = asyncio.create_task(ops.run(request))
    await eventually(lambda: _has_open(ops, device_id))
    [op] = await ops.pending(device_id, 1)
    sha = hashlib.sha256(data).hexdigest()
    assert await ops.start_transfer(device_id, 1, "conn", op.id, op.digest, len(data), sha) == "started"
    count = -(-len(data) // CHUNK_BYTES)
    for seq in range(count):
        await ops.store_chunk(
            device_id, 1, "conn", op.id, op.digest, seq, data[seq * CHUNK_BYTES:(seq + 1) * CHUNK_BYTES],
            named(data) if seq == count - 1 else None,
        )
    assert await asyncio.wait_for(waiting, 5.0) == named(data)
    return op.id


async def _has_open(ops: DeviceOperations, device_id: UUID) -> bool:
    return bool(await ops.pending(device_id, 1))


async def half_sent(rig, ops: DeviceOperations, device_id: UUID, root: UUID) -> tuple[UUID, asyncio.Task]:
    """A read whose transfer stopped after its first chunk, its operation still open."""
    request = OperationRequest(**{**_fields(read_request(rig)), "device_id": device_id, "root_session_id": root,
                                  "calling_session_id": root})
    waiting = asyncio.create_task(ops.run(request))
    await eventually(lambda: _has_open(ops, device_id))
    [op] = await ops.pending(device_id, 1)
    sha = hashlib.sha256(DATA).hexdigest()
    assert await ops.start_transfer(device_id, 1, "conn", op.id, op.digest, len(DATA), sha) == "started"
    assert await ops.store_chunk(device_id, 1, "conn", op.id, op.digest, 0, DATA[:CHUNK_BYTES], None) == "stored"
    return op.id, waiting


async def aged(session_factory, operation_id: UUID, **values) -> None:
    async with session_factory() as db:
        await db.execute(update(DeviceTransfer).where(DeviceTransfer.operation_id == operation_id).values(**values))
        await db.commit()


async def kept(session_factory) -> set[UUID]:
    async with session_factory() as db:
        return set((await db.execute(select(DeviceTransfer.operation_id))).scalars())


async def test_the_reaper_deletes_what_nothing_will_read_and_keeps_every_operation(laptop_rig, api, session_factory):
    rig = laptop_rig
    ops = rig.ops
    async with session_factory() as db:
        await db.execute(delete(DeviceTransfer))
        await db.commit()
    consumed_long_ago = await answered(rig, ops)
    await aged(session_factory, consumed_long_ago, consumed_at=func.now() - timedelta(hours=25))
    consumed_lately = await answered(rig, ops)
    await aged(session_factory, consumed_lately, consumed_at=func.now() - timedelta(hours=1))
    orphan = await answered(rig, ops)
    await aged(session_factory, orphan, created_at=func.now() - timedelta(days=8))
    waiting_for_its_tool = await answered(rig, ops)
    await aged(session_factory, waiting_for_its_tool, created_at=func.now() - timedelta(days=1))
    stopped_mid_way, stopped = await half_sent(rig, ops, rig.device_id, rig.root)
    await ops.cancel([rig.root])
    await asyncio.wait_for(stopped, 5.0)
    # Another computer's transfer under way: one half-sent transfer per device.
    other, other_root = await bound_device(api, "Other laptop")
    under_way, still_open = await half_sent(rig, ops, UUID(other["id"]), other_root)
    async with session_factory() as db:
        operations = (await db.execute(select(func.count()).select_from(DeviceOperation))).scalar_one()

    assert await reap_transfers(session_factory) == 3

    assert await kept(session_factory) == {consumed_lately, waiting_for_its_tool, under_way}
    async with session_factory() as db:
        assert (await db.execute(select(func.count()).select_from(DeviceOperation))).scalar_one() == operations
        assert (await db.execute(
            select(func.count()).select_from(DeviceTransferChunk)
            .where(DeviceTransferChunk.operation_id.in_([consumed_long_ago, orphan, stopped_mid_way]))
        )).scalar_one() == 0
    await stop(still_open)


PDF_TEXT = "Transfers carry this document whole."


def pdf(size: int = 30 * 1024 * 1024) -> bytes:
    """A one-page PDF that says PDF_TEXT, made about *size* bytes by an attachment."""
    page = io.BytesIO()
    drawing = canvas.Canvas(page)
    drawing.drawString(72, 720, PDF_TEXT)
    drawing.save()
    writer = PdfWriter(PdfReader(io.BytesIO(page.getvalue())))
    writer.add_attachment("padding.bin", os.urandom(size))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class StoppingStore(SessionStore):
    """A worker that stops just before it commits a tool result."""

    async def emit_event(self, session_id, kind, data, **kwargs):
        if kind == EventType.TOOL_RESULT:
            raise asyncio.CancelledError("the worker stopped")
        return await super().emit_event(session_id, kind, data, **kwargs)


async def test_a_worker_stopped_after_the_computer_sent_a_30_mib_pdf_resumes_with_the_same_bytes(
    laptop_rig, session_factory, redis_client,
):
    rig = laptop_rig
    await rig.laptop.connect()
    (rig.folder / "report.pdf").write_bytes(pdf())
    store, tools = SessionStore(session_factory), builtin_tools()
    io_ = {"redis_client": redis_client, "session_factory": session_factory}
    with pytest.raises(asyncio.CancelledError):
        await tool_call(rig, StoppingStore(session_factory), tools, "call_1", "read_file", {"path": "report.pdf"}, **io_)
    # The computer sent the whole PDF; no result was committed, so nothing is consumed.
    assert rig.laptop.ran[-1] == "read" and rig.laptop.chunks_sent[-1][1] == 30
    assert await consumed_of(session_factory, rig.root, "call_1") == [None]
    ran, sent = len(rig.laptop.ran), len(rig.laptop.chunks_sent)
    file_ops._read_tracker.clear()
    await take_over(store, rig)

    resumed = await resume_call(rig, store, tools, "call_1", "read_file", {"path": "report.pdf"}, **io_)

    assert PDF_TEXT in resumed["content"]
    assert (len(rig.laptop.ran), len(rig.laptop.chunks_sent)) == (ran, sent)
    [consumed] = await consumed_of(session_factory, rig.root, "call_1")
    assert consumed is not None
