"""A channel /stop that actually halts a turn emits a deliverable confirmation.

The inbound handler only posts an optimistic "Stopping…" ack. The terminal
"stopped" confirmation is emitted from the harness abort point
(:meth:`AgentHarness._abort_iteration_with_pause`) once the turn has really
halted — but only for a channel /stop (reason ``channel_stop``), not for a
REST /pause or a lease-loss interrupt.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from surogates.harness.loop import AgentHarness
from surogates.session.events import EventType


class _RecordingStore:
    def __init__(self, status: str) -> None:
        self._status = status
        self.emitted: list[tuple[uuid.UUID, EventType, dict]] = []

    async def get_session(self, session_id):
        return SimpleNamespace(id=session_id, status=self._status)

    async def emit_event(self, session_id, event_type, data):
        self.emitted.append((session_id, event_type, data))
        return len(self.emitted)


def _harness(store: _RecordingStore, *, interrupt_message: str) -> AgentHarness:
    h = AgentHarness.__new__(AgentHarness)
    h._store = store
    h._worker_id = "test-worker"
    h._sandbox_pool = None
    h._interrupt_requested = True
    h._interrupt_message = interrupt_message
    return h


@pytest.mark.asyncio
async def test_channel_stop_emits_stopped_confirmation():
    store = _RecordingStore(status="active")
    h = _harness(store, interrupt_message="channel_stop")
    session = SimpleNamespace(id=uuid.uuid4(), status="active")

    await h._abort_iteration_with_pause(session, saga=None)

    types = [t for (_, t, _) in store.emitted]
    assert EventType.SESSION_STOPPED in types
    # active session → no SESSION_PAUSE (that is the /pause REST path only).
    assert EventType.SESSION_PAUSE not in types


@pytest.mark.asyncio
async def test_rest_pause_does_not_emit_stopped_confirmation():
    # A REST /pause sets status='paused' and uses its own reason string; it must
    # emit SESSION_PAUSE but never the channel SESSION_STOPPED confirmation.
    store = _RecordingStore(status="paused")
    h = _harness(store, interrupt_message="Session paused by user")
    session = SimpleNamespace(id=uuid.uuid4(), status="paused")

    await h._abort_iteration_with_pause(session, saga=None)

    types = [t for (_, t, _) in store.emitted]
    assert EventType.SESSION_STOPPED not in types
    assert EventType.SESSION_PAUSE in types
