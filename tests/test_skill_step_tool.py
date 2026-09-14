"""The skill_step tool records one procedure transition per call as a
``skill.step`` event and rejects malformed markers without emitting."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from surogates.session.events import EventType
from surogates.tools.builtin.skill_step import _skill_step_handler
from surogates.tools.registry import ToolRegistry
from surogates.tools.router import ToolLocation, ToolRouter
from surogates.tools.runtime import ToolRuntime


class _Store:
    def __init__(self) -> None:
        self.events: list[tuple[Any, str, dict]] = []

    async def emit_event(self, session_id, type_, data) -> int:
        self.events.append((session_id, getattr(type_, "value", type_), data))
        return len(self.events)


async def _call(store, sid, **args) -> dict:
    return json.loads(await _skill_step_handler(
        args, session_id=str(sid), session_store=store, tool_call_id="call-7",
    ))


@pytest.mark.asyncio
async def test_a_marker_emits_one_event_and_confirms():
    store, sid = _Store(), uuid4()
    out = await _call(store, sid, skill="kubectl-diagnose", step="s3", status="started")
    assert out == {"ok": True, "skill": "kubectl-diagnose", "step": "s3", "status": "started"}
    assert store.events == [(str(sid), EventType.SKILL_STEP.value, {
        "skill": "kubectl-diagnose", "step": "s3", "status": "started", "tool_call_id": "call-7",
    })]


@pytest.mark.asyncio
@pytest.mark.parametrize("args, field", [
    ({"step": "s3", "status": "started"}, "skill"),
    ({"skill": "x" * 201, "step": "s3", "status": "started"}, "skill"),
    ({"skill": "x", "step": "step 3", "status": "started"}, "step"),
    ({"skill": "x", "step": "s12345678", "status": "started"}, "step"),
    ({"skill": "x", "step": "s3", "status": "in_progress"}, "status"),
])
async def test_malformed_markers_are_refused_without_emitting(args, field):
    store = _Store()
    out = await _call(store, uuid4(), **args)
    assert field in out["error"]
    assert store.events == []


@pytest.mark.asyncio
async def test_status_is_case_insensitive_and_trimmed():
    store, sid = _Store(), uuid4()
    out = await _call(store, sid, skill=" proc ", step=" s2 ", status=" Completed ")
    assert out["ok"] and out["skill"] == "proc" and out["step"] == "s2" and out["status"] == "completed"


@pytest.mark.asyncio
async def test_without_a_session_nothing_is_recorded():
    out = json.loads(await _skill_step_handler({"skill": "x", "step": "s1", "status": "started"}))
    assert "session" in out["error"]


def test_registered_and_routed_in_process():
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    names = {s["function"]["name"] for s in registry.get_schemas()}
    assert "skill_step" in names
    router = ToolRouter(registry=registry, sandbox_pool=None, governance=None)
    assert router.resolve_location("skill_step") is ToolLocation.HARNESS
