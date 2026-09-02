"""A closed browser must reach the model, not just the UI.

``browser.destroyed`` was a UI-only event: the pane updated and the card
disappeared, while the model's context kept the screenshots and page text
from before. Its next browser call silently provisions a fresh blank
browser, so nothing in the transcript marks the discontinuity — which is
how an agent ends up describing a page it no longer has.
"""

from __future__ import annotations

from types import SimpleNamespace

from surogates.harness.loop_context_replay import ContextReplayMixin
from surogates.session.events import EventType


class _Replayer(ContextReplayMixin):
    """The mixin under test; replay reads nothing else off self."""


def _event(etype: str, data: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(type=etype, data=data or {})


def _user(text: str) -> SimpleNamespace:
    return _event(EventType.USER_MESSAGE.value, {"content": text})


def _replay(events: list[SimpleNamespace]) -> list[dict]:
    return _Replayer()._rebuild_messages(events)


def _texts(messages: list[dict]) -> str:
    return "\n".join(
        m["content"] for m in messages if isinstance(m.get("content"), str)
    )


def test_a_destroyed_browser_is_reported_to_the_model() -> None:
    messages = _replay([
        _user("open a browser and read the news"),
        _event(EventType.BROWSER_DESTROYED.value, {"browser_id": "b-1"}),
    ])
    combined = _texts(messages)
    assert "browser" in combined.lower()
    assert "closed" in combined.lower()


def test_the_note_lands_after_the_work_it_invalidates() -> None:
    # Ordering is the whole point: a note before the page content would
    # read as stale history rather than as "what you saw is gone".
    messages = _replay([
        _user("read mediafax.ro"),
        _event(EventType.BROWSER_DESTROYED.value, {"browser_id": "b-1"}),
        _user("what is on the page?"),
    ])
    contents = [m["content"] for m in messages if isinstance(m.get("content"), str)]
    closed = next(i for i, c in enumerate(contents) if "closed" in c.lower())
    asked = next(i for i, c in enumerate(contents) if "what is on the page" in c)
    assert closed < asked


def test_a_close_mid_tool_call_does_not_split_the_pair() -> None:
    # A human clicks close whenever they like, including while the agent is
    # mid-turn. A user-role message inserted between an assistant's
    # tool_calls and its tool results is rejected outright by the provider,
    # which is why user messages are deferred to the iteration boundary —
    # this note has to observe the same rule.
    messages = _replay([
        _user("go"),
        _event(EventType.LLM_REQUEST.value),
        _event(EventType.LLM_RESPONSE.value, {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "tc1", "function": {"name": "browser_open"}}],
            },
        }),
        _event(EventType.BROWSER_DESTROYED.value, {"browser_id": "b-1"}),
        _event(EventType.TOOL_RESULT.value, {"tool_call_id": "tc1", "content": "ok"}),
    ])
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "tool", "user"]
    assert "closed" in messages[-1]["content"].lower()


def test_replay_stays_stable_across_runs() -> None:
    events = [
        _user("hi"),
        _event(EventType.BROWSER_DESTROYED.value, {"browser_id": "b-1"}),
    ]
    assert _replay(events) == _replay(events)


def test_a_session_that_never_had_a_browser_is_untouched() -> None:
    messages = _replay([_user("hello")])
    assert "closed" not in _texts(messages).lower()
