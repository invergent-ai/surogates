"""The device link's side of a write's transfer, without a database or a socket."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import uuid
from types import SimpleNamespace

import pytest

from surogates.devices.link import TRANSFER_WINDOW, _Close, _Link
from surogates.devices.operations import OpenOperation
from surogates.devices.workspace import CHUNK_BYTES

pytestmark = pytest.mark.asyncio

ROOT = uuid.UUID("00000000-0000-4000-8000-000000000001")
DATA = bytes(range(256)) * (CHUNK_BYTES * 11 // 2 // 256)  # five chunks and a half


def write(data: bytes = DATA, ordinal: int = 1) -> OpenOperation:
    return OpenOperation(
        id=uuid.uuid4(), root_session_id=ROOT, calling_session_id=ROOT, invocation_id=f"call-{ordinal}",
        ordinal=ordinal, kind="write",
        args={"key": "/f/big.bin", "transfer": {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}},
        digest=f"{ordinal:064d}",
    )


def which(ordinal: int, **args) -> OpenOperation:
    return OpenOperation(
        id=uuid.uuid4(), root_session_id=ROOT, calling_session_id=ROOT, invocation_id=f"call-{ordinal}",
        ordinal=ordinal, kind="which", args={"name": "sh", **args}, digest=f"{ordinal:064d}",
    )


class FakeOperations:
    """The journal's calls the link makes: open operations, and the data of the writes among them."""

    def __init__(self, *operations: OpenOperation, data: bytes = DATA) -> None:
        self.open = list(operations)
        self.data = data
        self.fetched: list[tuple[uuid.UUID, int]] = []

    async def closed_among(self, device_id, operation_ids):
        return []

    async def pending(self, device_id, generation, *, exclude=frozenset(), limit=100):
        return [op for op in self.open if op.id not in exclude][:limit]

    async def complete(self, device_id, generation, operation_id, digest, outcome):
        self.open = [op for op in self.open if op.id != operation_id]
        return "completed"

    async def outgoing_chunk(self, device_id, generation, operation_id, seq):
        if all(op.id != operation_id for op in self.open):
            return None  # closed, and its data reaped
        self.fetched.append((operation_id, seq))
        return self.data[seq * CHUNK_BYTES:(seq + 1) * CHUNK_BYTES]


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)

    def chunks(self, operation: OpenOperation) -> list[int]:
        return [f["seq"] for f in self.sent if f["type"] == "chunk" and f["id"] == str(operation.id)]


class FakePresence:
    async def holds(self, device_id, holder) -> bool:
        return True


class FetchedBeforeItCloses(FakeOperations):
    """Reads the chunk after the first window before the write closes, and returns it once let go."""

    def __init__(self, *operations: OpenOperation) -> None:
        super().__init__(*operations)
        self.fetching, self.go = asyncio.Event(), asyncio.Event()

    async def outgoing_chunk(self, device_id, generation, operation_id, seq):
        data = await super().outgoing_chunk(device_id, generation, operation_id, seq)
        if seq == TRANSFER_WINDOW:
            self.fetching.set()
            await self.go.wait()
        return data


async def started(
    *operations: OpenOperation, journal: FakeOperations | None = None,
) -> tuple[_Link, FakeSocket, FakeOperations, asyncio.Task]:
    socket, journal = FakeSocket(), journal or FakeOperations(*operations)
    link = _Link(
        socket, device=SimpleNamespace(id=uuid.uuid4(), credential_generation=1), holder="holder",
        presence=FakePresence(), operations=journal,
    )
    return link, socket, journal, asyncio.create_task(link.transfers())


async def settled() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def ack(operation: OpenOperation, seq: int) -> dict:
    return {"type": "chunk_ack", "id": str(operation.id), "seq": seq}


