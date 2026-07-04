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


from surogates.session.events import EventType


def _llm_response(content: str):
    return SimpleNamespace(
        type=EventType.LLM_RESPONSE.value,
        data={"message": {"role": "assistant", "content": content}},
    )


class _RecordingStore(_ParentStore):
    def __init__(self, parent=None, child_events=None, fail_loop_result=False):
        super().__init__(parent)
        self.child_events = list(child_events or [])
        self.fail_loop_result = fail_loop_result
        self.emitted = []
        self.status_updates = []
        self.cursor_advancements = []
        self.next_event_id = 100

    async def get_events(self, session_id):
        return list(self.child_events)

    async def emit_event(self, session_id, event_type, data):
        if event_type == EventType.LOOP_RESULT and self.fail_loop_result:
            raise RuntimeError("boom")
        self.next_event_id += 1
        self.emitted.append((self.next_event_id, session_id, event_type, data))
        return self.next_event_id

    async def update_session_status(self, session_id, status):
        self.status_updates.append((session_id, status))

    async def advance_harness_cursor(self, session_id, cursor, lease_token):
        self.cursor_advancements.append((session_id, cursor, lease_token))


def _completion_harness(store):
    host = _harness(store)
    host._worker_id = "worker-1"
    host._sandbox_pool = None
    host._memory_manager = None
    host._turn_summarizer = None
    host._redis = None
    host._session_factory = None
    return host


async def _complete(host, child, *, messages=None, reason="stop", through_event_id=None):
    await host._complete_session(
        child,
        messages=messages if messages is not None else [{"role": "assistant", "content": "done"}],
        lease=SimpleNamespace(lease_token="lease-1"),
        reason=reason,
        through_event_id=through_event_id,
    )


def _types(store):
    return [event_type for _, _, event_type, _ in store.emitted]


async def test_web_parent_gets_loop_result_and_inbox_is_suppressed():
    schedule_id = uuid4()
    parent = _session(channel="web")
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(schedule_id)},
    )
    store = _RecordingStore(parent, [_llm_response("Full final answer")])

    await _complete(_completion_harness(store), child)

    loop_rows = [row for row in store.emitted if row[2] == EventType.LOOP_RESULT]
    assert len(loop_rows) == 1
    _, emitted_session_id, _, payload = loop_rows[0]
    assert emitted_session_id == parent.id
    assert payload["run_session_id"] == str(child.id)
    assert payload["scheduled_session_id"] == str(schedule_id)
    assert payload["content"] == "Full final answer"
    assert payload["outcome"] == "success"
    assert isinstance(payload["duration_seconds"], int)
    assert payload["run_completed_at"]
    assert EventType.INBOX_TASK_COMPLETE not in _types(store)


async def test_api_parent_gets_loop_result_and_inbox_is_suppressed():
    parent = _session(channel="api")
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )
    store = _RecordingStore(parent, [_llm_response("API final answer")])

    await _complete(_completion_harness(store), child)

    assert EventType.LOOP_RESULT in _types(store)
    assert EventType.INBOX_TASK_COMPLETE not in _types(store)


async def test_channel_parent_keeps_existing_inbox_completion_and_no_loop_result():
    parent = _session(channel="slack")
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )
    store = _RecordingStore(parent, [_llm_response("Channel final answer")])

    await _complete(_completion_harness(store), child)

    assert EventType.LOOP_RESULT not in _types(store)
    assert EventType.INBOX_TASK_COMPLETE in _types(store)


async def test_empty_final_response_suppresses_inbox_but_emits_no_loop_result():
    parent = _session(channel="web")
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )
    store = _RecordingStore(parent, child_events=[])

    await _complete(_completion_harness(store), child, messages=[])

    assert EventType.LOOP_RESULT not in _types(store)
    assert EventType.INBOX_TASK_COMPLETE not in _types(store)
    assert store.status_updates == [(child.id, "completed")]


async def test_loop_result_failure_does_not_abort_completion_and_still_suppresses_inbox():
    parent = _session(channel="web")
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )
    store = _RecordingStore(parent, [_llm_response("Final")], fail_loop_result=True)

    await _complete(_completion_harness(store), child)

    assert EventType.INBOX_TASK_COMPLETE not in _types(store)
    assert store.status_updates == [(child.id, "completed")]
    assert store.cursor_advancements


async def test_cursor_uses_child_session_complete_when_inbox_is_suppressed():
    parent = _session(channel="web")
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )
    store = _RecordingStore(parent, [_llm_response("Final")])

    await _complete(_completion_harness(store), child)

    session_complete_id = next(
        event_id for event_id, _, event_type, _ in store.emitted
        if event_type == EventType.SESSION_COMPLETE
    )
    assert store.cursor_advancements == [(child.id, session_complete_id, "lease-1")]


async def test_explicit_through_event_id_still_wins_for_cursor():
    parent = _session(channel="web")
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )
    store = _RecordingStore(parent, [_llm_response("Final")])

    await _complete(_completion_harness(store), child, through_event_id=77)

    assert store.cursor_advancements == [(child.id, 77, "lease-1")]
