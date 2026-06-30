"""Mid-turn steering: queued user messages fold into the running wake."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.session.events import EventType
from surogates.session.models import Session


def _make_loop_harness(*, session_store: Any, budget: IterationBudget | None = None) -> AgentHarness:
    harness = AgentHarness.__new__(AgentHarness)
    harness._store = session_store
    harness._llm = AsyncMock()
    harness._tools = MagicMock()
    harness._tenant = SimpleNamespace(org_id=uuid4(), user_id=uuid4())
    harness._worker_id = "test-worker"
    harness._budget = budget or IterationBudget(max_total=6)
    harness._compressor = SimpleNamespace(
        context_length=1000,
        _context_window=200_000,
        should_compress=lambda *a, **k: False,
    )
    harness._prompt = SimpleNamespace(has_agents=False)
    harness._redis = None
    harness._sandbox_pool = None
    harness._browser_pool = None
    harness._browser_control = None
    harness._storage = None
    harness._api_client = None
    harness._session_factory = None
    harness._vision_client = None
    harness._vision_model = ""
    harness._advisor_client = None
    harness._advisor_model = ""
    harness._advisor_max_calls_per_turn = 0
    harness._advisor_max_tokens = 0
    harness._checkpoints_enabled = False
    harness._saga_enabled = False
    harness._saga_settings = None
    harness._log_policy_allowed = False
    harness._memory_manager = None
    harness._memory_nudge_interval = 0
    harness._turns_since_memory = 0
    harness._skill_nudge_interval = 0
    harness._iters_since_skill = 0
    harness._user_turn_count = 0
    harness._streaming_enabled = False
    harness._default_model = "test-model"
    harness._current_model = "test-model"
    harness._background_tasks = set()
    harness._turn_summarizer = None
    harness._pending_iteration_summary_tasks = {}
    harness._completed_iteration_summaries = {}
    harness._turn_started_at = None
    harness._interrupt_requested = False
    harness._interrupt_message = None
    harness._system_prompt_cache = MagicMock()
    harness._system_prompt_cache.is_valid = MagicMock(return_value=False)
    harness._system_prompt_cache.invalidate = MagicMock(return_value=None)
    harness._cost_tracker = None
    harness._prefetch_memory = AsyncMock(return_value="")
    harness._maybe_consult_required_expert = AsyncMock(return_value=None)
    harness._maybe_consult_required_advisor = AsyncMock(return_value=None)
    harness._maybe_route_final_response_to_inbox = AsyncMock(return_value=None)
    harness._maybe_generate_title = MagicMock(return_value=None)
    harness._promote_fenced_artifacts = AsyncMock(return_value=None)
    harness._maybe_continue_outcome = AsyncMock(return_value=False)
    harness._maybe_run_mission_evaluator_for_session = AsyncMock(return_value=None)
    harness._mission_has_pending_work = AsyncMock(return_value=False)
    harness._maybe_summarize_iteration = AsyncMock(return_value=None)
    harness._complete_session = AsyncMock(return_value=None)
    harness._end_turn = AsyncMock(return_value=None)
    harness._provider_rate_limit_guard = MagicMock(return_value=None)
    harness._compress_context_callback = MagicMock(return_value=lambda *a, **k: None)
    # Collaborators on the tool-execution path (used only by the mid-tool
    # steer test). Mocked so a tool-calling iteration can run end-to-end.
    harness._inject_checkpoint_hashes = AsyncMock(return_value=None)
    harness._dynamic_loop_wait_succeeded = MagicMock(return_value=False)
    harness._active_executor = None
    harness._credential_vault = None
    harness._summary_client = None
    harness._summary_model = ""
    harness._media_gen = None
    harness._turn_gate = None
    harness._bundle = None
    return harness


def _make_session() -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1",
        channel="web", status="active", config={},
        created_at=now, updated_at=now,
    )


def _steer_event(eid: int, text: str):
    return SimpleNamespace(id=eid, data={"content": text})


async def _drive(harness, responses, monkeypatch, *, all_events=None):
    call_log = iter(responses)

    async def fake_call_llm_with_retry(**_kwargs):
        try:
            return next(call_log)
        except StopIteration as exc:
            raise AssertionError("loop drove more iterations than scripted") from exc

    monkeypatch.setattr(
        "surogates.harness.loop.call_llm_with_retry", fake_call_llm_with_retry,
    )
    session = _make_session()
    lease = SimpleNamespace(lease_token=uuid4())
    await harness._run_loop(
        session, [{"role": "user", "content": "do the task"}],
        "system", lease, all_events=all_events or [],
    )
    return [
        (c.args[1], c.args[2]) for c in harness._store.emit_event.await_args_list
    ]


def _tool_call_response(tc_id: str):
    return (
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": tc_id, "type": "function",
                         "function": {"name": "noop", "arguments": "{}"}}]},
        {"model": "test-model", "finish_reason": "tool_calls",
         "input_tokens": 1, "output_tokens": 1},
    )


def _final_response(text: str):
    return (
        {"role": "assistant", "content": text, "tool_calls": None},
        {"model": "test-model", "finish_reason": "stop",
         "input_tokens": 1, "output_tokens": 1},
    )


def _patch_tool_exec(monkeypatch, tool_results):
    """Patch the module-level execute_tool_calls the non-streaming path uses."""
    async def fake_execute_tool_calls(tool_calls_raw, **_kwargs):
        return list(tool_results)
    monkeypatch.setattr(
        "surogates.harness.loop.execute_tool_calls", fake_execute_tool_calls,
    )


@pytest.mark.asyncio
async def test_steer_message_starts_new_turn_id_mid_wake(monkeypatch):
    # get_events sequence: iter1 loop-top (none) -> iter2 loop-top (a steer
    # message arrived during iter1's tool call) -> iter2 completion drain (none).
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    store.get_events = AsyncMock(
        side_effect=[[], [_steer_event(50, "also do Z")], []],
    )
    store.execute = AsyncMock(return_value=None)
    harness = _make_loop_harness(session_store=store)

    # iter1 emits a tool call; the patched executor returns its result; the
    # iter2 loop-top boundary injects the steer message; iter2 is the final.
    _patch_tool_exec(monkeypatch, [{"role": "tool", "tool_call_id": "X", "content": "ok"}])

    emits = await _drive(
        harness,
        responses=[_tool_call_response("X"), _final_response("done")],
        monkeypatch=monkeypatch,
    )
    req_payloads = [p for t, p in emits if t == EventType.LLM_REQUEST]
    assert len(req_payloads) == 2
    # second iteration belongs to a NEW turn with iteration_index reset to 0
    assert req_payloads[0]["turn_id"] != req_payloads[1]["turn_id"]
    assert req_payloads[1]["iteration_index"] == 0


@pytest.mark.asyncio
async def test_no_steer_message_keeps_single_turn(monkeypatch):
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    store.get_events = AsyncMock(return_value=[])
    store.execute = AsyncMock(return_value=None)
    harness = _make_loop_harness(session_store=store)
    _patch_tool_exec(monkeypatch, [{"role": "tool", "tool_call_id": "X", "content": "ok"}])

    emits = await _drive(
        harness,
        responses=[_tool_call_response("X"), _final_response("done")],
        monkeypatch=monkeypatch,
    )
    req_payloads = [p for t, p in emits if t == EventType.LLM_REQUEST]
    assert len(req_payloads) == 2
    # same turn across both iterations; index increments
    assert req_payloads[0]["turn_id"] == req_payloads[1]["turn_id"]
    assert req_payloads[1]["iteration_index"] == 1


@pytest.mark.asyncio
async def test_initial_user_message_not_re_incorporated(monkeypatch):
    # all_events already contains the initial user.message at id 50; the
    # boundary query must not re-inject it.
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    store.get_events = AsyncMock(return_value=[])  # nothing past the cursor
    store.execute = AsyncMock(return_value=None)
    harness = _make_loop_harness(session_store=store)

    emits = await _drive(
        harness,
        responses=[_final_response("done")],
        monkeypatch=monkeypatch,
        all_events=[SimpleNamespace(id=50, type=EventType.USER_MESSAGE.value, data={"content": "do the task"})],
    )
    req_payloads = [p for t, p in emits if t == EventType.LLM_REQUEST]
    assert len(req_payloads) == 1
    # get_events was queried with after >= 50 (cursor seeded from all_events)
    first_after = store.get_events.await_args_list[0].kwargs["after"]
    assert first_after >= 50


@pytest.mark.asyncio
async def test_followup_at_completion_continues_instead_of_completing(monkeypatch):
    # iter1 returns a final response (no tool calls); a follow-up is waiting,
    # so the wake must NOT complete — it continues as a new turn and iter2
    # produces the real final response.
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    # boundary query at iter1 top: nothing; completion drain at iter1: a
    # follow-up; boundary query at iter2 top: nothing; drain at iter2: none.
    store.get_events = AsyncMock(side_effect=[
        [],                              # iter1 loop-top steer check
        [_steer_event(60, "one more thing")],  # iter1 completion drain
        [],                              # iter2 loop-top steer check
        [],                              # iter2 completion drain
    ])
    store.execute = AsyncMock(return_value=None)
    harness = _make_loop_harness(session_store=store)

    emits = await _drive(
        harness,
        responses=[_final_response("first answer"), _final_response("second answer")],
        monkeypatch=monkeypatch,
    )
    # completion happened exactly once, after the second response
    assert harness._complete_session.await_count == 1
    req_payloads = [p for t, p in emits if t == EventType.LLM_REQUEST]
    assert len(req_payloads) == 2
    assert req_payloads[0]["turn_id"] != req_payloads[1]["turn_id"]
    assert req_payloads[1]["iteration_index"] == 0


@pytest.mark.asyncio
async def test_no_followup_completes_normally(monkeypatch):
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    store.get_events = AsyncMock(return_value=[])  # never any steer messages
    store.execute = AsyncMock(return_value=None)
    harness = _make_loop_harness(session_store=store)

    await _drive(harness, responses=[_final_response("done")], monkeypatch=monkeypatch)
    assert harness._complete_session.await_count == 1
