"""Archived-session workspace cleanup must not wipe shared boundary workspaces.

A managed-channel session's workspace lives under the per-channel boundary
prefix, shared with every other thread/session in that channel. Deleting one
session must therefore NOT delete-prefix the shared boundary workspace, or a
sibling's live files vanish. Non-boundary (web/api) sessions keep the old
per-session cleanup.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.api.routes.sessions import _cleanup_archived_workspaces


class _RecordingStorage:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        self.deleted.append((bucket, prefix))
        return 0


def _session(*, channel: str, config: dict):
    return SimpleNamespace(id=uuid4(), channel=channel, config=config)


@pytest.mark.asyncio
async def test_cleanup_retains_shared_boundary_workspace():
    storage = _RecordingStorage()
    session = _session(
        channel="slack",
        config={
            "storage_bucket": "agent-bucket",
            "storage_key_prefix": "project/agent",
            "memory_boundary": "slack:c:G1",
            "workspace_boundary": "slack:c:G1",
        },
    )

    await _cleanup_archived_workspaces(storage, [session])

    # The shared boundary workspace outlives any one session — no delete.
    assert storage.deleted == []


@pytest.mark.asyncio
async def test_cleanup_deletes_non_boundary_session_workspace():
    storage = _RecordingStorage()
    sid = uuid4()
    session = SimpleNamespace(
        id=sid,
        channel="web",
        config={
            "storage_bucket": "agent-bucket",
            "storage_key_prefix": "project/agent",
        },
    )

    await _cleanup_archived_workspaces(storage, [session])

    assert storage.deleted == [
        ("agent-bucket", f"project/agent/{sid}/")
    ]


@pytest.mark.asyncio
async def test_cleanup_skips_session_without_bucket():
    storage = _RecordingStorage()
    session = _session(channel="web", config={})

    await _cleanup_archived_workspaces(storage, [session])

    assert storage.deleted == []
