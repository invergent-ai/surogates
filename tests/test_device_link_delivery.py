"""The device link's delivery state, without a database or a socket."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from surogates.devices.link import _Link
from surogates.devices.operations import OpenOperation

ROOT = uuid.UUID("00000000-0000-4000-8000-000000000001")


def open_operation(ordinal: int) -> OpenOperation:
    return OpenOperation(
        id=uuid.uuid4(),
        root_session_id=ROOT,
        calling_session_id=ROOT,
        invocation_id=f"call-{ordinal}",
        ordinal=ordinal,
        kind="which",
        args={"name": "sh"},
        digest=f"{ordinal:064d}",
    )


class FakeOperations:
    """The journal's calls the link makes, over a list of open operations."""

    def __init__(self, *operations: OpenOperation, delay: float = 0.0) -> None:
        self.open = list(operations)
        self.delay = delay
        self.statuses: list[str] = []
        self.closed: set[uuid.UUID] = set()  # by a cancellation or a revocation

    async def closed_among(self, device_id, operation_ids):
        return [i for i in operation_ids if i in self.closed]

    async def pending(self, device_id, generation, *, exclude=frozenset(), limit=100):
        # The query reads the journal first and the caller sees the answer
        # later, as a database call does.
        found = [op for op in self.open if op.id not in exclude][:limit]
        await asyncio.sleep(self.delay)
        return found

    async def complete(self, device_id, generation, operation_id, digest, outcome):
        status = "completed" if any(op.id == operation_id for op in self.open) else "duplicate"
        self.open = [op for op in self.open if op.id != operation_id]
        self.statuses.append(status)
        return status


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)

    def operation_ids(self) -> list[str]:
        return [frame["id"] for frame in self.sent if frame["type"] == "op"]


class FakePresence:
    async def holds(self, device_id, holder) -> bool:
        return True


def make_link(operations: FakeOperations) -> tuple[_Link, FakeSocket]:
    socket = FakeSocket()
    link = _Link(
        socket,
        device=SimpleNamespace(id=uuid.uuid4(), credential_generation=1),
        holder="holder",
        presence=FakePresence(),
        operations=operations,
    )
    return link, socket


def reply(operation: OpenOperation) -> dict:
    return {"type": "op_result", "id": str(operation.id), "digest": operation.digest, "outcome": {"ok": True}}


async def test_concurrent_deliveries_send_an_operation_once():
    op = open_operation(1)
    link, socket = make_link(FakeOperations(op, delay=0.05))
    await asyncio.gather(link.deliver(), link.deliver())
    assert socket.operation_ids() == [str(op.id)]


@pytest.mark.parametrize("repeat", [False, True])
async def test_an_answered_operation_leaves_the_delivered_set(repeat):
    first, second = open_operation(1), open_operation(2)
    operations = FakeOperations(first, second)
    link, socket = make_link(operations)
    await link.deliver()
    assert link._delivered == {first.id, second.id}

    await link.record(reply(first))
    if repeat:
        await link.record(reply(first))
    assert operations.statuses == (["completed", "duplicate"] if repeat else ["completed"])
    # The answered one is gone from the set; the one still open stays in it.
    assert link._delivered == {second.id}
    await link.deliver()
    assert socket.operation_ids() == [str(first.id), str(second.id)]
    assert [f for f in socket.sent if f["type"] == "op_ack"] == [
        {"type": "op_ack", "id": str(first.id)}
    ] * (2 if repeat else 1)


async def test_a_cancelled_operation_is_told_to_the_app_once_and_leaves_the_delivered_set():
    first, second = open_operation(1), open_operation(2)
    operations = FakeOperations(first, second)
    link, socket = make_link(operations)
    await link.deliver()
    operations.closed.add(first.id)
    operations.open = [second]

    await link.deliver()
    await link.deliver()

    assert [f for f in socket.sent if f["type"] == "cancel"] == [{"type": "cancel", "id": str(first.id)}]
    assert link._delivered == {second.id}
