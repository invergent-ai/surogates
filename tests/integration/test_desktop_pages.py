"""A 50 MiB text file read page by page through the real desktop app over the real link."""

from __future__ import annotations

import asyncio
import json
import statistics
import time
import tracemalloc

import pytest
from sqlalchemy import func, select

from surogates.db.models import DeviceOperation, DeviceTransfer
from surogates.devices.workspace import MAX_READ_BYTES
from surogates.session.store import SessionStore
from surogates.tools.builtin import file_ops

from .test_desktop_file_operations import PAGE, journal_dir, on_app  # noqa: F401  (fixture)
from .test_desktop_link_client import built_client, client, connected  # noqa: F401  (fixture)
from .test_devices import (  # noqa: F401  (fixtures)
    api,
    builtin_tools,
    laptop_rig,
    link_url,
    tool_call,
)

pytestmark = [pytest.mark.desktop, pytest.mark.asyncio(loop_scope="session")]

LINE = "2026-10-05T12:00:00.000Z INFO request {:09d} handled in 12 ms path=/api/v1/things\n"


class Stalls:
    """The longest the event loop went without running a 1 ms ticker: how long something held it."""

    def __init__(self) -> None:
        self.longest = 0.0
        self._task: asyncio.Task | None = None

    async def _tick(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            before = loop.time()
            await asyncio.sleep(0.001)
            self.longest = max(self.longest, loop.time() - before - 0.001)

    def __enter__(self) -> Stalls:
        self._task = asyncio.get_running_loop().create_task(self._tick())
        return self

    def __exit__(self, *exc) -> None:
        assert self._task is not None
        self._task.cancel()


async def round_trip(rig, kind: str, args: dict) -> float:
    started = time.monotonic()
    assert "ok" in await on_app(rig, kind, args)
    return time.monotonic() - started


async def test_a_50_mib_log_is_read_page_by_page_through_the_app(
    built_client, laptop_rig, link_url, session_factory, redis_client, journal_dir,
):
    """Each page moves the file's head and the page, never the file: the worker's loop and memory, and the app's scan."""
    rig = laptop_rig
    folder = journal_dir / "folder"
    folder.mkdir()
    count = MAX_READ_BYTES // len(LINE.format(0))
    (folder / "big.log").write_text("".join(LINE.format(n) for n in range(count)))
    store, tools = SessionStore(session_factory), builtin_tools()
    io_ = {"redis_client": redis_client, "session_factory": session_factory}
    app = await client(built_client, link_url, rig.token, journal_dir / "journal.sqlite", folder=folder)
    pages = {"first": 1, "middle": count // 2, "last": count - 99}
    measured = {}
    try:
        await app.until(connected)
        for name, offset in pages.items():
            file_ops._read_tracker.clear()
            started = time.monotonic()
            with Stalls() as stalls:
                read = await tool_call(rig, store, tools, f"call_{name}", "read_file", {"path": "big.log", "offset": offset}, **io_)
            elapsed = time.monotonic() - started
            shown = json.loads(read["content"])
            assert shown["content"].startswith(LINE.format(offset - 1)), shown
            assert shown["total_lines"] == count
            # Read again for its memory: tracemalloc slows what it traces.
            file_ops._read_tracker.clear()
            tracemalloc.start()
            try:
                await tool_call(rig, store, tools, f"call_{name}_memory", "read_file", {"path": "big.log", "offset": offset}, **io_)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            # The app's own time: a page against a small operation, each asked alone.
            small = statistics.median([await round_trip(rig, "stat", {"key": str(folder)}) for _ in range(5)])
            paged = statistics.median([
                await round_trip(rig, "read_lines", {"key": str(folder / "big.log"), **PAGE, "offset": offset})
                for _ in range(5)
            ])
            measured[name] = (elapsed, stalls.longest, peak, paged, small)
    finally:
        await app.close()

    async with session_factory() as db:
        transfers = (await db.execute(
            select(func.count()).select_from(DeviceTransfer)
            .join(DeviceOperation, DeviceOperation.id == DeviceTransfer.operation_id)
            .where(DeviceOperation.root_session_id == rig.root)
        )).scalar_one()
        outcomes = (await db.execute(
            select(DeviceOperation.kind, DeviceOperation.outcome).where(DeviceOperation.root_session_id == rig.root)
        )).all()
    largest = max(len(json.dumps(outcome)) for kind, outcome in outcomes if kind != "bind")
    for name, (elapsed, stall, peak, paged, small) in measured.items():
        print(
            f"\n{name} page of a 50 MiB log: {elapsed * 1000:.0f} ms; the worker's loop held at most {stall * 1000:.0f} ms; "
            f"tracemalloc peak {peak / 2**20:.1f} MiB; read_lines on the app {paged * 1000:.0f} ms, a stat {small * 1000:.0f} ms",
        )
    print(f"largest outcome journaled: {largest} characters; transfers: {transfers}")
    # Nothing but the head and the pages crossed the link.
    assert transfers == 0
    assert largest < 300_000
    # Loose bounds: they catch the whole file coming back, not this machine's speed.
    for elapsed, stall, peak, paged, _small in measured.values():
        assert peak < 32 * 2**20
        assert stall < 0.5
        assert elapsed < 10
        assert paged < 5
