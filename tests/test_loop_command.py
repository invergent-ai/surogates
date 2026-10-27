"""``/loop``: commands it answers itself, and what it hands to the model."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

import surogates.scheduled.store as store_module

from tests.test_wake_slash_command_gate import (
    _harness,
    _llm_responses,
    _permissive,
    _session,
    _stub_store,
)


class _Loops:
    def __init__(self, transitions: bool = True, status: str = "active") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.transitions = transitions
        self.status = status

    def __call__(self, _factory):
        return self

    def _row(self, **kw):
        now = datetime.now(timezone.utc)
        return SimpleNamespace(
            id=uuid4(), prompt=kw.get("prompt"), timezone=getattr(kw.get("schedule"), "timezone_name", "UTC"),
            schedule_display=getattr(kw.get("schedule"), "display", None), next_run_at=now,
            expires_at=now + timedelta(days=7), created_at=now, status=self.status, schedule={},
        )

    async def list_for_user(self, **kw):
        self.calls.append(("list_for_user", kw))
        return []

    async def create_loop(self, **kw):
        self.calls.append(("create_loop", kw))
        return self._row(**kw)

    async def create_dynamic_loop(self, **kw):
        self.calls.append(("create_dynamic_loop", kw))
        return self._row(**kw)

    async def _transition(self, verb, kw):
        self.calls.append((verb, kw))
        return self.transitions

    async def cancel(self, **kw):
        return await self._transition("cancel", kw)

    async def pause(self, **kw):
        return await self._transition("pause", kw)

    async def resume(self, **kw):
        return await self._transition("resume", kw)

    async def get_for(self, schedule_id, **kw):
        return self._row()


async def _loop(monkeypatch, text: str, loops: _Loops) -> str:
    monkeypatch.setattr(store_module, "ScheduledSessionStore", loops)
    session = _session()
    session.config = {"browser": {"profile": "p1"}, "ignored": 1}
    store = _stub_store(session, [])
    harness = _harness(store, _permissive())
    await harness._handle_loop_command(session, text, SimpleNamespace(lease_token="t"))
    return _llm_responses(store)[-1]


async def test_bare_loop_shows_how_to_use_it(monkeypatch):
    assert (await _loop(monkeypatch, "/loop", _Loops())).startswith("Usage: `/loop")


async def test_list_says_when_there_are_none(monkeypatch):
    loops = _Loops()
    assert await _loop(monkeypatch, "/loop list", loops) == "No active or paused loops."
    assert loops.calls[0][1]["status"] == "active,paused"


@pytest.mark.parametrize("verb, done", [("cancel", "cancelled"), ("stop", "cancelled"),
                                         ("pause", "paused"), ("resume", "resumed")])
async def test_verbs_act_on_the_named_loop(monkeypatch, verb, done):
    loops = _Loops()
    schedule_id = uuid4()
    reply = await _loop(monkeypatch, f"/loop {verb} {schedule_id}", loops)
    assert reply == f"Loop {schedule_id} {done}."
    name, kw = loops.calls[-1]
    assert kw["schedule_id"] == schedule_id
    assert ("ended_by" in kw) == (name == "cancel")


async def test_a_verb_without_an_id_asks_which(monkeypatch):
    reply = await _loop(monkeypatch, "/loop cancel", _Loops())
    assert reply.startswith("Which loop? Use `/loop cancel <id>`.")


async def test_a_verb_that_does_not_apply_says_why(monkeypatch):
    schedule_id = uuid4()
    reply = await _loop(monkeypatch, f"/loop resume {schedule_id}", _Loops(transitions=False))
    assert reply == f"Loop {schedule_id} is active; it cannot be resumed."


async def test_a_fixed_cadence_makes_a_loop_with_the_session_context(monkeypatch):
    loops = _Loops()
    reply = await _loop(monkeypatch, "/loop every day at 9 in Europe/Bucharest check the inbox", loops)
    name, kw = loops.calls[-1]
    assert name == "create_loop"
    assert kw["prompt"] == "check the inbox"
    assert kw["schedule"].timezone_name == "Europe/Bucharest"
    assert kw["context"] == {"browser": {"profile": "p1"}, "channel": "api"}
    assert "Timezone: Europe/Bucharest" in reply
    assert "/loop pause" in reply


async def test_no_cadence_makes_a_dynamic_loop(monkeypatch):
    loops = _Loops()
    reply = await _loop(monkeypatch, "/loop watch the build until it is green", loops)
    assert loops.calls[-1][0] == "create_dynamic_loop"
    assert "dynamic" in reply


def test_a_cadence_it_cannot_read_goes_to_the_model():
    harness = _harness(_stub_store(_session(), []), _permissive())
    session = _session()
    assert not harness._answers_itself("/loop every second tuesday post the newsletter", session)
    assert harness._answers_itself("/loop list", session)
    assert harness._answers_itself("/loop 5m check", session)
