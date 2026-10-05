"""The reference laptop's transfers on their own, against a small server."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os

import pytest
from websockets.asyncio.server import serve

from surogates.devices.workspace import CHUNK_BYTES, MAX_PAYLOAD_BYTES, MAX_WRITE_BYTES
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.fake_laptop import FakeLaptop

pytestmark = pytest.mark.asyncio

WELCOME = {
    "type": "welcome", "protocol": 1, "device_id": "d", "org_id": "o", "agent_id": "a", "user_id": "u",
    "name": "Laptop", "heartbeat_s": 15,
}


async def until(check, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    assert check()


def read_op(path) -> dict:
    return {
        "type": "op", "id": "op-1", "session_id": "r", "calling_session_id": "r", "invocation_id": "1:c",
        "ordinal": 1, "kind": "read", "args": {"key": str(path), "max_bytes": None}, "digest": "d",
    }


def chunks(frames: list[dict]) -> list[int]:
    return [frame["seq"] for frame in frames if frame["type"] == "chunk"]


async def test_a_transfer_starts_over_on_a_newer_connection_and_the_older_one_sends_nothing_more(tmp_path):
    (tmp_path / "big.bin").write_bytes(os.urandom(6 * CHUNK_BYTES))
    op = read_op(tmp_path / "big.bin")
    connections: list[list[dict]] = []
    replaced, acknowledged_late = asyncio.Event(), asyncio.Event()
    late: list[asyncio.Task] = []

    async def acknowledge_late(ws) -> None:
        # The first connection's acknowledgement, arriving once the laptop has moved on, and
        # before the second connection sends the operation again.
        await replaced.wait()
        await ws.send(json.dumps({"type": "chunk_ack", "id": "op-1", "seq": 0}))
        acknowledged_late.set()

    async def server(ws) -> None:
        frames: list[dict] = []
        connections.append(frames)
        # The first connection acknowledges nothing in time: its transfer waits with its window full.
        acknowledging = len(connections) > 1
        await ws.recv()
        await ws.send(json.dumps(WELCOME))
        if acknowledging:
            await acknowledged_late.wait()
        await ws.send(json.dumps(op))
        if not acknowledging:
            late.append(asyncio.create_task(acknowledge_late(ws)))
        async for raw in ws:
            frame = json.loads(raw)
            frames.append(frame)
            if acknowledging and frame["type"] == "chunk":
                last = frame["seq"] == 5
                await ws.send(json.dumps(
                    {"type": "op_ack", "id": "op-1"} if last else {"type": "chunk_ack", "id": "op-1", "seq": frame["seq"]},
                ))

    # A chunk's frame is about 1.4 MB: the link's own limit is 2 MiB characters.
    async with serve(server, "127.0.0.1", 0, max_size=None) as running:
        port = running.sockets[0].getsockname()[1]
        laptop = FakeLaptop(f"ws://127.0.0.1:{port}", "surg_dev_test", LocalWorkspaceIO(str(tmp_path)))
        try:
            await laptop.connect()
            await until(lambda: chunks(connections[0]) == [0, 1, 2, 3])
            await laptop.connect()
            replaced.set()
            await until(lambda: "op-1" in laptop.acked)
            # The first connection's transfer ended quietly when the second replaced it.
            failed = [task.exception() for task in laptop._tasks if task.done() and task.exception()]
        finally:
            await laptop.disconnect()
    assert failed == []
    assert chunks(connections[0]) == [0, 1, 2, 3]
    assert chunks(connections[1]) == [0, 1, 2, 3, 4, 5]
    assert laptop.ran == ["read"]


async def test_an_operation_repeated_after_its_transfer_was_acknowledged_is_answered_with_nothing(tmp_path):
    (tmp_path / "big.bin").write_bytes(os.urandom(2 * CHUNK_BYTES))
    connections: list[list[dict]] = []

    async def server(ws) -> None:
        frames: list[dict] = []
        connections.append(frames)
        await ws.recv()
        await ws.send(json.dumps(WELCOME))
        await ws.send(json.dumps(read_op(tmp_path / "big.bin")))
        async for raw in ws:
            frame = json.loads(raw)
            if frame["type"] == "ping":
                continue
            frames.append(frame)
            if frame["type"] == "chunk":
                await ws.send(json.dumps(
                    {"type": "op_ack", "id": "op-1"} if frame["seq"] == 1
                    else {"type": "chunk_ack", "id": "op-1", "seq": frame["seq"]},
                ))

    async with serve(server, "127.0.0.1", 0, max_size=None) as running:
        port = running.sockets[0].getsockname()[1]
        laptop = FakeLaptop(f"ws://127.0.0.1:{port}", "surg_dev_test", LocalWorkspaceIO(str(tmp_path)))
        try:
            await laptop.connect()
            await until(lambda: "op-1" in laptop.acked)
            await laptop.connect()
            await until(lambda: laptop.received.count("op-1") == 2)
            # Time for a header or a chunk to go, were one to.
            await asyncio.sleep(0.2)
        finally:
            await laptop.disconnect()
    assert [frame["type"] for frame in connections[0]] == ["op_result", "chunk", "chunk"]
    # As the app's journal: the chunks went with the acknowledgement, so the server has its result.
    assert connections[1] == []
    assert laptop.ran == ["read"]


DATA = os.urandom(CHUNK_BYTES * 5 // 2)  # three chunks, the last one short


def write_op(path, sha256: str | None = None) -> dict:
    transfer = {"size": len(DATA), "sha256": sha256 or hashlib.sha256(DATA).hexdigest()}
    return {
        "type": "op", "id": "op-1", "session_id": "r", "calling_session_id": "r", "invocation_id": "1:c",
        "ordinal": 1, "kind": "write", "args": {"key": str(path), "transfer": transfer}, "digest": "d",
    }


def piece(seq: int) -> bytes:
    return DATA[seq * CHUNK_BYTES:(seq + 1) * CHUNK_BYTES]


def chunk(seq: int, data: bytes | None = None) -> dict:
    data = piece(seq) if data is None else data
    return {"type": "chunk", "id": "op-1", "seq": seq, "data": base64.b64encode(data).decode("ascii")}


async def sent_to(tmp_path, *frames: dict) -> tuple[FakeLaptop, list[dict]]:
    """A server that sends *frames* after its welcome: the laptop, and what it answered once every chunk is acknowledged."""
    back: list[dict] = []

    async def server(ws) -> None:
        await ws.recv()
        await ws.send(json.dumps(WELCOME))
        for frame in frames:
            await ws.send(json.dumps(frame))
        async for raw in ws:
            frame = json.loads(raw)
            if frame["type"] != "ping":
                back.append(frame)

    async with serve(server, "127.0.0.1", 0, max_size=None) as running:
        port = running.sockets[0].getsockname()[1]
        laptop = FakeLaptop(f"ws://127.0.0.1:{port}", "surg_dev_test", LocalWorkspaceIO(str(tmp_path)))
        try:
            await laptop.connect()
            sent = sum(frame["type"] == "chunk" for frame in frames)
            await until(lambda: sum(frame["type"] == "chunk_ack" for frame in back) == sent)
            # Time for an answer to go, were one to.
            await asyncio.sleep(0.2)
        finally:
            await laptop.disconnect()
    return laptop, back


async def test_a_write_stopped_while_its_data_comes_never_runs(tmp_path):
    laptop, back = await sent_to(
        tmp_path, write_op(tmp_path / "out.bin"), chunk(0), {"type": "cancel", "id": "op-1"}, chunk(1), chunk(2),
    )
    assert laptop.ran == []
    assert [frame["type"] for frame in back] == ["chunk_ack"] * 3
    assert not (tmp_path / "out.bin").exists()


@pytest.mark.parametrize(("sha256", "sent"), [
    # Named as the chunks come, so only the order check finds it.
    (hashlib.sha256(piece(1) + piece(0) + piece(2)).hexdigest(), [chunk(1), chunk(0), chunk(2)]),
    (None, [chunk(0), chunk(2)]),
    # Its last chunk too long, named as it comes, so only the size check finds it.
    (hashlib.sha256(DATA + b"more").hexdigest(), [chunk(0), chunk(1), chunk(2, piece(2) + b"more")]),
    ("0" * 64, [chunk(0), chunk(1), chunk(2)]),
], ids=["out-of-order", "a-chunk-missing", "a-chunk-too-long", "another-sha256"])
async def test_a_writes_data_that_does_not_come_whole_and_matching_is_answered_and_never_run(tmp_path, sha256, sent):
    laptop, back = await sent_to(tmp_path, write_op(tmp_path / "out.bin", sha256), *sent)
    assert laptop.ran == []
    assert [frame["outcome"] for frame in back if frame["type"] == "op_result"] == [{"error": {
        "type": "other",
        "message": "The data this computer received for this write was incomplete or did not match, so it was not written",
    }}]
    assert not (tmp_path / "out.bin").exists()


NAMED = {"size": len(DATA), "sha256": hashlib.sha256(DATA).hexdigest()}


@pytest.mark.parametrize("args", [
    {"data": base64.b64encode(b"x" * (MAX_PAYLOAD_BYTES + 1)).decode("ascii")},
    {"transfer": NAMED, "data": "aGk="},
    {"transfer": {**NAMED, "size": MAX_PAYLOAD_BYTES}},
    {"transfer": {**NAMED, "size": MAX_WRITE_BYTES + 1}},
    {"transfer": {**NAMED, "size": len(DATA) + 0.5}},
    {"transfer": {**NAMED, "sha256": NAMED["sha256"].upper()}},
    {"transfer": {**NAMED, "sha256": "0" * 63}},
    {"transfer": {**NAMED, "extra": 1}},
], ids=[
    "inline-over-1-mib", "data-beside-a-transfer", "no-more-than-1-mib", "over-50-mib", "a-fractional-size",
    "an-upper-case-sha256", "a-short-sha256", "another-key",
])
async def test_a_write_naming_its_data_in_a_form_the_app_does_not_take_is_answered_so_and_never_run(tmp_path, args):
    op = {**write_op(tmp_path / "out.bin"), "args": {"key": str(tmp_path / "out.bin"), **args}}
    laptop, back = await sent_to(tmp_path, op)
    assert laptop.ran == []
    assert [frame["outcome"] for frame in back if frame["type"] == "op_result"] == [{"error": {
        "type": "other",
        "message": "This write named its data in a form this computer does not take, so it was not written",
    }}]
    assert not (tmp_path / "out.bin").exists()