async def test_a_writes_data_follows_its_op_at_most_four_chunks_ahead_of_the_apps_acknowledgements():
    op = write()
    link, socket, _, task = await started(op)
    await link.deliver()
    await settled()
    assert socket.sent[0]["type"] == "op"
    assert socket.chunks(op) == [0, 1, 2, 3] == list(range(TRANSFER_WINDOW))
    # Each acknowledgement lets one more go.
    link.chunk_acked(ack(op, 0))
    await settled()
    assert socket.chunks(op) == [0, 1, 2, 3, 4]
    link.chunk_acked(ack(op, 2))  # acknowledges every chunk up to it
    await settled()
    assert socket.chunks(op) == [0, 1, 2, 3, 4, 5]
    sent = b"".join(base64.b64decode(f["data"]) for f in socket.sent if f["type"] == "chunk")
    assert sent == DATA
    task.cancel()


async def test_other_frames_go_while_a_transfer_waits_for_its_acknowledgements():
    op, small = write(), which(2)
    link, socket, journal, task = await started(op)
    await link.deliver()
    await settled()
    assert socket.chunks(op) == [0, 1, 2, 3]
    journal.open.append(small)
    await link.deliver()
    await link.send({"type": "pong"})
    assert [f["type"] for f in socket.sent[-2:]] == ["op", "pong"]
    assert socket.sent[-2]["id"] == str(small.id)
    task.cancel()


@pytest.mark.parametrize("how", ["forgotten", "answered", "closed"])
async def test_a_transfer_stops_once_its_write_is_cancelled_answered_or_closed(how):
    op, after = write(), write(ordinal=2)
    link, socket, journal, task = await started(op, after)
    await link.deliver()
    await settled()
    assert socket.chunks(op) == [0, 1, 2, 3]
    if how == "forgotten":
        link.forget(op.id)  # a live cancel
    elif how == "answered":
        await link.record({"type": "op_result", "id": str(op.id), "digest": op.digest, "outcome": {"ok": None}})
    else:
        journal.open.remove(op)  # cancelled, its live cancel lost, and its data reaped
        link.chunk_acked(ack(op, 0))
    await settled()
    assert socket.chunks(op) == [0, 1, 2, 3]
    # The next write's data goes.
    assert socket.chunks(after) == [0, 1, 2, 3]
    task.cancel()


@pytest.mark.parametrize("how", ["cancelled", "answered"])
async def test_no_chunk_follows_a_cancel_or_an_answer_that_comes_while_the_chunk_is_fetched(how):
    op = write()
    link, socket, journal, task = await started(journal=FetchedBeforeItCloses(op))
    await link.deliver()
    await settled()
    link.chunk_acked(ack(op, 0))
    await asyncio.wait_for(journal.fetching.wait(), 1.0)
    if how == "cancelled":
        link.forget(op.id)  # a live cancel
    else:
        await link.record({"type": "op_result", "id": str(op.id), "digest": op.digest, "outcome": {"ok": None}})
    journal.go.set()
    await settled()
    assert socket.chunks(op) == [0, 1, 2, 3]
    task.cancel()


async def test_one_write_at_a_time_the_next_once_the_last_chunk_is_acknowledged():
    op, after = write(), write(ordinal=2)
    link, socket, _, task = await started(op, after)
    await link.deliver()
    await settled()
    link.chunk_acked(ack(op, 3))
    await settled()
    assert socket.chunks(op) == [0, 1, 2, 3, 4, 5]
    assert socket.chunks(after) == []
    link.chunk_acked(ack(op, 5))
    await settled()
    assert socket.chunks(after) == [0, 1, 2, 3]
    task.cancel()


async def test_an_acknowledgement_for_no_transfer_under_way_changes_nothing():
    op, other = write(), write(ordinal=2)
    link, socket, _, task = await started(op)
    await link.deliver()
    await settled()
    link.chunk_acked(ack(other, 3))
    await settled()
    assert socket.chunks(op) == [0, 1, 2, 3]
    task.cancel()


