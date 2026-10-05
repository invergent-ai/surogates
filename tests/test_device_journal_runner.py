"""A tool call's journal runner, over a fake journal: how it hands a transfer's bytes to the call."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid

import pytest

from surogates.devices import operations as operations_module
from surogates.devices.operations import JournalRunner

pytestmark = pytest.mark.asyncio


def named(data: bytes) -> dict:
    return {"ok": {"transfer": {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}}


class FakeOperations:
    """Answers each ordinal's read with a transfer of that ordinal's data."""

    def __init__(self, data: dict[int, bytes], gate: asyncio.Event | None = None) -> None:
        self.data = data
        self.gate = gate

    async def run(self, request):
        if self.gate is not None:
            await self.gate.wait()
        return named(self.data[request.ordinal])

    async def transfer_chunks(self, calling_session_id, invocation_id, ordinal):
        data = self.data[ordinal]
        return [data[:3], data[3:]]


def runner_over(operations: FakeOperations) -> JournalRunner:
    root = uuid.uuid4()
    return JournalRunner(
        operations, device_id=uuid.uuid4(), root_session_id=root, calling_session_id=root, invocation_id="1:call",
    )


async def test_a_transfers_bytes_are_joined_and_checked_off_the_event_loop(monkeypatch):
    on_loop: list[bool] = []
    whole = operations_module._whole

    def watched(chunks, transfer):
        on_loop.append(threading.current_thread() is threading.main_thread())
        return whole(chunks, transfer)

    monkeypatch.setattr(operations_module, "_whole", watched)
    runner = runner_over(FakeOperations({1: b"the file's data"}))
    assert await runner.run("read", {"key": "/f/a", "max_bytes": None}) == {"ok": b"the file's data"}
    assert on_loop == [False]


async def test_operations_of_one_call_asked_for_together_each_get_their_own_data():
    gate = asyncio.Event()
    runner = runner_over(FakeOperations({1: b"first file", 2: b"second file"}, gate))
    first = asyncio.create_task(runner.run("read", {"key": "/f/a", "max_bytes": None}))
    second = asyncio.create_task(runner.run("read", {"key": "/f/b", "max_bytes": None}))
    await asyncio.sleep(0)
    gate.set()
    assert await asyncio.gather(first, second) == [{"ok": b"first file"}, {"ok": b"second file"}]
