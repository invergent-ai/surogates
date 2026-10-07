"""A turn's tool saga: it ends with its turn, and a stop compensates only that turn."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import surogates.harness.loop as loop_module
from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.runtime import SlashCommandConfig
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime
from tests.test_steer_loop import _final_response, _make_loop_harness

from .test_devices import api  # noqa: F401  (api is a fixture)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def calling(*calls: tuple[str, dict]) -> tuple[dict, dict]:
    """A model reply that calls each of *calls*, as ``(name, arguments)``."""
    return (
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"call_{n}_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for n, (name, args) in enumerate(calls)
        ]},
        {"model": "test-model", "finish_reason": "tool_calls", "input_tokens": 1, "output_tokens": 1},
    )


async def a_turn(
    api, monkeypatch, session, replies, *, saga: bool = True, pool: Any = None, during=None,
    saga_settings: Any = None,
) -> AgentHarness:
    """One real turn of *session*, against a model that gives *replies* in
    order, its turn ending as a worker ends it.  The ``memory`` tool runs
    *during* (with the harness) instead of writing memory.  Returns the harness."""
    store = api.app.state.session_store
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    harness = _make_loop_harness(session_store=store, budget=IterationBudget(max_total=6))
    dispatch = registry.dispatch

    async def tool(name, arguments, **kwargs):
        if name != "memory":
            return await dispatch(name, arguments, **kwargs)
        if during is not None:
            await during(harness)
        return '{"ok": true}'

    monkeypatch.setattr(registry, "dispatch", tool)
    harness._tools = registry
    harness._tenant = SimpleNamespace(org_id=session.org_id, user_id=session.user_id, asset_root="/tmp/test")
    harness._session_factory = api.app.state.session_factory
    harness._redis = api.app.state.redis
    harness._slash_commands = SlashCommandConfig()
    harness._saga_enabled = saga
    harness._saga_settings = saga_settings
    harness._sandbox_pool = pool
    del harness._complete_session  # the real turn end
    replies = iter(replies)

    async def model(**kwargs):
        message, usage = next(replies)
        if kwargs.get("on_tool_call_complete") is not None:
            for call in message.get("tool_calls") or []:
                kwargs["on_tool_call_complete"](call)
        return message, usage

    monkeypatch.setattr(loop_module, "call_llm_with_retry", model)
    lease = await store.try_acquire_lease(session.id, "worker-sagas", ttl_seconds=60)
    events = await store.get_events(session.id)
    session = await store.get_session(session.id)
    await harness._run_loop(session, harness._rebuild_messages(events), "system", lease, all_events=events)
    await store.release_lease(session.id, lease.lease_token)
    return harness


async def stop(harness) -> None:
    harness.interrupt("stopped by the user")


async def saga_events(api, session_id) -> list[tuple[str, dict]]:
    events = await api.app.state.session_store.get_events(session_id)
    return [(e.type, e.data) for e in events if e.type.startswith("saga.")]


async def a_chat(api):
    created = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    return await api.app.state.session_store.get_session(UUID(created.json()["id"]))


async def test_a_turn_end_completes_its_saga_so_a_stop_in_the_next_turn_undoes_only_that_turn(api, monkeypatch):
    chat = await a_chat(api)
    store = api.app.state.session_store
    await store.emit_event(chat.id, EventType.USER_MESSAGE, {"content": "Remember my name."})
    remember = calling(("memory", {"action": "add", "content": "Name: Ana"}))
    await a_turn(api, monkeypatch, chat, [remember, _final_response("Noted.")])
    [(_, first)], [(_, done)] = [
        [(t, d) for t, d in await saga_events(api, chat.id) if t == kind]
        for kind in (EventType.SAGA_START.value, EventType.SAGA_COMPLETE.value)
    ]
    assert (done["saga_id"], done["status"], done["steps_executed"]) == (first["saga_id"], "completed", 1)

    await store.emit_event(chat.id, EventType.USER_MESSAGE, {"content": "Remember my city too."})
    pool = SimpleNamespace(ensure=AsyncMock(side_effect=RuntimeError("no pod")), destroy_for_session=AsyncMock())
    await a_turn(api, monkeypatch, chat, [remember, _final_response("Noted.")], pool=pool, during=stop)
    pool.ensure.assert_not_awaited()  # nothing the turn did can be undone, so no pod is set up
    sagas = await saga_events(api, chat.id)
    second = [d for t, d in sagas if t == EventType.SAGA_START.value][-1]
    assert second["saga_id"] != first["saga_id"]
    [compensated] = [d for t, d in sagas if t == EventType.SAGA_COMPENSATE.value]
    # Only this turn's step is compensated; the first turn's stays done.
    [step] = [d for t, d in sagas if t == EventType.SAGA_STEP_BEGIN.value and d["saga_id"] == second["saga_id"]]
    assert (compensated["saga_id"], compensated.get("failed_steps")) == (second["saga_id"], [step["step_id"]])
    # A compensation ends its saga, so no later step joins it.
    last_type, last = sagas[-1]
    assert (last_type, last["saga_id"], last["status"]) == (EventType.SAGA_COMPLETE.value, second["saga_id"], "escalated")

    await store.emit_event(chat.id, EventType.USER_MESSAGE, {"content": "Thanks."})
    await a_turn(api, monkeypatch, chat, [_final_response("You're welcome.")])
    third = [d for t, d in await saga_events(api, chat.id) if t == EventType.SAGA_START.value][-1]
    assert third["saga_id"] not in (first["saga_id"], second["saga_id"])


async def test_a_stop_whose_sandbox_cannot_be_set_up_still_ends_its_saga(api, monkeypatch):
    chat = await a_chat(api)
    store = api.app.state.session_store
    await store.emit_event(chat.id, EventType.USER_MESSAGE, {"content": "Remember my name."})
    remember = calling(("memory", {"action": "add", "content": "Name: Ana"}))
    remember[0]["tool_calls"][0]["_checkpoint_hash"] = "0" * 40  # a step a restore could undo
    pool = SimpleNamespace(
        ensure=AsyncMock(side_effect=RuntimeError("the pod is gone")),
        destroy_for_session=AsyncMock(),
    )
    await a_turn(api, monkeypatch, chat, [remember, _final_response("Noted.")], pool=pool, during=stop)
    pool.ensure.assert_awaited_once()
    sagas = await saga_events(api, chat.id)
    [(_, first)] = [(t, d) for t, d in sagas if t == EventType.SAGA_START.value]
    [step] = [d for t, d in sagas if t == EventType.SAGA_STEP_BEGIN.value]
    [compensated] = [d for t, d in sagas if t == EventType.SAGA_COMPENSATE.value]
    assert (compensated["saga_id"], compensated["steps_rolled_back"], compensated["failed_steps"]) == (
        first["saga_id"], 0, [step["step_id"]],
    )
    last_type, last = sagas[-1]
    assert (last_type, last["saga_id"], last["status"]) == (EventType.SAGA_COMPLETE.value, first["saga_id"], "escalated")

    await store.emit_event(chat.id, EventType.USER_MESSAGE, {"content": "Thanks."})
    await a_turn(api, monkeypatch, chat, [_final_response("You're welcome.")])
    second = [d for t, d in await saga_events(api, chat.id) if t == EventType.SAGA_START.value][-1]
    assert second["saga_id"] != first["saga_id"]


async def test_a_failed_turn_completes_its_saga(api, monkeypatch):
    chat = await a_chat(api)
    await api.app.state.session_store.emit_event(chat.id, EventType.USER_MESSAGE, {"content": "Remember my name."})
    provider_error = (
        {"role": "assistant", "content": "", "tool_calls": None},
        {"model": "test-model", "finish_reason": "error", "input_tokens": 1, "output_tokens": 1},
    )
    await a_turn(api, monkeypatch, chat, [calling(("memory", {"action": "add", "content": "Name: Ana"}))] + [provider_error] * 3)
    assert (await api.app.state.session_store.get_session(chat.id)).status == "failed"
    [(_, done)] = [(t, d) for t, d in await saga_events(api, chat.id) if t == EventType.SAGA_COMPLETE.value]
    assert done["status"] == "completed"
