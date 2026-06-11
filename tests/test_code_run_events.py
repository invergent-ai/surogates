"""Unit tests for CODE_RUN_* event types + context replay mapping."""

from __future__ import annotations

from types import SimpleNamespace

from surogates.session.events import EventType


def test_code_run_event_values():
    assert EventType.CODE_RUN_STARTED.value == "code.run_started"
    assert EventType.CODE_RUN_PROGRESS.value == "code.run_progress"
    assert EventType.CODE_RUN_RESULT.value == "code.run_result"


class _ReplayHost:
    """Minimal host exposing only the replay mixin under test."""


def _event(etype: EventType, data: dict, eid: int):
    return SimpleNamespace(type=etype.value, data=data, id=eid)


def _rebuild(events):
    from surogates.harness.loop_context_replay import ContextReplayMixin

    host = type("_H", (ContextReplayMixin,), {})()
    return host._rebuild_messages(events)


def test_code_run_result_replayed_as_user_message():
    events = [
        _event(EventType.USER_MESSAGE, {"content": "/code claude \"fix\""}, 1),
        _event(
            EventType.CODE_RUN_RESULT,
            {"agent": "claude", "final_message": "Fixed the build."},
            2,
        ),
    ]
    messages = _rebuild(events)
    joined = "\n".join(m["content"] for m in messages if m.get("role") == "user")
    assert "/code claude finished" in joined
    assert "Fixed the build." in joined


def test_code_run_result_error_replayed():
    events = [
        _event(EventType.USER_MESSAGE, {"content": "/code codex \"go\""}, 1),
        _event(
            EventType.CODE_RUN_RESULT,
            {"agent": "codex", "error": "pod reclaimed mid-run"},
            2,
        ),
    ]
    messages = _rebuild(events)
    joined = "\n".join(m["content"] for m in messages if m.get("role") == "user")
    assert "/code codex failed" in joined
    assert "pod reclaimed mid-run" in joined


def test_code_run_progress_not_replayed():
    events = [
        _event(EventType.USER_MESSAGE, {"content": "/code claude \"x\""}, 1),
        _event(EventType.CODE_RUN_PROGRESS, {"chunk": "thinking..."}, 2),
    ]
    messages = _rebuild(events)
    # Only the user message replays; progress is UI-only.
    assert all("thinking" not in m.get("content", "") for m in messages)
