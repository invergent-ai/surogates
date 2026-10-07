"""A landing's durable record, and a landing a killed worker left running."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from .test_devices import api  # noqa: F401  (api is a fixture)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_the_history_table_is_made_with_its_indexes(api):
    async with api.app.state.session_factory() as db:
        names = set((await db.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'workstream_history'"))).scalars())
    assert {"idx_workstream_history_workstream", "idx_workstream_history_files", "idx_workstream_history_running"} <= names
