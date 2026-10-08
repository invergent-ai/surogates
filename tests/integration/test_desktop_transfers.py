"""Reads and writes too large for one frame, through the real desktop app over the real link."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from surogates.db.models import DeviceOperation, DeviceTransfer
from surogates.devices.operations import OperationRequest
from surogates.devices.workspace import MAX_READ_BYTES, MAX_WRITE_BYTES
from surogates.session.store import SessionStore
from surogates.tools.builtin import file_ops

from .test_desktop_file_operations import journal_dir  # noqa: F401  (fixture)
from .test_desktop_link_client import built_client, client, connected  # noqa: F401  (fixture)
from .test_device_transfers import PDF_TEXT, StoppingStore, big_text, consumed_of, pdf
from .test_device_writes import big_file, reporting
from .test_devices import (  # noqa: F401  (fixtures)
    _fields,
    api,
    builtin_tools,
    device_io,
    laptop_rig,
    link_url,
    request_for,
    resume_call,
    take_over,
    tool_call,
)

pytestmark = [pytest.mark.desktop, pytest.mark.asyncio(loop_scope="session")]


def journal_rows(journal) -> int:
    with contextlib.closing(sqlite3.connect(journal)) as db:
        return db.execute("SELECT count(*) FROM operations").fetchone()[0]


async def operations_of(session_factory, root: UUID) -> int:
    async with session_factory() as db:
        return (await db.execute(
            select(func.count()).select_from(DeviceOperation).where(DeviceOperation.root_session_id == root)
        )).scalar_one()


async def test_a_worker_stopped_after_the_app_sent_a_30_mib_pdf_resumes_with_the_same_bytes(
    built_client, laptop_rig, link_url, session_factory, redis_client, journal_dir,
):
    rig = laptop_rig
    folder = journal_dir / "folder"
    folder.mkdir()
    (folder / "report.pdf").write_bytes(pdf())
    journal = journal_dir / "journal.sqlite"
    store, tools = SessionStore(session_factory), builtin_tools()
    io_ = {"redis_client": redis_client, "session_factory": session_factory}
    app = await client(built_client, link_url, rig.token, journal, folder=folder)
    try:
        await app.until(connected)
        with pytest.raises(asyncio.CancelledError):
            await tool_call(
                rig, StoppingStore(session_factory), tools, "call_1", "read_file", {"path": "report.pdf"}, **io_,
            )
        assert await consumed_of(session_factory, rig.root, "call_1") == [None]
        asked = await operations_of(session_factory, rig.root)
        file_ops._read_tracker.clear()
        await take_over(store, rig)

        resumed = await resume_call(rig, store, tools, "call_1", "read_file", {"path": "report.pdf"}, **io_)

        assert PDF_TEXT in resumed["content"]
        assert await operations_of(session_factory, rig.root) == asked
        [consumed] = await consumed_of(session_factory, rig.root, "call_1")
        assert consumed is not None
    finally:
        await app.close()
    # The app journaled every operation it was given.
    assert journal_rows(journal) == asked - 1  # the bind was answered by the server-side rig, not the app


def stat_of(rig, folder: Path) -> OperationRequest:
    """A small operation: the folder's stat, which its file host answers."""
    return OperationRequest(**{**_fields(request_for(rig.device_id, rig.root)), "kind": "stat", "args": {"key": str(folder)}})


def read_of(rig, key: str) -> OperationRequest:
    return OperationRequest(**{
        **_fields(request_for(rig.device_id, rig.root)), "kind": "read", "args": {"key": key, "max_bytes": None},
    })


async def under_way(session_factory, device_id: UUID) -> bool:
    """Whether the device's transfer has started: the server took its header."""
    async with session_factory() as db:
        return bool((await db.execute(
            select(func.count()).select_from(DeviceTransfer)
            .join(DeviceOperation, DeviceOperation.id == DeviceTransfer.operation_id)
            .where(DeviceOperation.device_id == device_id)
        )).scalar_one())


async def test_the_link_carries_50_mib_and_answers_small_operations_meanwhile(
    built_client, laptop_rig, link_url, session_factory, journal_dir,
):
    """Throughput, and how long a small operation waits, before the header and on the link.

    Before the header the app reads, decodes, hashes and journals the 50 MiB. Localhost
    cannot exercise backpressure: nothing queues on it, so these bounds hold with or
    without the sender's window and write gate. The sender's unit test in
    desktop/test/transfers.test.ts is what pins those.
    """
    rig = laptop_rig
    folder = journal_dir / "folder"
    folder.mkdir()
    data = os.urandom(MAX_READ_BYTES)
    (folder / "most.bin").write_bytes(data)
    app = await client(built_client, link_url, rig.token, journal_dir / "journal.sqlite", folder=folder)
    try:
        await app.until(connected)
        alone = []
        for _ in range(5):
            started = time.monotonic()
            assert (await asyncio.wait_for(rig.ops.run(stat_of(rig, folder)), 10.0))["ok"]["is_dir"] is True
            alone.append(time.monotonic() - started)

        started = time.monotonic()
        reading = asyncio.create_task(rig.ops.run(read_of(rig, str(folder / "most.bin"))))
        # Each small operation counts in the phase it was asked in.
        before_header, on_link = [], []
        while not reading.done():
            phase = on_link if on_link or await under_way(session_factory, rig.device_id) else before_header
            asked = time.monotonic()
            assert (await asyncio.wait_for(rig.ops.run(stat_of(rig, folder)), 30.0))["ok"]["is_dir"] is True
            phase.append(time.monotonic() - asked)
        outcome = await reading
        elapsed = time.monotonic() - started
    finally:
        await app.close()

    assert outcome["ok"]["transfer"]["size"] == MAX_READ_BYTES
    async with session_factory() as db:
        on_the_link = (await db.execute(
            select(DeviceOperation.completed_at - DeviceTransfer.created_at)
            .join(DeviceTransfer, DeviceTransfer.operation_id == DeviceOperation.id)
            .where(DeviceOperation.device_id == rig.device_id)
        )).scalar_one().total_seconds()
    print(
        f"\n50 MiB read in {elapsed:.2f} s ({MAX_READ_BYTES / 2**20 / elapsed:.1f} MiB/s), "
        f"{on_the_link:.2f} s of it from the header to the last chunk stored; "
        f"a small operation alone: median {sorted(alone)[2] * 1000:.0f} ms; "
        f"before the header: {len(before_header)} answered, slowest {max(before_header, default=0) * 1000:.0f} ms; "
        f"on the link: {len(on_link)} answered, slowest {max(on_link, default=0) * 1000:.0f} ms",
    )
    # Loose bounds: they catch a regression, not this machine's speed.
    assert elapsed < 60
    assert before_header and max(before_header) < 10
    assert on_link and max(on_link) < 10


