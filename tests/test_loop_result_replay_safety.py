from types import SimpleNamespace
from uuid import uuid4

from surogates.harness.loop_context_replay import ContextReplayMixin
from surogates.session.events import EventType
from surogates.session.store import _DELIVERABLE_EVENTS


def _event(event_type: EventType, data: dict, event_id: int = 1):
    return SimpleNamespace(
        id=event_id,
        session_id=uuid4(),
        type=event_type.value,
        data=data,
    )


def test_loop_result_event_type_exists():
    assert EventType.LOOP_RESULT.value == "loop.result"


def test_loop_result_is_not_a_deliverable_event():
    assert EventType.LOOP_RESULT not in _DELIVERABLE_EVENTS


def test_rebuild_messages_ignores_loop_result():
    host = type("_ReplayHost", (ContextReplayMixin,), {})()
    rebuilt = host._rebuild_messages([
        _event(
            EventType.LLM_RESPONSE,
            {"message": {"role": "assistant", "content": "normal reply"}},
            1,
        ),
        _event(EventType.LOOP_RESULT, {"content": "scheduled output"}, 2),
    ])

    joined = "\n".join(str(m.get("content", "")) for m in rebuilt)
    assert "normal reply" in joined
    assert "scheduled output" not in joined
