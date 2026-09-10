"""Agent-loop completion and provider cooldown fallback regressions."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.sandbox.pool import SandboxPool
from surogates.session.models import Session


def _make_harness(**overrides: Any) -> AgentHarness:
    """Create a minimal AgentHarness with mocked dependencies."""
    from surogates.harness.context import ContextCompressor
    from surogates.harness.prompt import PromptBuilder
    from surogates.tenant.context import TenantContext
    from surogates.tools.registry import ToolRegistry

    tenant = TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/test",
    )

    defaults = dict(
        session_store=AsyncMock(),
        tool_registry=ToolRegistry(),
        llm_client=AsyncMock(),
        tenant=tenant,
        worker_id="test-worker",
        budget=IterationBudget(max_total=90),
        context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=MagicMock(spec=PromptBuilder),
        sandbox_pool=MagicMock(spec=SandboxPool),
        vision_client=None,
        vision_model="",
    )
    defaults.update(overrides)

    return AgentHarness(
        session_store=defaults["session_store"],
        tool_registry=defaults["tool_registry"],
        llm_client=defaults["llm_client"],
        tenant=defaults["tenant"],
        worker_id=defaults["worker_id"],
        budget=defaults["budget"],
        context_compressor=defaults["context_compressor"],
        prompt_builder=defaults["prompt_builder"],
        sandbox_pool=defaults["sandbox_pool"],
        vision_client=defaults["vision_client"],
        vision_model=defaults["vision_model"],
    )


def _session_with_config(config: dict[str, Any]) -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-1",
        channel="scheduled" if config.get("scheduled_dynamic_loop") else "web",
        status="active",
        config=config,
        created_at=now,
        updated_at=now,
    )


class TestSessionLifecycle:


    async def test_final_response_completes_primary_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A final no-tool response completes the current objective."""
        store = AsyncMock()
        store.emit_event = AsyncMock(side_effect=[101, 102])
        store.get_events = AsyncMock(return_value=[])

        harness = _make_harness(
            session_store=store,
            budget=IterationBudget(max_total=3),
            context_compressor=SimpleNamespace(context_length=1000),
            prompt_builder=SimpleNamespace(has_agents=False),
            sandbox_pool=None,
        )
        harness._streaming_enabled = False
        harness._prefetch_memory = AsyncMock(return_value="")
        harness._maybe_consult_required_expert = AsyncMock(return_value=None)
        harness._maybe_route_final_response_to_inbox = AsyncMock(return_value=None)
        harness._maybe_generate_title = MagicMock(return_value=None)
        harness._promote_fenced_artifacts = AsyncMock(return_value=None)
        harness._complete_session = AsyncMock(return_value=None)
        harness._end_turn = AsyncMock(return_value=None)

        async def fake_call_llm_with_retry(**_: Any) -> tuple[dict, dict]:
            return (
                {
                    "role": "assistant",
                    "content": "Objective complete.",
                    "tool_calls": None,
                },
                {
                    "model": "test-model",
                    "finish_reason": "stop",
                    "input_tokens": 1,
                    "output_tokens": 2,
                },
            )

        monkeypatch.setattr(
            "surogates.harness.loop.call_llm_with_retry",
            fake_call_llm_with_retry,
        )

        now = datetime.now(timezone.utc)
        session = Session(
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
        lease = SimpleNamespace(lease_token=uuid4())

        await harness._run_loop(
            session,
            [{"role": "user", "content": "Do the task"}],
            "system",
            lease,
            all_events=[],
        )

        harness._complete_session.assert_awaited_once()
        _, kwargs = harness._complete_session.await_args
        assert kwargs["reason"] == "completed"
        assert kwargs["through_event_id"] == 102
        harness._end_turn.assert_not_awaited()


class TestRateLimitWait:
    """A provider cooldown is a wait, not a failure.

    The guard raised on the spot, and the dispatcher then retried the whole
    session three times about seven seconds apart -- so a cooldown of any
    length killed the session before those retries could help. On the
    workspace-bench prod run that cost 15 of 70 tasks, six of them before a
    single tool call, over waits as short as 6 seconds.
    """


    async def test_fallback_is_preferred_over_waiting(self) -> None:
        """A fallback provider is not the one being throttled, so switching
        beats parking the session for minutes."""
        from surogates.harness.llm_call import call_llm_with_retry

        guard = SimpleNamespace(remaining_seconds=AsyncMock(return_value=200.0))
        activated: list[bool] = []

        def activate_fallback() -> bool:
            activated.append(True)
            return True

        calls: list[dict] = []

        async def fake_call(**kw):
            calls.append(kw)
            return ({"role": "assistant", "content": "ok"}, {})

        import surogates.harness.llm_call as m
        orig_ns, orig_sleep = m.call_llm_non_streaming, m.interruptible_sleep
        slept: list[float] = []

        async def fake_sleep(sec, _f):
            slept.append(sec)

        m.call_llm_non_streaming, m.interruptible_sleep = fake_call, fake_sleep
        try:
            msg, _ = await call_llm_with_retry(
                session=SimpleNamespace(id=uuid4()),
                create_kwargs={"model": "m", "messages": []},
                iteration=1, llm_client=AsyncMock(), store=AsyncMock(),
                streaming_enabled=False, interrupt_check=lambda: False,
                activate_fallback=activate_fallback,
                get_current_model=lambda: "fallback-model",
                set_streaming_enabled=lambda _v: None,
                compress_context=lambda *a, **k: None,
                context_compressor=None,
                rate_limit_guard=guard,
            )
        finally:
            m.call_llm_non_streaming, m.interruptible_sleep = orig_ns, orig_sleep

        assert activated, "should have tried the fallback provider"
        assert not slept, "should not wait when a fallback is available"
        assert msg["content"] == "ok"
        # The guard is keyed to the provider we moved off; consulting it again
        # after failing over would strand the session on a stale cooldown.
        assert guard.remaining_seconds.await_count == 1
        assert calls[-1]["create_kwargs"]["model"] == "fallback-model"
