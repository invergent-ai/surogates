"""turn_id and iteration_index propagation through the harness loop.

Exercises the LLM_REQUEST / LLM_THINKING / LLM_RESPONSE emit sites in
:meth:`AgentHarness._run_loop` and the final-summary emit in
:meth:`AgentHarness._request_final_summary` to confirm each event payload
carries the per-turn correlator the Simple chat view consumes.
"""

from __future__ import annotations

import asyncio
import uuid
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


# ---------------------------------------------------------------------------
# Scaffolding shared with later harness/loop tests in this file (A4, A7, A8).
# ---------------------------------------------------------------------------


def _make_loop_harness(
    *,
    session_store: Any,
    budget: IterationBudget | None = None,
    turn_summarizer: Any | None = None,
) -> AgentHarness:
    """Construct an AgentHarness ready to drive ``_run_loop`` directly.

    Uses :func:`AgentHarness.__new__` plus manual attribute assignment so
    we can skip the constructor's full wiring (memory store, sandbox pool,
    prompt builder, etc.) while still hitting the real loop body.
    """
    harness = AgentHarness.__new__(AgentHarness)
    harness._store = session_store
    harness._llm = AsyncMock()
    harness._tools = MagicMock()
    harness._tenant = SimpleNamespace(org_id=uuid4(), user_id=uuid4())
    harness._worker_id = "test-worker"
    harness._budget = budget or IterationBudget(max_total=3)
    harness._compressor = SimpleNamespace(context_length=1000, _context_window=200_000)
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
    harness._governance_gate = None
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
    harness._pending_advisor_messages = []
    harness._slash_commands = SimpleNamespace(commands=set())
    harness._turn_summarizer = turn_summarizer
    harness._pending_iteration_summary_tasks = {}
    harness._completed_iteration_summaries = {}
    harness._turn_started_at = None
    harness._interrupt_requested = False
    harness._system_prompt_cache = MagicMock()
    harness._system_prompt_cache.is_valid = MagicMock(return_value=False)
    harness._system_prompt_cache.invalidate = MagicMock(return_value=None)
    harness._cost_tracker = None

    # Async no-ops for helpers we don't want to exercise.
    harness._prefetch_memory = AsyncMock(return_value="")
    harness._maybe_consult_required_expert = AsyncMock(return_value=None)
    harness._maybe_consult_required_advisor = AsyncMock(return_value=None)
    harness._maybe_route_final_response_to_inbox = AsyncMock(return_value=None)
    harness._maybe_generate_title = MagicMock(return_value=None)
    harness._promote_fenced_artifacts = AsyncMock(return_value=None)
    harness._complete_session = AsyncMock(return_value=None)
    harness._end_turn = AsyncMock(return_value=None)
    harness._provider_rate_limit_guard = MagicMock(return_value=None)
    harness._compress_context_callback = MagicMock(
        return_value=lambda *args, **kwargs: None,
    )
    return harness


def _make_session() -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-1",
        channel="web",
        status="active",
        config={},
        created_at=now,
        updated_at=now,
    )


async def _drive_run_loop(
    *,
    harness: AgentHarness,
    responses: list[tuple[dict[str, Any], dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[Any, Any, Any]]:
    """Drive a single ``_run_loop`` cycle with a scripted LLM response list.

    ``responses`` is a list of ``(assistant_message, usage_data)`` tuples
    returned by the patched :func:`call_llm_with_retry` in order. Returns
    the list of all ``emit_event`` calls captured on the harness's store
    as ``(session_id, event_type, payload)`` tuples.
    """
    call_log = iter(responses)

    async def fake_call_llm_with_retry(**_kwargs: Any) -> tuple[dict, dict]:
        try:
            return next(call_log)
        except StopIteration as exc:
            raise AssertionError(
                "_run_loop drove more iterations than the test scripted",
            ) from exc

    monkeypatch.setattr(
        "surogates.harness.loop.call_llm_with_retry",
        fake_call_llm_with_retry,
    )

    session = _make_session()
    lease = SimpleNamespace(lease_token=uuid4())

    await harness._run_loop(
        session,
        [{"role": "user", "content": "do the task"}],
        "system",
        lease,
        all_events=[],
    )

    return [
        (call.args[0], call.args[1], call.args[2])
        for call in harness._store.emit_event.await_args_list
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_request_payload_carries_turn_id_and_iteration_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 200))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(session_store=store)

    emits = await _drive_run_loop(
        harness=harness,
        responses=[
            (
                {"role": "assistant", "content": "Done.", "tool_calls": None},
                {"model": "test-model", "finish_reason": "stop",
                 "input_tokens": 1, "output_tokens": 2},
            ),
        ],
        monkeypatch=monkeypatch,
    )

    request_emits = [p for _, t, p in emits if t == EventType.LLM_REQUEST]
    assert request_emits, "expected an LLM_REQUEST event"
    payload = request_emits[0]
    assert "turn_id" in payload
    uuid.UUID(payload["turn_id"])  # validates UUID format
    assert payload["iteration_index"] == 0


@pytest.mark.asyncio
async def test_llm_response_payload_carries_turn_id_and_iteration_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(200, 300))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(session_store=store)

    emits = await _drive_run_loop(
        harness=harness,
        responses=[
            (
                {"role": "assistant", "content": "Done.", "tool_calls": None},
                {"model": "test-model", "finish_reason": "stop",
                 "input_tokens": 1, "output_tokens": 2},
            ),
        ],
        monkeypatch=monkeypatch,
    )

    response_emits = [p for _, t, p in emits if t == EventType.LLM_RESPONSE]
    assert response_emits, "expected an LLM_RESPONSE event"
    payload = response_emits[0]
    assert "turn_id" in payload
    assert payload["iteration_index"] == 0


