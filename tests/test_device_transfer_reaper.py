"""The worker's device transfer reaper keeps sweeping past a failed pass."""

from __future__ import annotations

import asyncio

import pytest

from surogates.jobs import device_transfers

pytestmark = pytest.mark.asyncio


async def test_the_reaper_goes_on_after_a_pass_that_fails(monkeypatch):
    passes: list[str] = []

    async def reap(session_factory) -> int:
        passes.append(session_factory)
        if len(passes) == 1:
            raise ConnectionError("the database is away")
        if len(passes) == 3:
            raise asyncio.CancelledError
        return 2

    monkeypatch.setattr(device_transfers, "reap_transfers", reap)
    with pytest.raises(asyncio.CancelledError):
        await device_transfers.run_transfer_reaper_loop("factory", interval_seconds=0)
    assert passes == ["factory"] * 3