async def test_a_patch_on_a_5_mib_file_through_the_app_resumes_without_writing_again(
    built_client, laptop_rig, link_url, session_factory, redis_client, journal_dir,
):
    rig = laptop_rig
    folder = journal_dir / "folder"
    folder.mkdir()
    args, patched = big_file(folder)
    journal = journal_dir / "journal.sqlite"
    store, tools = SessionStore(session_factory), builtin_tools()
    io_ = {"redis_client": redis_client, "session_factory": session_factory}
    app = await client(built_client, link_url, rig.token, journal, folder=folder)
    try:
        await app.until(connected)
        with pytest.raises(asyncio.CancelledError):
            await tool_call(rig, StoppingStore(session_factory), tools, "call_1", "patch", args, **io_)
        assert (folder / "big.txt").read_text() == patched
        asked = await operations_of(session_factory, rig.root)
        file_ops._read_tracker.clear()
        await take_over(store, rig)

        resumed = await resume_call(rig, store, tools, "call_1", "patch", args, **io_)

        assert json.loads(resumed["content"])["status"] == "ok", resumed
        # Nothing new was asked of the app: the write was answered from the journal.
        assert await operations_of(session_factory, rig.root) == asked
        assert (folder / "big.txt").read_text() == patched
    finally:
        await app.close()
    assert journal_rows(journal) == asked - 1  # the bind was answered by the server-side rig, not the app


async def test_a_3_mib_tool_result_spills_onto_the_computer_through_the_app(
    built_client, laptop_rig, link_url, session_factory, redis_client, journal_dir,
):
    rig = laptop_rig
    folder = journal_dir / "folder"
    folder.mkdir()
    report = big_text(70_000)
    app = await client(built_client, link_url, rig.token, journal_dir / "journal.sqlite", folder=folder)
    try:
        await app.until(connected)
        result = await tool_call(
            rig, SessionStore(session_factory), reporting(report), "call_1", "big_report", {},
            redis_client=redis_client, session_factory=session_factory,
        )
    finally:
        await app.close()
    assert "Full output saved to: .surogates-results/call_1.txt" in result["content"]
    assert (folder / ".surogates-results" / "call_1.txt").read_text() == report
    # The app writes the folder's ignore file too: no protected name, and not asked about.
    assert (folder / ".surogates-results" / ".gitignore").read_bytes() == b"*\n"


async def test_a_50_mib_write_crosses_the_link_and_small_operations_are_answered_meanwhile(
    built_client, laptop_rig, link_url, journal_dir, caplog,
):
    """The other direction's measurement: from the worker's hash to the file on the computer."""
    rig = laptop_rig
    folder = journal_dir / "folder"
    folder.mkdir()
    data = os.urandom(MAX_WRITE_BYTES)
    app = await client(built_client, link_url, rig.token, journal_dir / "journal.sqlite", folder=folder)
    try:
        await app.until(connected)
        wio = device_io(rig.ops, rig.device_id, rig.root, folder)
        started = time.monotonic()
        with caplog.at_level(logging.INFO, logger="surogates.devices.link"):
            writing = asyncio.create_task(wio.write(str(folder / "most.bin"), data))
            meanwhile = []
            while not writing.done():
                asked = time.monotonic()
                assert (await asyncio.wait_for(rig.ops.run(stat_of(rig, folder)), 30.0))["ok"]["is_dir"] is True
                meanwhile.append(time.monotonic() - asked)
            await writing
        elapsed = time.monotonic() - started
    finally:
        await app.close()

    assert (folder / "most.bin").read_bytes() == data
    # The API's own line for the write's transfer: from its op to the app's last acknowledgement.
    [on_the_link] = [
        float(found.group(1)) for record in caplog.records
        if (found := re.search(r"transfer \S+ sent: \d+ bytes, (\d+\.\d\d) s from its header", record.getMessage()))
    ]
    print(
        f"\n50 MiB write in {elapsed:.2f} s ({MAX_WRITE_BYTES / 2**20 / elapsed:.1f} MiB/s), "
        f"{on_the_link:.2f} s of it on the link, from its op to the last chunk acknowledged; "
        f"meanwhile {len(meanwhile)} small operations answered, slowest {max(meanwhile, default=0) * 1000:.0f} ms",
    )
    # Loose bounds: they catch a regression, not this machine's speed.
    assert elapsed < 60
    assert meanwhile and max(meanwhile) < 10
