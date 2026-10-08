"""The device link's side of a transfer, without a database or a socket."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import uuid
from types import SimpleNamespace

import pytest

from surogates.devices.link import DAMAGED_OUTCOME, _Close, _Link
from surogates.devices.workspace import CHUNK_BYTES, MAX_PAYLOAD_BYTES, MAX_READ_BYTES

pytestmark = pytest.mark.asyncio

OPERATION = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
DATA = bytes(range(256)) * (CHUNK_BYTES * 5 // 2 // 256)  # two chunks and a half


class FakeOperations:
    """The journal's transfer calls the link makes, answering with the statuses a test sets."""

    def __init__(self, *, start: str = "started", chunk: str | None = None) -> None:
        self.start_status = start
        self.chunk_status = chunk
        self.starts: list[tuple] = []
        self.chunks: list[tuple[int, bytes, dict | None]] = []

    async def start_transfer(self, device_id, generation, holder, operation_id, digest, size, sha256):
        self.starts.append((operation_id, digest, size, sha256))
        return self.start_status

    async def store_chunk(self, device_id, generation, holder, operation_id, digest, seq, data, outcome):
        self.chunks.append((seq, data, outcome))
        if self.chunk_status is not None:
            return self.chunk_status
        return "completed" if outcome is not None else "stored"


class FakePresence:
    """The device's presence key: the connection that holds it, or None once it expired."""

    def __init__(self, key: str | None) -> None:
        self.key = key

    async def refresh(self, device_id, holder) -> bool:
        # An expired key is the connection's to take back, as DevicePresence's script does.
        if self.key not in (None, holder):
            return False
        self.key = holder
        return True


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)


def make_link(operations: FakeOperations, *, key: str | None = "holder") -> tuple[_Link, FakeSocket]:
    socket = FakeSocket()
    link = _Link(
        socket,
        device=SimpleNamespace(id=uuid.uuid4(), credential_generation=1),
        holder="holder",
        presence=FakePresence(key),
        operations=operations,
    )
    return link, socket


def header(data: bytes = DATA, *, operation_id: uuid.UUID = OPERATION, **transfer) -> dict:
    named = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), **transfer}
    return {
        "type": "op_result", "id": str(operation_id), "digest": "d" * 64,
        "outcome": {"ok": {"transfer": named}},
    }


def chunks(data: bytes = DATA, *, operation_id: uuid.UUID = OPERATION) -> list[dict]:
    return [
        {
            "type": "chunk", "id": str(operation_id), "seq": seq,
            "data": base64.b64encode(data[at:at + CHUNK_BYTES]).decode("ascii"),
        }
        for seq, at in enumerate(range(0, len(data), CHUNK_BYTES))
    ]


async def test_a_header_and_its_chunks_are_stored_in_order_and_the_last_is_acknowledged_as_the_result():
    operations = FakeOperations()
    link, socket = make_link(operations)
    await link.record(header())
    for frame in chunks():
        await link.chunk(frame)

    assert operations.starts == [(OPERATION, "d" * 64, len(DATA), hashlib.sha256(DATA).hexdigest())]
    assert b"".join(data for _, data, _ in operations.chunks) == DATA
    assert [seq for seq, _, _ in operations.chunks] == [0, 1, 2]
    # Only the last chunk records the outcome, which is the header's.
    assert [outcome for _, _, outcome in operations.chunks] == [None, None, header()["outcome"]]
    assert socket.sent == [
        {"type": "chunk_ack", "id": str(OPERATION), "seq": 0},
        {"type": "chunk_ack", "id": str(OPERATION), "seq": 1},
        {"type": "op_ack", "id": str(OPERATION)},
    ]


async def test_a_transfer_that_completes_is_logged_once_with_its_device_size_and_time(caplog):
    link, _ = make_link(FakeOperations())
    with caplog.at_level(logging.INFO, logger="surogates.devices.link"):
        await link.record(header())
        for frame in chunks():
            await link.chunk(frame)
    [line] = [record.getMessage() for record in caplog.records if record.name == "surogates.devices.link"]
    assert re.fullmatch(
        rf"device {link._device.id} transfer {OPERATION} completed: {len(DATA)} bytes, \d+\.\d\d s from its header",
        line,
    ), line


@pytest.mark.parametrize(("start", "key", "ending"), [
    ("rejected", "holder", r"closed 4400 \(transfer for an operation this device was not given, or whose result is never one\)"),
    ("stale", "holder", r"closed 4403 \(credentials rotated\)"),
    ("started", "another", r"closed 4409 \(superseded\)"),
    ("busy", "holder", "busy"),
], ids=["rejected", "stale", "superseded", "busy"])
async def test_a_refused_header_is_logged_once_with_how_it_ended(caplog, start, key, ending):
    link, _ = make_link(FakeOperations(start=start), key=key)
    with caplog.at_level(logging.INFO, logger="surogates.devices.link"):
        with pytest.raises(_Close) as closed:
            await link.record(header())
        # As serve_device_link ends the connection.
        link.closed(closed.value.code, closed.value.reason)
    [line] = [record.getMessage() for record in caplog.records if record.name == "surogates.devices.link"]
    assert re.fullmatch(
        rf"device {link._device.id} transfer {OPERATION} {ending}: {len(DATA)} bytes, \d+\.\d\d s from its header",
        line,
    ), line


