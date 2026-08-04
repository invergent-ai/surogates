"""Which completed sessions raise a ``task_complete`` inbox item.

The item exists to tell a person about work they did not watch, and the
only thing that retires it is opening that session's conversation (the
chat surfaces delete it on open; the store suppresses it outright while
someone is watching). A session with no such conversation therefore mints
an item nothing can ever clear — which is how one agent accumulated
hundreds of unread "Task complete" rows from subagents and API runs.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.harness.loop_artifact_completion import ArtifactCompletionMixin
from surogates.session.events import EventType
from surogates.session.inbox_payload import raises_completion_inbox_item


def _session(*, channel: str, parent_id=None, config=None, task_id=None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-a",
        channel=channel,
        status="active",
        parent_id=parent_id,
        title="A task",
        config=config or {},
        created_at=now,
        updated_at=now,
        task_id=task_id,
    )


class _RecordingStore:
    """Just enough store for ``_complete_session`` to run end to end."""

    def __init__(self):
        self.events: list[tuple[EventType, dict]] = []
        self.next_event_id = 100
        self.statuses: list[str] = []
        self.cursor_targets: list[int] = []

    async def emit_event(self, session_id, event_type, data):
        self.events.append((event_type, data))
        self.next_event_id += 1
        return self.next_event_id

    async def get_events(self, session_id):
        return []

    async def update_session_status(self, session_id, status):
        self.statuses.append(status)

    async def advance_harness_cursor(self, session_id, target, lease_token):
        self.cursor_targets.append(target)

    async def get_session(self, session_id):
        from surogates.session.store import SessionNotFoundError

        raise SessionNotFoundError(str(session_id))


def _harness(store):
    host = type("_Harness", (ArtifactCompletionMixin,), {})()
    host._store = store
    host._sandbox_pool = None
    host._memory_manager = None
    host._turn_summarizer = None
    host._worker_id = "worker-1"
    host._redis = None
    host._session_factory = None
    host._tenant = SimpleNamespace(user_id=uuid4(), service_account_id=None)
    return host


async def _complete(session):
    store = _RecordingStore()
    await _harness(store)._complete_session(
        session,
        [{"role": "assistant", "content": "done"}],
        SimpleNamespace(lease_token="lease-1"),
        reason="stop",
    )
    return store


def _kinds(store) -> list[EventType]:
    return [event_type for event_type, _ in store.events]


# --- the predicate ---------------------------------------------------------


@pytest.mark.parametrize("channel", ["web", "website"])
def test_root_conversation_channels_notify(channel):
    assert raises_completion_inbox_item(_session(channel=channel)) is True


@pytest.mark.parametrize(
    "channel", ["api", "slack", "telegram", "whatsapp", "ambient", "scheduled"]
)
def test_channels_without_an_inbox_surface_do_not_notify(channel):
    # api/ambient/scheduled runs have no conversation a person opens; the
    # messaging channels deliver the answer in the conversation itself.
    assert raises_completion_inbox_item(_session(channel=channel)) is False


@pytest.mark.parametrize("channel", ["worker", "task", "delegation", "web"])
def test_child_sessions_never_notify(channel):
    # A delegated child's result reaches its coordinator through
    # WORKER_COMPLETE; the human reads it in the parent conversation.
    assert (
        raises_completion_inbox_item(_session(channel=channel, parent_id=uuid4()))
        is False
    )


# --- the emission itself ---------------------------------------------------


async def test_web_root_session_emits_the_inbox_item():
    store = await _complete(_session(channel="web"))
    assert EventType.INBOX_TASK_COMPLETE in _kinds(store)


@pytest.mark.parametrize(
    "session",
    [
        _session(channel="worker", parent_id=uuid4()),
        _session(channel="task", parent_id=uuid4(), task_id=uuid4()),
        _session(channel="delegation", parent_id=uuid4()),
        _session(channel="api"),
        _session(channel="slack"),
    ],
    ids=["worker", "task", "delegation", "api", "slack"],
)
async def test_sessions_without_an_inbox_surface_emit_no_item(session):
    store = await _complete(session)
    assert EventType.INBOX_TASK_COMPLETE not in _kinds(store)
    # The session still completes and is still marked done — only the
    # notification is dropped.
    assert EventType.SESSION_COMPLETE in _kinds(store)
    assert store.statuses == ["completed"]


async def test_scheduled_run_with_a_channel_parent_still_announces():
    # Scheduled runs are unwatched by construction: the gate must not
    # swallow the one notification a loop run produces. (A web/api parent
    # takes the inline loop.result path instead — covered in
    # test_loop_result_inline_delivery.)
    store = await _complete(
        _session(
            channel="scheduled",
            parent_id=uuid4(),
            config={"scheduled_session_id": str(uuid4())},
        )
    )
    assert EventType.INBOX_TASK_COMPLETE in _kinds(store)


async def test_cursor_advances_past_completion_without_an_inbox_item():
    # The cursor used to ride on the inbox event id; with no item emitted
    # it must fall back to the session-complete event or the worker
    # replays the completion forever.
    store = await _complete(_session(channel="api"))
    assert store.cursor_targets == [store.next_event_id]
