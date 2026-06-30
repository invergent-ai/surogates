import uuid

import pytest

from surogates.ambient.ticker import AmbientTicker
from surogates.ambient.store import AmbientSchedule


class FakeStore:
    def __init__(self, rows): self._rows = rows; self.claims = 0
    async def claim_due(self, *, worker_id, limit, lease_seconds=120):
        self.claims += 1
        return self._rows if self.claims == 1 else []


def _row():
    return AmbientSchedule(
        id=uuid.uuid4(), org_id=uuid.uuid4(), agent_id="ag", platform="slack",
        channel_id="C1", cadence_seconds=1800, status="active",
    )


@pytest.mark.asyncio
async def test_tick_once_materializes_each_due_row():
    materialized = []
    async def fake_mat(row): materialized.append(row.channel_id)
    ticker = AmbientTicker(
        FakeStore([_row(), _row()]), redis=None, materialize=fake_mat, worker_id="w1",
    )
    await ticker.tick_once()
    assert materialized == ["C1", "C1"]


@pytest.mark.asyncio
async def test_tick_once_isolates_one_bad_row():
    calls = []
    async def fake_mat(row):
        calls.append(row.channel_id)
        if len(calls) == 1:
            raise RuntimeError("boom")
    ticker = AmbientTicker(
        FakeStore([_row(), _row()]), redis=None, materialize=fake_mat, worker_id="w1",
    )
    await ticker.tick_once()  # must not raise
    assert len(calls) == 2  # second row still processed