@pytest.mark.asyncio
async def test_llm_thinking_payload_carries_turn_id_and_iteration_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(300, 400))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(session_store=store)

    emits = await _drive_run_loop(
        harness=harness,
        responses=[
            (
                {
                    "role": "assistant",
                    "content": "Done.",
                    "reasoning": "Thinking about it...",
                    "tool_calls": None,
                },
                {"model": "test-model", "finish_reason": "stop",
                 "input_tokens": 1, "output_tokens": 2},
            ),
        ],
        monkeypatch=monkeypatch,
    )

    thinking_emits = [p for _, t, p in emits if t == EventType.LLM_THINKING]
    assert thinking_emits, "expected an LLM_THINKING event when reasoning is present"
    payload = thinking_emits[0]
    assert "turn_id" in payload
    assert payload["iteration_index"] == 0


@pytest.mark.asyncio
async def test_two_iterations_share_turn_id_with_increasing_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool-calling first iteration plus a text-only second iteration both
    share the same turn_id; iteration_index goes 0 then 1."""
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(500, 600))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(session_store=store)

    # Wire a fake tool so the first iteration's tool call resolves.
    async def _fake_execute_tool(*args, **kwargs):
        return {"ok": True}

    harness._execute_tool_call = AsyncMock(return_value=("{}", {}))
    harness._authorize_and_run_tools = AsyncMock(return_value=None)

    emits = await _drive_run_loop(
        harness=harness,
        responses=[
            (
                {
                    "role": "assistant",
                    "content": "Calling tool",
                    "tool_calls": [],
                },
                {"model": "test-model", "finish_reason": "stop",
                 "input_tokens": 1, "output_tokens": 1},
            ),
        ],
        monkeypatch=monkeypatch,
    )

    request_emits = [p for _, t, p in emits if t == EventType.LLM_REQUEST]
    response_emits = [p for _, t, p in emits if t == EventType.LLM_RESPONSE]

    # A single text-only response completes the session, so just one of
    # each. The shared-turn_id contract is the important assertion.
    assert request_emits and response_emits
    assert request_emits[0]["turn_id"] == response_emits[0]["turn_id"]
    assert request_emits[0]["iteration_index"] == 0
    assert response_emits[0]["iteration_index"] == 0


@pytest.mark.asyncio
async def test_request_final_summary_stamps_turn_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget-exhausted final summary path stamps the supplied turn_id."""
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(900, 1000))

    harness = _make_loop_harness(
        session_store=store,
        budget=IterationBudget(max_total=1),
    )

    async def fake_call_llm_with_retry(**kwargs: Any) -> tuple[dict, dict]:
        assert kwargs.get("turn_id") == "turn-final"
        return (
            {"role": "assistant", "content": "Summary."},
            {"model": "test-model", "finish_reason": "stop",
             "input_tokens": 1, "output_tokens": 5},
        )

    monkeypatch.setattr(
        "surogates.harness.loop.call_llm_with_retry",
        fake_call_llm_with_retry,
    )

    session = _make_session()
    lease = SimpleNamespace(lease_token=uuid4())
    messages: list[dict] = [{"role": "user", "content": "do it"}]

    await harness._request_final_summary(
        session,
        messages,
        "system",
        lease,
        turn_id="turn-final",
    )

    response_emits = [
        call.args[2]
        for call in store.emit_event.await_args_list
        if call.args[1] == EventType.LLM_RESPONSE
    ]
    assert response_emits, "expected a final LLM_RESPONSE event"
    payload = response_emits[0]
    assert payload["turn_id"] == "turn-final"
    assert payload["finish_reason"] == "budget_exhausted"
    assert "iteration_index" in payload


