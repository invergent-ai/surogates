"""Ordering fixes in the harness loop: persist after revision, fail through
completion, dedupe before dispatch, per-session interrupt."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.harness.loop_constants import _LENGTH_CONTINUATION_PROMPT
from surogates.session.events import EventType
from surogates.session.models import Session, SessionLease
from tests.test_steer_loop import _make_loop_harness, _make_session


def _snapshot_store() -> AsyncMock:
    """A store whose emit_event copies the payload, like a real row insert."""
    store = AsyncMock()
    store.persisted = []

    async def emit(_sid, etype, data):
        store.persisted.append((etype, copy.deepcopy(data)))
        return 100 + len(store.persisted)

    store.emit_event = AsyncMock(side_effect=emit)
    return store


def _harness(store: AsyncMock | None = None, **attrs: Any) -> AgentHarness:
    store = store or _snapshot_store()
    store.get_events = AsyncMock(return_value=[])
    store.execute = AsyncMock(return_value=None)
    h = _make_loop_harness(session_store=store, budget=IterationBudget(max_total=10))
    h._fail_session = AsyncMock(return_value=None)
    h._try_activate_pro_fallback = MagicMock(return_value=False)
    for k, v in attrs.items():
        setattr(h, k, v)
    return h


def _resp(content: str, finish: str = "stop", **extra: Any) -> tuple[dict, dict]:
    msg = {"role": "assistant", "content": content, "tool_calls": None, **extra}
    usage = {"model": "test-model", "finish_reason": finish,
             "input_tokens": 1, "output_tokens": 1}
    return msg, usage


def _tool_resp(call_id: str, finish: str = "stop", content: str = "") -> tuple[dict, dict]:
    msg = {"role": "assistant", "content": content, "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": "noop", "arguments": "{}"},
    }]}
    return msg, {"model": "test-model", "finish_reason": finish,
                 "input_tokens": 1, "output_tokens": 1}


async def _drive(harness, responses, monkeypatch, messages=None):
    log = iter(responses)

    async def fake_call(**_k):
        try:
            return next(log)
        except StopIteration as exc:
            raise AssertionError("loop drove more iterations than scripted") from exc

    monkeypatch.setattr("surogates.harness.loop.call_llm_with_retry", fake_call)

    async def fake_exec(tool_calls_raw, **_k):
        return [{"role": "tool", "tool_call_id": tc["id"], "content": "ok"}
                for tc in tool_calls_raw]

    monkeypatch.setattr("surogates.harness.loop.execute_tool_calls", fake_exec)
    harness._find_invalid_tool_calls = MagicMock(return_value=[])
    messages = messages if messages is not None else [{"role": "user", "content": "q"}]
    session = _make_session()
    await harness._run_loop(
        session, messages, "system", SimpleNamespace(lease_token=uuid4()), all_events=[],
    )
    return list(harness._store.persisted)


def _responses(emits):
    return [p for t, p in emits if t == EventType.LLM_RESPONSE]


@pytest.mark.asyncio
async def test_length_continuation_prefix_reaches_persisted_response(monkeypatch):
    h = _harness()
    emits = await _drive(h, [_resp("part1", "length"), _resp("part2")], monkeypatch)
    assert _responses(emits)[-1]["message"]["content"] == "part1part2"


@pytest.mark.asyncio
async def test_recovered_conclusion_reaches_persisted_response(monkeypatch):
    h = _harness()
    monkeypatch.setattr(
        "surogates.harness.loop.conclude_from_transcript", AsyncMock(return_value="42"),
    )
    emits = await _drive(h, [_resp("")] * 4, monkeypatch)
    assert _responses(emits)[-1]["message"]["content"] == "42"
    h._fail_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_length_finish_with_complete_tool_call_executes_it(monkeypatch):
    h = _harness()
    messages = [{"role": "user", "content": "q"}]
    await _drive(h, [_tool_resp("c1", finish="length"), _resp("Done.")], monkeypatch, messages)
    assert not any(m.get("content") == _LENGTH_CONTINUATION_PROMPT for m in messages)
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "c1" for m in messages)


@pytest.mark.asyncio
async def test_provider_error_counter_resets_after_a_good_call(monkeypatch):
    h = _harness()
    emits = await _drive(
        h,
        [_resp("", "error"), _resp("", "error"), _tool_resp("c1"),
         _resp("", "error"), _resp("Done.")],
        monkeypatch,
    )
    h._fail_session.assert_not_awaited()
    assert _responses(emits)[-1]["message"]["content"] == "Done."


@pytest.mark.asyncio
async def test_provider_error_with_partial_text_is_retried(monkeypatch):
    h = _harness()
    emits = await _drive(h, [_resp("The three causes are: 1)", "error"), _resp("Done.")], monkeypatch)
    assert _responses(emits)[-1]["message"]["content"] == "Done."
    h._complete_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_in_loop_provider_failure_goes_through_fail_session(monkeypatch):
    h = _harness()
    await _drive(h, [_resp("", "error")] * 3, monkeypatch)
    h._fail_session.assert_awaited_once()
    assert h._fail_session.await_args.kwargs["reason"] == "provider_error"
    h._store.update_session_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_single_tool_forwards_interrupt_check(tmp_path):
    from surogates.harness.tool_exec import execute_single_tool
    from surogates.tools.registry import ToolRegistry, ToolSchema

    seen: dict[str, Any] = {}

    async def handler(arguments: dict, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    registry = ToolRegistry()
    registry.register(
        "probe",
        ToolSchema(name="probe", description="probe", parameters={"type": "object"}),
        handler,
    )
    emitted: list = []

    class Store:
        async def emit_event(self, *a, **k) -> int:
            emitted.append(a)
            return len(emitted)

        async def advance_harness_cursor(self, *a, **k) -> None:
            return None

    now = datetime.now(timezone.utc)
    session = Session(
        id=uuid4(), org_id=uuid4(), agent_id="agent", channel="api", status="running",
        model="m", config={"workspace_path": str(tmp_path)}, created_at=now, updated_at=now,
    )
    lease = SessionLease(session_id=session.id, owner_id="w", lease_token=uuid4(), expires_at=now)
    check = lambda: True  # noqa: E731

    await execute_single_tool(
        {"id": "c1", "function": {"name": "probe", "arguments": "{}"}},
        session=session, lease=lease, store=Store(), tools=registry,
        tenant=SimpleNamespace(), interrupt_check=check,
    )
    assert seen["interrupt_check"] is check


@pytest.mark.asyncio
async def test_coding_agent_cancels_on_the_session_interrupt_only(monkeypatch):
    from surogates.tools.builtin import coding_agent as mod

    captured: dict[str, Any] = {}

    async def fake_run(**kwargs):
        captured["should_cancel"] = kwargs["should_cancel"]
        return SimpleNamespace(status="ok", result=None, branch="b", checkout_dir="d")

    monkeypatch.setattr(mod, "execute_coding_run", fake_run)
    monkeypatch.setattr(mod, "resolve_git_pat", AsyncMock(return_value="pat"))
    monkeypatch.setattr(mod, "_build_ensure", lambda *a, **k: None)
    session = _make_session()
    session.config = {"repos": [{"url": "https://x/y.git", "name": "y", "default_branch": "main"}]}
    store = AsyncMock()
    store.get_session = AsyncMock(return_value=session)
    flag = {"v": False}

    await mod._run_coding_agent_handler(
        {"agent": "claude", "prompt": "fix it"},
        tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4()),
        session_id=str(session.id), session_store=store,
        sandbox_pool=MagicMock(), credential_vault=MagicMock(),
        interrupt_check=lambda: flag["v"],
    )
    assert captured["should_cancel"]() is False
    flag["v"] = True
    assert captured["should_cancel"]() is True
