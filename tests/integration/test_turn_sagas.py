"""A turn's tool saga: it ends with its turn, and a stop compensates only that turn."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

import surogates.harness.loop as loop_module
from surogates.harness import tool_exec
from surogates.harness.budget import IterationBudget
from surogates.governance.events import saga_compensate_event, saga_start_event, saga_step_event
from surogates.harness.loop import AgentHarness
from surogates.runtime import SlashCommandConfig
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime
from tests.test_steer_loop import _final_response, _make_loop_harness
from tests.test_wake_stranded_user_message import _harness

from .test_devices import api  # noqa: F401  (api is a fixture)
from .test_workstream_threads import start
from .test_workstreams import create, master_of

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
    # A thread's step starts from a snapshot of its copy, so a restore could undo it.
    chat = await start(api, await master_of(api, await create(api)), goal="Work on the report.")
    monkeypatch.setattr(tool_exec, "_snapshot_copy", AsyncMock(return_value="0" * 40))
    store = api.app.state.session_store
    await store.emit_event(chat.id, EventType.USER_MESSAGE, {"content": "Remember my name."})
    remember = calling(("memory", {"action": "add", "content": "Name: Ana"}))
    pool = SimpleNamespace(
        ensure=AsyncMock(side_effect=RuntimeError("the pod is gone")),
        destroy_for_session=AsyncMock(),
        copy_fresh=lambda key: False,
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


async def a_saga_left_open(api, chat, *, compensating: bool) -> str:
    """A turn's saga with one step done, that nothing closed; its id."""
    store, saga_id = api.app.state.session_store, f"saga:{uuid4()}"
    await store.emit_event(chat.id, EventType.SAGA_START, saga_start_event(saga_id, str(chat.id)))
    for kind, state in ((EventType.SAGA_STEP_BEGIN, "executing"), (EventType.SAGA_STEP_COMMITTED, "committed")):
        await store.emit_event(chat.id, kind, saga_step_event(saga_id, "step-1", "memory", state))
    if compensating:
        await store.emit_event(chat.id, EventType.SAGA_COMPENSATE, saga_compensate_event(saga_id, 0, "interrupt"))
    return saga_id


@pytest.mark.parametrize(("compensating", "end", "status"), [
    (False, EventType.SESSION_FAIL, "completed"),  # the dispatcher gave up on the turn itself
    (False, EventType.SESSION_COMPLETE, "completed"),  # the turn's saga.complete was lost
    (True, EventType.SESSION_FAIL, "escalated"),  # lost while it was being put back
    (True, EventType.SESSION_PAUSE, "escalated"),  # a stop's, lost once it was put back
    (True, EventType.SESSION_STOPPED, "escalated"),  # a channel stop's
    (False, EventType.SESSION_PAUSE, "escalated"),  # a stop whose worker was lost before it put anything back
    (False, EventType.SESSION_STOPPED, "escalated"),
])
async def test_a_saga_a_turn_left_open_is_closed_before_the_next_turn_starts_its_own(
    api, monkeypatch, compensating, end, status,
):
    chat = await a_chat(api)
    store = api.app.state.session_store
    left_open = await a_saga_left_open(api, chat, compensating=compensating)
    await store.emit_event(chat.id, end, {"error": "gave up"} if end is EventType.SESSION_FAIL else {})
    await store.emit_event(chat.id, EventType.USER_MESSAGE, {"content": "Thanks."})
    await a_turn(api, monkeypatch, chat, [_final_response("You're welcome.")])
    sagas = [(t, d) for t, d in await saga_events(api, chat.id) if t in (EventType.SAGA_START.value, EventType.SAGA_COMPLETE.value)]
    [closed, (_, started), (_, done)] = sagas[-3:]
    assert closed == (EventType.SAGA_COMPLETE.value, {**closed[1], "saga_id": left_open, "status": status, "steps_executed": 1})
    assert started["saga_id"] != left_open and done["saga_id"] == started["saga_id"]


async def test_a_stopped_turn_still_putting_back_keeps_its_saga_from_the_next_turn(api, monkeypatch):
    chat = await a_chat(api)
    store = api.app.state.session_store
    await a_saga_left_open(api, chat, compensating=False)
    await store.emit_event(chat.id, EventType.SESSION_PAUSE, {})  # the pause route's, before its turn hears it
    unwinding = await store.try_acquire_lease(chat.id, "worker-stopped", ttl_seconds=60)
    await store.emit_event(chat.id, EventType.USER_MESSAGE, {"content": "Go on."})
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    # The next turn waits for the stopped one's lease, so it never closes a saga still being put back.
    assert await _harness(store).wake(chat.id) == "lease_held"
    assert [t for t, _ in await saga_events(api, chat.id) if t == EventType.SAGA_COMPLETE.value] == []
    await store.release_lease(chat.id, unwinding.lease_token)


async def test_a_turn_retried_after_a_crash_keeps_its_saga(api, monkeypatch):
    chat = await a_chat(api)
    store = api.app.state.session_store
    await store.emit_event(chat.id, EventType.SESSION_COMPLETE, {})  # an earlier turn
    await store.emit_event(chat.id, EventType.USER_MESSAGE, {"content": "Remember my name."})
    running = await a_saga_left_open(api, chat, compensating=False)  # this turn's, before its worker crashed
    await a_turn(api, monkeypatch, chat, [_final_response("Noted.")])
    starts = [d for t, d in await saga_events(api, chat.id) if t == EventType.SAGA_START.value]
    [(_, done)] = [(t, d) for t, d in await saga_events(api, chat.id) if t == EventType.SAGA_COMPLETE.value]
    assert ([d["saga_id"] for d in starts], done["saga_id"]) == ([running], running)