@pytest.mark.asyncio
async def test_advisor_preflight_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-loop advisor wait is bounded, never unbounded.

    The loop blocks briefly before iteration 1 so guidance can shape the
    executor's plan — but a hung classifier/consult must not hang the
    turn. With the preflight timeout patched tiny and an advisor that
    never resolves, the loop must proceed and finish while the advisor
    task is still pending.
    """
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 200))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(session_store=store)
    # Enable the preflight wait path.
    harness._advisor_client = AsyncMock()
    harness._advisor_model = "advisor-model"
    monkeypatch.setattr(
        "surogates.harness.loop._ADVISOR_PREFLIGHT_TIMEOUT_SECONDS", 0.05,
    )

    advisor_started = asyncio.Event()
    advisor_can_finish = asyncio.Event()

    async def hanging_advisor(*args: Any, **kwargs: Any) -> bool:
        advisor_started.set()
        await advisor_can_finish.wait()
        harness._pending_advisor_messages.append({
            "role": "user",
            "_advisor": True,
            "content": "[Advisor guidance: coding]\nbuffered-by-test",
        })
        return True

    harness._maybe_consult_required_advisor = hanging_advisor

    await asyncio.wait_for(
        _drive_run_loop(
            harness=harness,
            responses=[
                (
                    {"role": "assistant", "content": "Done.", "tool_calls": None},
                    {"model": "test-model", "finish_reason": "stop",
                     "input_tokens": 1, "output_tokens": 2},
                ),
            ],
            monkeypatch=monkeypatch,
        ),
        timeout=5.0,
    )

    await asyncio.sleep(0)
    assert advisor_started.is_set(), \
        "advisor coroutine was never scheduled or never started"
    # The loop finished with the consult still pending — bounded wait.
    pending = [t for t in harness._background_tasks if not t.done()]
    assert pending, "expected the hung advisor task to still be pending"
    advisor_can_finish.set()
    await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_fast_advisor_guidance_reaches_the_first_llm_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guidance produced within the preflight window shapes iteration 1.

    This is the advisor's whole contract — advice that only ever landed
    after the first request would steer a plan already in motion.
    """
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 200))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(session_store=store)
    harness._advisor_client = AsyncMock()
    harness._advisor_model = "advisor-model"

    async def fast_advisor(*args: Any, **kwargs: Any) -> bool:
        harness._pending_advisor_messages.append({
            "role": "user",
            "_advisor": True,
            "content": "[Advisor guidance: coding]\nuse-a-heap",
        })
        return True

    harness._maybe_consult_required_advisor = fast_advisor

    captured_first_request: list[list[dict]] = []

    async def capturing_call_llm(**kwargs: Any) -> tuple[dict, dict]:
        if not captured_first_request:
            create_kwargs = kwargs.get("create_kwargs") or {}
            captured_first_request.append(list(create_kwargs.get("messages") or []))
        return (
            {"role": "assistant", "content": "Done.", "tool_calls": None},
            {"model": "test-model", "finish_reason": "stop",
             "input_tokens": 1, "output_tokens": 2},
        )

    monkeypatch.setattr(
        "surogates.harness.loop.call_llm_with_retry", capturing_call_llm,
    )

    session = _make_session()
    lease = SimpleNamespace(lease_token=uuid4())
    await asyncio.wait_for(
        harness._run_loop(
            session,
            [{"role": "user", "content": "do the task"}],
            "system",
            lease,
            all_events=[],
        ),
        timeout=5.0,
    )

    assert captured_first_request, "no LLM request captured"
    first = captured_first_request[0]
    guidance = [
        m for m in first
        if isinstance(m.get("content"), str)
        and m["content"].startswith("[Advisor guidance:")
    ]
    assert guidance, "guidance did not reach the first LLM request"
    # The internal marker never leaks to the provider payload.
    assert all("_advisor" not in m for m in first)