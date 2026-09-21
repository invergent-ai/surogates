"""The cleanup CronJob deletes workspaces whose session row is gone."""

from __future__ import annotations

import uuid

import pytest

from surogates.db.models import Session
from surogates.jobs.platform_cleanup import sweep_orphan_workspaces

pytestmark = pytest.mark.asyncio

BUCKET = "surogate-workspaces"


class _Storage:
    def __init__(self, keys: list[str]) -> None:
        self.keys = list(keys)

    async def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        assert bucket == BUCKET
        return [k for k in self.keys if k.startswith(prefix)]

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        matched = [k for k in self.keys if k.startswith(prefix)]
        self.keys = [k for k in self.keys if not k.startswith(prefix)]
        return len(matched)


async def _add_session(
    sf, session_id: uuid.UUID, *, status: str = "completed",
) -> None:
    async with sf() as db:
        db.add(
            Session(
                id=session_id,
                org_id=uuid.uuid4(),
                agent_id="support-bot",
                channel="api",
                status=status,
                config={},
            ),
        )
        await db.commit()


async def test_sweep_deletes_a_deleted_sessions_leftover_workspace(sf):
    live = uuid.uuid4()
    deleted = uuid.uuid4()
    await _add_session(sf, live)
    await _add_session(sf, deleted, status="archived")
    storage = _Storage([
        f"{live}/notes.md",
        f"{deleted}/report.csv",
        f"{deleted}/out/summary.json",
    ])

    outcome = await sweep_orphan_workspaces(
        storage=storage, session_factory=sf, bucket=BUCKET,
    )

    assert outcome == {
        "scanned": 2, "orphans": 1, "objects": 2, "errors": 0,
    }
    assert storage.keys == [f"{live}/notes.md"]


async def test_sweep_deletes_a_workspace_with_no_session_row(sf):
    orphan = uuid.uuid4()
    storage = _Storage([f"{orphan}/report.csv"])

    outcome = await sweep_orphan_workspaces(
        storage=storage, session_factory=sf, bucket=BUCKET,
    )

    assert outcome["orphans"] == 1
    assert storage.keys == []


async def test_sweep_leaves_non_session_prefixes_alone(sf):
    orphan = uuid.uuid4()
    storage = _Storage([
        "boundaries/slack:c:C123/workspace/shared.md",
        f"{orphan}/scratch.txt",
    ])

    outcome = await sweep_orphan_workspaces(
        storage=storage, session_factory=sf, bucket=BUCKET,
    )

    assert outcome["scanned"] == 1
    assert storage.keys == ["boundaries/slack:c:C123/workspace/shared.md"]


async def test_sweep_survives_a_failing_delete(sf):
    first, second = sorted([uuid.uuid4(), uuid.uuid4()])
    storage = _Storage([f"{first}/a.txt", f"{second}/b.txt"])
    failing = storage.delete_prefix

    async def _delete_prefix(bucket: str, prefix: str) -> int:
        if prefix.startswith(str(first)):
            raise RuntimeError("bucket said no")
        return await failing(bucket, prefix)

    storage.delete_prefix = _delete_prefix

    outcome = await sweep_orphan_workspaces(
        storage=storage, session_factory=sf, bucket=BUCKET,
    )

    assert outcome["errors"] == 1
    assert outcome["objects"] == 1
    assert storage.keys == [f"{first}/a.txt"]