@pytest.mark.parametrize("seq", [4, 99], ids=["next", "past-the-end"])
async def test_an_acknowledgement_of_a_chunk_not_sent_is_a_protocol_error(seq):
    # It would move the window past what went, and the write would never end.
    op = write()
    link, socket, _, task = await started(op)
    await link.deliver()
    await settled()
    assert socket.chunks(op) == [0, 1, 2, 3]
    with pytest.raises(_Close) as closed:
        link.chunk_acked(ack(op, seq))
    assert closed.value.code == 4400
    task.cancel()


async def test_only_a_write_has_data_to_send():
    other = which(1, transfer={"size": len(DATA), "sha256": hashlib.sha256(DATA).hexdigest()})
    link, socket, _, task = await started(other)
    await link.deliver()
    await settled()
    assert [f["type"] for f in socket.sent] == ["op"]
    task.cancel()


@pytest.mark.parametrize("frame", [
    {"type": "chunk_ack", "seq": 0},
    {"type": "chunk_ack", "id": "not-a-uuid", "seq": 0},
    {"type": "chunk_ack", "id": str(uuid.uuid4())},
    {"type": "chunk_ack", "id": str(uuid.uuid4()), "seq": -1},
    {"type": "chunk_ack", "id": str(uuid.uuid4()), "seq": "0"},
    {"type": "chunk_ack", "id": str(uuid.uuid4()), "seq": True},
], ids=["no-id", "bad-id", "no-seq", "negative", "seq-text", "seq-bool"])
async def test_a_malformed_acknowledgement_is_a_protocol_error(frame):
    link, _, _, task = await started()
    with pytest.raises(_Close) as closed:
        link.chunk_acked(frame)
    assert closed.value.code == 4400
    task.cancel()


async def test_a_write_queued_behind_another_counts_its_time_from_its_op(caplog):
    op, after = write(), write(ordinal=2)
    link, _, _, task = await started(op, after)
    with caplog.at_level(logging.INFO, logger="surogates.devices.link"):
        await link.deliver()
        await settled()
        await asyncio.sleep(0.3)  # the second write's op went; its data waits behind the first's
        link.chunk_acked(ack(op, 3))
        await settled()
        link.chunk_acked(ack(op, 5))
        await settled()
        link.forget(after.id)
        await settled()
    first, second = [record.getMessage() for record in caplog.records if record.name == "surogates.devices.link"]
    assert f"transfer {op.id} sent" in first and f"transfer {after.id} stopped" in second
    assert float(re.search(r"(\d+\.\d\d) s from its header", second).group(1)) >= 0.3
    task.cancel()


async def test_a_connection_that_ends_logs_each_write_it_had_still_to_send_as_cut_off(caplog):
    op, after = write(), write(ordinal=2)
    link, _, _, task = await started(op, after)
    with caplog.at_level(logging.INFO, logger="surogates.devices.link"):
        await link.deliver()
        await settled()
        task.cancel()  # the first write's data under way, the second's op sent
        await settled()
    lines = [record.getMessage() for record in caplog.records if record.name == "surogates.devices.link"]
    assert [re.search(r"transfer (\S+) (.+?):", line).groups() for line in lines] == [
        (str(op.id), "cut off"), (str(after.id), "cut off"),
    ], lines


@pytest.mark.parametrize(("end", "how"), [("acknowledged", "sent"), ("cancelled", "stopped"), ("cut off", "cut off")])
async def test_a_writes_transfer_is_logged_once_with_how_it_ended(caplog, end, how):
    op = write()
    link, _, _, task = await started(op)
    with caplog.at_level(logging.INFO, logger="surogates.devices.link"):
        await link.deliver()
        await settled()
        link.chunk_acked(ack(op, 3))
        await settled()
        if end == "acknowledged":
            link.chunk_acked(ack(op, 5))
        elif end == "cancelled":
            link.forget(op.id)
        else:
            task.cancel()  # the connection ended
        await settled()
    [line] = [record.getMessage() for record in caplog.records if record.name == "surogates.devices.link"]
    assert re.fullmatch(
        rf"device {link._device.id} transfer {op.id} {how}: {len(DATA)} bytes, \d+\.\d\d s from its header",
        line,
    ), line
    task.cancel()
