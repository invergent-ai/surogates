from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.harness.loop_artifact_completion import ArtifactCompletionMixin
from surogates.session.store import SessionNotFoundError


def _session(*, channel: str, parent_id=None, config=None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-a",
        channel=channel,
        status="active",
        parent_id=parent_id,
        title="Loop run",
        config=config or {},
        created_at=now,
        updated_at=now,
        task_id=None,
    )


class _ParentStore:
    def __init__(self, parent=None):
        self.parent = parent

    async def get_session(self, session_id):
        if self.parent is not None and session_id == self.parent.id:
            return self.parent
        raise SessionNotFoundError(str(session_id))


def _harness(store):
    host = type("_Harness", (ArtifactCompletionMixin,), {})()
    host._store = store
    return host


@pytest.mark.parametrize("parent_channel", ["web", "api"])
async def test_resolves_web_and_api_loop_parent(parent_channel):
    parent = _session(channel=parent_channel)
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )

    assert await _harness(_ParentStore(parent))._resolve_loop_result_parent(child) is parent


@pytest.mark.parametrize("parent_channel", ["slack", "telegram", "teams", "ambient"])
async def test_skips_channel_and_private_parents(parent_channel):
    parent = _session(channel=parent_channel)
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )

    assert await _harness(_ParentStore(parent))._resolve_loop_result_parent(child) is None


async def test_skips_detached_scheduled_run():
    child = _session(
        channel="scheduled",
        parent_id=None,
        config={"scheduled_session_id": str(uuid4())},
    )

    assert await _harness(_ParentStore())._resolve_loop_result_parent(child) is None


async def test_skips_missing_parent():
    child = _session(
        channel="scheduled",
        parent_id=uuid4(),
        config={"scheduled_session_id": str(uuid4())},
    )

    assert await _harness(_ParentStore())._resolve_loop_result_parent(child) is None


async def test_accepts_legacy_scheduled_run_marker_even_if_channel_drifted():
    parent = _session(channel="web")
    child = _session(
        channel="api",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )

    assert await _harness(_ParentStore(parent))._resolve_loop_result_parent(child) is parent
