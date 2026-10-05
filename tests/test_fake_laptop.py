"""The reference laptop on its own: a transfer under way when a newer connection replaces its own."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from websockets.asyncio.server import serve

from surogates.devices.workspace import CHUNK_BYTES
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


async def test_a_transfer_starts_over_on_a_newer_connection_and_the_older_one_sends_nothing_more(tmp_path):
    (tmp_path / "big.bin").write_bytes(os.urandom(6 * CHUNK_BYTES))
    op = {
        "type": "op", "id": "op-1", "session_id": "r", "calling_session_id": "r", "invocation_id": "1:c",
        "ordinal": 1, "kind": "read", "args": {"key": str(tmp_path / "big.bin"), "max_bytes": None}, "digest": "d",
    }
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

    def chunks(frames: list[dict]) -> list[int]:
        return [frame["seq"] for frame in frames if frame["type"] == "chunk"]

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