async def test_data_that_does_not_match_its_sha256_is_recorded_as_damaged():
    operations = FakeOperations()
    link, socket = make_link(operations)
    await link.record(header(sha256="0" * 64))
    for frame in chunks():
        await link.chunk(frame)
    assert operations.chunks[-1][2] == DAMAGED_OUTCOME
    assert socket.sent[-1] == {"type": "op_ack", "id": str(OPERATION)}


async def test_a_header_for_a_closed_operation_is_unwanted_and_the_chunks_behind_it_are_dropped():
    operations = FakeOperations(start="unwanted")
    link, socket = make_link(operations)
    await link.record(header())
    for frame in chunks():
        await link.chunk(frame)
    assert socket.sent == [{"type": "unwanted", "id": str(OPERATION)}]
    assert operations.chunks == []

    # The next transfer is taken.
    operations.start_status = "started"
    await link.record(header())
    await link.chunk(chunks()[0])
    assert socket.sent[-1] == {"type": "chunk_ack", "id": str(OPERATION), "seq": 0}


async def test_an_operation_that_closes_mid_transfer_makes_its_next_chunk_unwanted():
    operations = FakeOperations()
    link, socket = make_link(operations)
    await link.record(header())
    first, second, third = chunks()
    await link.chunk(first)
    operations.chunk_status = "unwanted"
    await link.chunk(second)
    await link.chunk(third)
    assert socket.sent == [
        {"type": "chunk_ack", "id": str(OPERATION), "seq": 0},
        {"type": "unwanted", "id": str(OPERATION)},
    ]
    assert [seq for seq, _, _ in operations.chunks] == [0, 1]


def _wrong_length() -> dict:
    frame = chunks()[0]
    return {**frame, "data": base64.b64encode(DATA[:CHUNK_BYTES - 1]).decode("ascii")}


@pytest.mark.parametrize("frame", [
    {**chunks()[1]},  # out of order
    {**chunks()[0], "seq": "0"},
    {**chunks()[0], "seq": True},
    _wrong_length(),
    {**chunks()[0], "data": chunks()[0]["data"][:-1]},  # not padded base64
    {**chunks()[0], "data": chunks()[0]["data"].replace("A", "-", 1)},
    {**chunks()[0], "data": None},
    {"type": "chunk", "seq": 0, "data": ""},
], ids=["out-of-order", "seq-text", "seq-bool", "short", "unpadded", "base64url", "no-data", "no-id"])
async def test_a_malformed_chunk_is_a_protocol_error(frame):
    link, _ = make_link(FakeOperations())
    await link.record(header())
    with pytest.raises(_Close) as closed:
        await link.chunk(frame)
    assert closed.value.code == 4400


@pytest.mark.parametrize("transfer", [
    {"size": MAX_PAYLOAD_BYTES},
    {"size": MAX_READ_BYTES + 1},
    {"size": float(len(DATA))},
    {"sha256": "A" * 64},
    {"sha256": "0" * 63},
    {"extra": 1},
], ids=["inline-size", "over-the-cap", "float", "upper-case", "short-digest", "extra-key"])
async def test_a_malformed_header_is_a_protocol_error(transfer):
    operations = FakeOperations()
    link, _ = make_link(operations)
    with pytest.raises(_Close) as closed:
        await link.record(header(**transfer))
    assert closed.value.code == 4400
    assert operations.starts == []


async def test_a_header_whose_ok_value_carries_more_than_the_transfer_is_a_protocol_error():
    operations = FakeOperations()
    link, _ = make_link(operations)
    frame = header()
    frame["outcome"]["ok"]["data"] = "AAAA"
    with pytest.raises(_Close) as closed:
        await link.record(frame)
    assert closed.value.code == 4400
    assert operations.starts == []


async def test_a_second_header_while_a_transfer_is_under_way_is_a_protocol_error():
    link, _ = make_link(FakeOperations())
    await link.record(header())
    with pytest.raises(_Close) as closed:
        await link.record(header(operation_id=uuid.uuid4()))
    assert closed.value.code == 4400


async def test_a_header_on_a_connection_another_superseded_takes_nothing_over():
    operations = FakeOperations()
    link, socket = make_link(operations, key="another")
    with pytest.raises(_Close) as closed:
        await link.record(header())
    assert closed.value.code == 4409
    assert operations.starts == []
    assert socket.sent == []


async def test_a_header_after_the_presence_key_expired_takes_it_back():
    operations = FakeOperations()
    link, socket = make_link(operations, key=None)
    await link.record(header())
    await link.chunk(chunks()[0])
    assert link._presence.key == "holder"
    assert socket.sent == [{"type": "chunk_ack", "id": str(OPERATION), "seq": 0}]


@pytest.mark.parametrize(("key", "code"), [("another", 4409), ("holder", 4400)], ids=["superseded", "live"])
async def test_a_lost_transfer_ends_a_superseded_connection_for_good_and_the_live_one_for_a_resend(key, code):
    link, _ = make_link(FakeOperations(chunk="lost"))
    await link.record(header())
    link._presence.key = key
    with pytest.raises(_Close) as closed:
        await link.chunk(chunks()[0])
    assert closed.value.code == code


@pytest.mark.parametrize(("start", "chunk", "code"), [
    ("rejected", None, 4400),
    ("busy", None, 4400),
    ("stale", None, 4403),
    ("started", "stale", 4403),
])
async def test_what_the_journal_refuses_ends_the_link(start, chunk, code):
    link, _ = make_link(FakeOperations(start=start, chunk=chunk))
    with pytest.raises(_Close) as closed:
        await link.record(header())
        await link.chunk(chunks()[0])
    assert closed.value.code == code
