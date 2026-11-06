"""The device link's delivery state, without a database or a socket."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from surogates.devices.link import CLOSE_PROTOCOL, _Close, _Link
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
        self.waits: list[tuple[uuid.UUID, str | None]] = []

    async def closed_among(self, device_id, operation_ids):
        return [i for i in operation_ids if i in self.closed]

    async def waiting(self, operation_id, reason):
        self.waits.append((operation_id, reason))

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


def waiting(operation_id, reason) -> dict:
    return {"type": "op_waiting", "id": str(operation_id), "reason": reason}


async def test_an_operation_waiting_for_its_folder_is_recorded_while_it_waits_and_forgotten_after():
    op = open_operation(1)
    operations = FakeOperations(op)
    link, _ = make_link(operations)
    await link.deliver()
    await link.waiting(waiting(op.id, "folder"))
    await link.waiting(waiting(op.id, None))
    assert operations.waits == [(op.id, "folder"), (op.id, None)]


async def test_a_wait_for_an_operation_not_delivered_on_this_link_changes_nothing():
    op = open_operation(1)
    operations = FakeOperations(op)
    link, _ = make_link(operations)
    await link.waiting(waiting(op.id, "folder"))
    await link.waiting(waiting(uuid.uuid4(), None))
    assert operations.waits == []


async def test_a_wait_that_cannot_be_recorded_leaves_the_link_open():
    op = open_operation(1)
    operations = FakeOperations(op)

    async def refuse(operation_id, reason):
        raise ConnectionError("redis is away")

    operations.waiting = refuse
    link, _ = make_link(operations)
    await link.deliver()
    await link.waiting(waiting(op.id, "folder"))


@pytest.mark.parametrize("frame_id, reason", [("nope", "folder"), (None, None), ("delivered", "offline"), ("delivered", 3)])
async def test_a_malformed_wait_closes_the_link_as_a_protocol_error(frame_id, reason):
    op = open_operation(1)
    link, _ = make_link(FakeOperations(op))
    await link.deliver()
    with pytest.raises(_Close) as closed:
        await link.waiting({"type": "op_waiting", "id": str(op.id) if frame_id == "delivered" else frame_id, "reason": reason})
    assert closed.value.code == CLOSE_PROTOCOL
