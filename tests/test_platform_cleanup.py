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
    sf, session_id: uuid.UUID, *, status: str = "completed", config: dict | None = None,
) -> None:
    async with sf() as db:
        db.add(
            Session(
                id=session_id,
                org_id=uuid.uuid4(),
                agent_id="support-bot",
                channel="api",
                status=status,
                config=config or {},
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


async def test_sweep_keeps_a_folder_with_no_session_row(sf):
    unknown = uuid.uuid4()
    storage = _Storage([f"{unknown}/report.csv"])

    outcome = await sweep_orphan_workspaces(
        storage=storage, session_factory=sf, bucket=BUCKET,
    )

    assert outcome["orphans"] == 0
    assert storage.keys == [f"{unknown}/report.csv"]


async def test_sweep_keeps_live_workspaces_under_a_key_prefix_that_starts_with_a_uuid(sf):
    project, live = uuid.uuid4(), uuid.uuid4()
    above = f"{project}/agent-a"
    await _add_session(sf, live, config={"storage_key_prefix": above})
    storage = _Storage([f"{above}/{live}/notes.md"])

    outcome = await sweep_orphan_workspaces(
        storage=storage, session_factory=sf, bucket=BUCKET,
    )

    assert outcome["orphans"] == 0
    assert storage.keys == [f"{above}/{live}/notes.md"]


@pytest.mark.parametrize("above", ["project-a/agent-a", "7d3f0c52-5a51-4f0e-9d3c-2b8f6a1e4c90/agent-a"])
async def test_sweep_reclaims_a_deleted_session_under_its_own_key_prefix(sf, above):
    live, deleted = uuid.uuid4(), uuid.uuid4()
    await _add_session(sf, live, config={"storage_key_prefix": above})
    await _add_session(sf, deleted, status="archived", config={"storage_key_prefix": above, "storage_bucket": BUCKET})
    storage = _Storage([f"{above}/{live}/notes.md", f"{above}/{deleted}/report.csv"])

    outcome = await sweep_orphan_workspaces(
        storage=storage, session_factory=sf, bucket=BUCKET,
    )

    assert outcome["orphans"] == 1
    assert storage.keys == [f"{above}/{live}/notes.md"]


async def test_sweep_keeps_a_folder_named_like_a_deleted_session_elsewhere(sf):
    live, deleted = uuid.uuid4(), uuid.uuid4()
    await _add_session(sf, live)
    await _add_session(sf, deleted, status="archived")
    storage = _Storage([f"{live}/{deleted}/a user's folder.txt"])

    outcome = await sweep_orphan_workspaces(
        storage=storage, session_factory=sf, bucket=BUCKET,
    )

    assert outcome["orphans"] == 0
    assert len(storage.keys) == 1


async def test_sweep_keeps_a_deleted_session_of_another_bucket(sf):
    deleted = uuid.uuid4()
    await _add_session(sf, deleted, status="archived", config={"storage_bucket": "another-bucket"})
    storage = _Storage([f"{deleted}/report.csv"])

    outcome = await sweep_orphan_workspaces(
        storage=storage, session_factory=sf, bucket=BUCKET,
    )

    assert outcome["orphans"] == 0


async def test_sweep_keeps_an_archived_session_with_a_boundary(sf):
    kept = uuid.uuid4()
    await _add_session(sf, kept, status="archived", config={"workspace_boundary": "workstream:p1"})
    storage = _Storage([f"{kept}/report.csv"])

    outcome = await sweep_orphan_workspaces(
        storage=storage, session_factory=sf, bucket=BUCKET,
    )

    assert outcome["orphans"] == 0


async def test_sweep_keeps_a_deleted_root_a_live_child_works_in(sf):
    root, child = uuid.uuid4(), uuid.uuid4()
    await _add_session(sf, root, status="archived")
    await _add_session(sf, child, config={"sandbox_root_session_id": str(root)})
    storage = _Storage([f"{root}/report.csv"])

    outcome = await sweep_orphan_workspaces(
        storage=storage, session_factory=sf, bucket=BUCKET,
    )

    assert outcome["orphans"] == 0


async def test_sweep_keeps_a_folder_inside_another_sessions_workspace_whatever_a_row_says(sf):
    live, deleted = uuid.uuid4(), uuid.uuid4()
    await _add_session(sf, live)
    await _add_session(sf, deleted, status="archived", config={"storage_key_prefix": str(live)})
    storage = _Storage([f"{live}/{deleted}/a user's folder.txt"])

    outcome = await sweep_orphan_workspaces(
        storage=storage, session_factory=sf, bucket=BUCKET,
    )

    assert outcome["orphans"] == 0
    assert len(storage.keys) == 1


async def test_sweep_keeps_a_prefix_that_holds_another_sessions_key_prefix(sf):
    live, deleted = uuid.uuid4(), uuid.uuid4()
    above = f"{deleted}/agent-a"
    await _add_session(sf, deleted, status="archived")
    await _add_session(sf, live, config={"storage_key_prefix": above})
    storage = _Storage([f"{above}/{live}/notes.md"])

    outcome = await sweep_orphan_workspaces(
        storage=storage, session_factory=sf, bucket=BUCKET,
    )

    assert outcome["orphans"] == 0
    assert storage.keys == [f"{above}/{live}/notes.md"]


async def test_sweep_leaves_non_session_prefixes_alone(sf):
    deleted = uuid.uuid4()
    await _add_session(sf, deleted, status="archived")
    storage = _Storage([
        "boundaries/slack:c:C123/workspace/shared.md",
        f"{deleted}/scratch.txt",
    ])

    outcome = await sweep_orphan_workspaces(
        storage=storage, session_factory=sf, bucket=BUCKET,
    )

    assert outcome["scanned"] == 1
    assert storage.keys == ["boundaries/slack:c:C123/workspace/shared.md"]


async def test_sweep_survives_a_failing_delete(sf):
    first, second = sorted([uuid.uuid4(), uuid.uuid4()])
    await _add_session(sf, first, status="archived")
    await _add_session(sf, second, status="archived")
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
