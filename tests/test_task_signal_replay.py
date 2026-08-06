"""A coordinator woken by a task signal must SEE that signal on replay.

``notify_parent_of_task_event`` wakes the parent, but a wake only helps if
``_rebuild_messages`` renders the event.  Without a branch the coordinator
replays its log, finds nothing new, and re-runs its previous turn — the
same silent stall the missing enqueue caused.
"""

from __future__ import annotations

from types import SimpleNamespace

from surogates.harness.loop import AgentHarness
from surogates.session.events import EventType


def _replay(event_type: EventType, data: dict) -> str:
    event = SimpleNamespace(type=event_type.value, data=data, id=1)
    messages = AgentHarness._rebuild_messages(SimpleNamespace(), [event])
    assert len(messages) == 1, f"{event_type.value} rendered {len(messages)} messages"
    assert messages[0]["role"] == "user"
    return messages[0]["content"]


def test_task_blocked_is_visible_to_the_coordinator() -> None:
    content = _replay(
        EventType.TASK_BLOCKED,
        {"task_id": "t-1", "worker_id": "w-1", "reason": "rate limit key unclear"},
    )
    assert "t-1" in content
    assert "rate limit key unclear" in content


def test_task_failed_is_visible_to_the_coordinator() -> None:
    content = _replay(
        EventType.TASK_FAILED, {"task_id": "t-2", "attempt_count": 3},
    )
    assert "t-2" in content
    assert "3" in content


def test_task_signals_survive_a_payload_missing_its_optional_fields() -> None:
    # _block_claim emits no worker_id; the dispatcher emits no reason.
    assert _replay(EventType.TASK_BLOCKED, {"task_id": "t-3"})
    assert _replay(EventType.TASK_FAILED, {"task_id": "t-4"})
