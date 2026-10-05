"""Reads too large for one frame, through the real desktop app over the real link."""

from __future__ import annotations

import asyncio
import os
import time
from uuid import UUID

import pytest
from sqlalchemy import func, select

from surogates.db.models import DeviceOperation, DeviceTransfer
from surogates.devices.operations import OperationRequest
from surogates.devices.workspace import MAX_READ_BYTES

from .test_desktop_file_operations import journal_dir  # noqa: F401  (fixture)
from .test_desktop_link_client import built_client, client, connected  # noqa: F401  (fixture)
from .test_devices import (  # noqa: F401  (fixtures)
    _fields,
    api,
    laptop_rig,
    link_url,
    request_for,
)

pytestmark = [pytest.mark.desktop, pytest.mark.asyncio(loop_scope="session")]


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
            assert await asyncio.wait_for(rig.ops.run(request_for(rig.device_id, rig.root)), 10.0) == {"ok": True}
            alone.append(time.monotonic() - started)

        started = time.monotonic()
        reading = asyncio.create_task(rig.ops.run(read_of(rig, str(folder / "most.bin"))))
        while not reading.done() and not await under_way(session_factory, rig.device_id):
            await asyncio.sleep(0.01)
        meanwhile = []
        while not reading.done():
            asked = time.monotonic()
            assert await asyncio.wait_for(rig.ops.run(request_for(rig.device_id, rig.root)), 30.0) == {"ok": True}
            meanwhile.append(time.monotonic() - asked)
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
        f"during the transfer: {len(meanwhile)} answered, slowest {max(meanwhile) * 1000:.0f} ms",
    )
    # Loose bounds: they catch a regression, not this machine's speed.
    assert elapsed < 60
    assert meanwhile and max(meanwhile) < 10
