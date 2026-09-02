"""Tests for production-hardening features in surogates.harness.loop.

Covers: retry helpers, response validation, tool result truncation,
length continuation, budget pressure warnings, invalid tool call recovery,
and the retry/fallback integration.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.credentials import CredentialPool, PooledCredential
from surogates.harness.llm_call import (
    extract_retry_after as _extract_retry_after,
    extract_status_code as _extract_status_code,
    interruptible_sleep as _interruptible_sleep,
    is_transient_error as _is_transient_error,
)
from surogates.harness.loop import AgentHarness
from surogates.harness.resilience import try_rotate_credential
from surogates.sandbox.pool import SandboxPool
from surogates.session.models import Session, SessionLease


# ---------------------------------------------------------------------------
# _extract_status_code
# ---------------------------------------------------------------------------


class TestExtractStatusCode:
    def test_from_status_code_attr(self) -> None:
        exc = Exception("rate limited")
        exc.status_code = 429  # type: ignore[attr-defined]
        assert _extract_status_code(exc) == 429

    def test_from_response_attr(self) -> None:
        exc = Exception("server error")
        exc.response = SimpleNamespace(status_code=502)  # type: ignore[attr-defined]
        assert _extract_status_code(exc) == 502

    def test_no_status_returns_none(self) -> None:
        exc = Exception("generic error")
        assert _extract_status_code(exc) is None

    def test_string_status_code_converted(self) -> None:
        exc = Exception("err")
        exc.status_code = "503"  # type: ignore[attr-defined]
        assert _extract_status_code(exc) == 503


# ---------------------------------------------------------------------------
# _is_transient_error
# ---------------------------------------------------------------------------


class TestIsTransientError:
    def test_connection_error(self) -> None:
        assert _is_transient_error(ConnectionError("reset")) is True

    def test_timeout_error(self) -> None:
        assert _is_transient_error(TimeoutError("timed out")) is True

    def test_generic_exception_not_transient(self) -> None:
        assert _is_transient_error(Exception("unknown")) is False

    def test_value_error_not_transient(self) -> None:
        assert _is_transient_error(ValueError("bad value")) is False


# ---------------------------------------------------------------------------
# _extract_retry_after
# ---------------------------------------------------------------------------


class TestExtractRetryAfter:
    def test_from_headers(self) -> None:
        exc = Exception("rate limited")
        exc.response = SimpleNamespace(  # type: ignore[attr-defined]
            headers={"retry-after": "5.0"},
        )
        assert _extract_retry_after(exc) == 5.0

    def test_from_body(self) -> None:
        exc = Exception("rate limited")
        exc.response = SimpleNamespace(headers={})  # type: ignore[attr-defined]
        exc.body = {"error": {"retry_after": 10.0}}  # type: ignore[attr-defined]
        assert _extract_retry_after(exc) == 10.0

    def test_capped_at_120(self) -> None:
        exc = Exception("rate limited")
        exc.response = SimpleNamespace(  # type: ignore[attr-defined]
            headers={"retry-after": "300"},
        )
        assert _extract_retry_after(exc) == 120.0

    def test_no_retry_after(self) -> None:
        exc = Exception("rate limited")
        assert _extract_retry_after(exc) is None

    def test_invalid_header_returns_none(self) -> None:
        exc = Exception("rate limited")
        exc.response = SimpleNamespace(  # type: ignore[attr-defined]
            headers={"retry-after": "not-a-number"},
        )
        assert _extract_retry_after(exc) is None


class TestCredentialRotation:
    def test_single_credential_rate_limit_does_not_mark_exhausted(self) -> None:
        pool = CredentialPool([PooledCredential(id="only", api_key="sk-only")])
        llm_client = SimpleNamespace(base_url="https://api.example.com")

        new_client, rotated = try_rotate_credential(
            pool,
            llm_client,  # type: ignore[arg-type]
            429,
            Exception("rate limited"),
        )

        assert rotated is False
        assert new_client is None
        current = pool.current()
        assert current is not None
        assert current.id == "only"
        assert current.status == "ok"


# ---------------------------------------------------------------------------
# _interruptible_sleep
# ---------------------------------------------------------------------------


class TestInterruptibleSleep:
    async def test_sleeps_for_duration(self) -> None:
        import time
        start = time.monotonic()
        await _interruptible_sleep(0.3, lambda: False)
        elapsed = time.monotonic() - start
        assert elapsed >= 0.25

    async def test_interrupted_early(self) -> None:
        import time
        start = time.monotonic()
        await _interruptible_sleep(5.0, lambda: True)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0

    async def test_non_callable_flag_true(self) -> None:
        import time
        start = time.monotonic()
        await _interruptible_sleep(5.0, True)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0

    async def test_non_callable_flag_false(self) -> None:
        import time
        start = time.monotonic()
        await _interruptible_sleep(0.3, False)
        elapsed = time.monotonic() - start
        assert elapsed >= 0.25


# ---------------------------------------------------------------------------
# AgentHarness -- helpers that don't need a full loop
# ---------------------------------------------------------------------------


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


def _chat_response(content: str, *, model: str = "test-model") -> SimpleNamespace:
    return SimpleNamespace(
        model=model,
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    model_dump=lambda **_kwargs: {
                        "role": "assistant",
                        "content": content,
                    }
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
        ),
    )


class TestDynamicLoopToolPolicy:
    """Dynamic loop sessions expose loop_wait but not scheduler creation tools."""

    def test_explicit_allowed_tools_honours_omission_of_ask_user_question(
        self,
    ) -> None:
        """An explicit ``allowed_tools`` is admin-authored contract --
        it must be honoured verbatim.  ``_ensure_always_available_tools``
        used to force-add ``ask_user_question`` to every allowlist,
        which silently defeated AgentDef contracts (e.g. the
        research-writer's ``[research_memory, create_artifact]``
        gained an ask_user_question the writer then used to stall the
        workflow asking format questions instead of producing the
        report).  Explicit lists are now respected."""
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        for name in ("ask_user_question", "web_search"):
            reg.register(
                name,
                ToolSchema(name=name, description="test", parameters={}),
                lambda _: "{}",
            )
        harness = _make_harness(tool_registry=reg)
        session = _session_with_config({"allowed_tools": ["web_search"]})

        tool_names = harness._tool_filter_for_session(session)

        assert tool_names == {"web_search"}

    def test_implicit_filter_still_adds_ask_user_question(self) -> None:
        """When the session has NO explicit allowlist, the
        always-available behaviour stays: an interactive session
        needs ask_user_question to be able to surface a clarifying
        prompt to the user, and there's no admin contract to honour."""
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        for name in ("ask_user_question", "web_search", "read_file"):
            reg.register(
                name,
                ToolSchema(name=name, description="test", parameters={}),
                lambda _: "{}",
            )
        harness = _make_harness(tool_registry=reg)
        # No allowed_tools / excluded_tools -- the implicit
        # "everything except worker-excluded" filter applies.
        session = _session_with_config({})

        tool_names = harness._tool_filter_for_session(session)

        assert tool_names is not None
        assert "ask_user_question" in tool_names

    def test_excluded_tools_cannot_remove_ask_user_question(self) -> None:
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        for name in ("ask_user_question", "web_search"):
            reg.register(
                name,
                ToolSchema(name=name, description="test", parameters={}),
                lambda _: "{}",
            )
        harness = _make_harness(tool_registry=reg)
        session = _session_with_config({"excluded_tools": ["ask_user_question"]})

        tool_names = harness._tool_filter_for_session(session)

        assert tool_names is not None
        assert "ask_user_question" in tool_names
        assert "web_search" in tool_names

    def test_coordinator_without_strict_flag_keeps_all_tools(self) -> None:
        """Legacy behaviour: a coordinator session with no strict flag
        and no excluded_tools sees the full registry.  This protects
        AgentDef-driven coordinators from a silent shape change."""
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        for name in ("spawn_task", "terminal", "read_file"):
            reg.register(
                name,
                ToolSchema(name=name, description="test", parameters={}),
                lambda _: "{}",
            )
        harness = _make_harness(tool_registry=reg)
        session = _session_with_config({"coordinator": True})

        tool_names = harness._tool_filter_for_session(session)

        # The execution-context gates materialise the implicit "everything"
        # filter so they can subtract from it, so the answer is the whole
        # registry rather than ``None`` — the LLM still sees every tool.
        assert tool_names == {"spawn_task", "terminal", "read_file"}

    def test_strict_coordinator_strips_implementation_tools(self) -> None:
        """/mission sets strict_coordinator=True so implementation tools
        (terminal, read_file, write_file, browser_*, web_*, vision_analyze,
        kb_*, create_artifact, list_files, search_files, patch, process)
        plus the task_id-gated self-tools (worker_context, worker_block,
        worker_complete) are stripped — the LLM has to delegate via
        spawn_task/delegate_task and cannot call self-tools that would
        target a non-existent ``Session.task_id``."""
        from surogates.tools.builtin.coordinator import (
            COORDINATOR_IMPLEMENTATION_TOOLS,
        )
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        registered = {
            "spawn_task", "delegate_task", "memory",
            "terminal", "read_file", "write_file", "browser_navigate",
            "web_search", "vision_analyze", "create_artifact",
            "worker_context", "worker_block", "worker_complete",
        }
        for name in registered:
            reg.register(
                name,
                ToolSchema(name=name, description="test", parameters={}),
                lambda _: "{}",
            )
        harness = _make_harness(tool_registry=reg)
        session = _session_with_config({
            "coordinator": True,
            "strict_coordinator": True,
        })

        tool_names = harness._tool_filter_for_session(session)

        assert tool_names is not None
        # Coordination + reasoning tools survive.
        assert "spawn_task" in tool_names
        assert "delegate_task" in tool_names
        assert "memory" in tool_names
        # Implementation tools are stripped.
        for stripped in COORDINATOR_IMPLEMENTATION_TOOLS & registered:
            assert stripped not in tool_names, (
                f"{stripped} should be stripped from strict coordinator"
            )
        # task_id-gated self-tools must be stripped — a coordinator has no
        # task_id, so these would error or no-op if the LLM tried to call them.
        assert "worker_context" not in tool_names
        assert "worker_block" not in tool_names
        assert "worker_complete" not in tool_names

    def test_strict_coordinator_combines_with_user_excluded_tools(self) -> None:
        """Operator-supplied ``excluded_tools`` adds to the implementation
        strip set — both are honored, not one or the other."""
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        for name in ("spawn_task", "delegate_task", "todo", "terminal"):
            reg.register(
                name,
                ToolSchema(name=name, description="test", parameters={}),
                lambda _: "{}",
            )
        harness = _make_harness(tool_registry=reg)
        session = _session_with_config({
            "coordinator": True,
            "strict_coordinator": True,
            "excluded_tools": ["todo"],
        })

        tool_names = harness._tool_filter_for_session(session)

        assert tool_names is not None
        assert "spawn_task" in tool_names
        assert "delegate_task" in tool_names
        # Operator excluded 'todo', strict-coordinator excluded 'terminal'.
        assert "todo" not in tool_names
        assert "terminal" not in tool_names


class TestSessionLifecycle:
    async def test_final_summary_describes_images_before_non_vision_model(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        monkeypatch.setenv("SUROGATES_CONFIG", str(tmp_path / "missing-config.yaml"))
        monkeypatch.setenv("SUROGATES_LLM_VISION_MODEL", "gpt-4o-mini")

        create = AsyncMock(
            side_effect=[
                _chat_response("A red square with the label OK.", model="gpt-4o-mini"),
                _chat_response("Final summary.", model="deepseek-chat"),
            ]
        )
        llm_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        store = AsyncMock()
        store.emit_event = AsyncMock(side_effect=[101, 102, 103])
        harness = _make_harness(
            session_store=store,
            llm_client=llm_client,
            sandbox_pool=None,
        )
        harness._streaming_enabled = False
        session = _session_with_config({})
        session.model = "deepseek-chat"
        now = datetime.now(timezone.utc)
        lease = SessionLease(
            session_id=session.id,
            owner_id="worker",
            lease_token=uuid4(),
            expires_at=now,
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,AAA=",
                            "detail": "auto",
                        },
                    },
                ],
            }
        ]

        await harness._request_final_summary(
            session=session,
            messages=messages,
            system_prompt="System prompt",
            lease=lease,
        )

        assert create.await_count == 2
        vision_call = create.await_args_list[0].kwargs
        assert vision_call["model"] == "gpt-4o-mini"
        assert any(
            part.get("type") == "image_url"
            for part in vision_call["messages"][0]["content"]
        )
        base_call = create.await_args_list[1].kwargs
        assert base_call["model"] == "deepseek-chat"
        assert "A red square with the label OK." in str(base_call["messages"])
        assert "image_url" not in str(base_call["messages"])
        assert isinstance(base_call["messages"][1]["content"], str)

    async def test_final_summary_routes_image_description_to_vision_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        monkeypatch.setenv("SUROGATES_CONFIG", str(tmp_path / "missing-config.yaml"))
        # Intentionally leave SUROGATES_LLM_VISION_MODEL unset -- the
        # injected vision client + model_override should take precedence
        # over the load_settings() fallback.

        main_create = AsyncMock(
            side_effect=[_chat_response("Final summary.", model="deepseek-chat")]
        )
        vision_create = AsyncMock(
            side_effect=[_chat_response("A red square.", model="surogate")]
        )
        main_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=main_create))
        )
        vision_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=vision_create))
        )

        store = AsyncMock()
        store.emit_event = AsyncMock(side_effect=[101, 102, 103])
        harness = _make_harness(
            session_store=store,
            llm_client=main_client,
            sandbox_pool=None,
            vision_client=vision_client,
            vision_model="surogate",
        )
        harness._streaming_enabled = False
        session = _session_with_config({})
        session.model = "deepseek-chat"
        now = datetime.now(timezone.utc)
        lease = SessionLease(
            session_id=session.id,
            owner_id="worker",
            lease_token=uuid4(),
            expires_at=now,
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,AAA=",
                            "detail": "auto",
                        },
                    },
                ],
            }
        ]

        await harness._request_final_summary(
            session=session,
            messages=messages,
            system_prompt="System prompt",
            lease=lease,
        )

        # Image description must go through the dedicated vision client
        # (different endpoint), not the main LLM client.
        assert vision_create.await_count == 1
        vision_call = vision_create.await_args_list[0].kwargs
        assert vision_call["model"] == "surogate"

        # Main client receives only the text-substituted summary call.
        assert main_create.await_count == 1
        base_call = main_create.await_args_list[0].kwargs
        assert base_call["model"] == "deepseek-chat"
        assert "A red square." in str(base_call["messages"])
        assert "image_url" not in str(base_call["messages"])

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

    def test_dynamic_loop_sessions_cannot_create_nested_cron_schedules(self) -> None:
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        for name in (
            "ask_user_question",
            "cron_create",
            "cron_delete",
            "cron_list",
            "loop_wait",
            "web_search",
        ):
            reg.register(
                name,
                ToolSchema(name=name, description="test", parameters={}),
                lambda _: "{}",
            )
        harness = _make_harness(tool_registry=reg)
        session = _session_with_config({
            "scheduled_dynamic_loop": True,
            "scheduled_session_id": str(uuid4()),
        })

        tool_names = harness._tool_filter_for_session(session)

        assert tool_names is not None
        assert "ask_user_question" in tool_names
        assert "loop_wait" in tool_names
        assert "web_search" in tool_names
        assert "cron_create" not in tool_names
        assert "cron_delete" not in tool_names
        assert "cron_list" not in tool_names

    def test_fixed_cron_loop_sessions_cannot_create_nested_cron_schedules(self) -> None:
        """Fixed-cron ``/loop`` children must also be barred from spawning schedules.

        Regression for the case where a ``/loop 1m`` child invoked
        ``cron_create`` from inside its wake and built a parallel cron
        job for the same task.
        """
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        for name in (
            "ask_user_question",
            "cron_create",
            "cron_delete",
            "cron_list",
            "loop_complete",
            "loop_wait",
            "web_search",
        ):
            reg.register(
                name,
                ToolSchema(name=name, description="test", parameters={}),
                lambda _: "{}",
            )
        harness = _make_harness(tool_registry=reg)
        session = _session_with_config({
            "scheduled_dynamic_loop": False,
            "scheduled_session_id": str(uuid4()),
            "scheduled_source": "loop",
        })

        tool_names = harness._tool_filter_for_session(session)

        assert tool_names is not None
        assert "ask_user_question" in tool_names
        assert "web_search" in tool_names
        assert "cron_create" not in tool_names
        assert "cron_delete" not in tool_names
        assert "cron_list" not in tool_names
        # No loop_wait for fixed-cron loops — the schedule controls cadence.
        assert "loop_wait" not in tool_names
        # Fixed-cron children get loop_complete so they can self-terminate.
        assert "loop_complete" in tool_names

    def test_dynamic_loop_sessions_do_not_get_loop_complete(self) -> None:
        """Dynamic loops self-terminate via loop_wait(completed=true)."""
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        for name in ("ask_user_question", "loop_complete", "loop_wait"):
            reg.register(
                name,
                ToolSchema(name=name, description="test", parameters={}),
                lambda _: "{}",
            )
        harness = _make_harness(tool_registry=reg)
        session = _session_with_config({
            "scheduled_dynamic_loop": True,
            "scheduled_session_id": str(uuid4()),
        })

        tool_names = harness._tool_filter_for_session(session)

        assert tool_names is not None
        assert "loop_wait" in tool_names
        assert "loop_complete" not in tool_names

    def test_non_scheduled_sessions_keep_cron_tools(self) -> None:
        """A regular (non-scheduled) session must still be able to use cron tools."""
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        for name in (
            "ask_user_question",
            "cron_create",
            "cron_delete",
            "cron_list",
            "loop_complete",
            "loop_wait",
        ):
            reg.register(
                name,
                ToolSchema(name=name, description="test", parameters={}),
                lambda _: "{}",
            )
        harness = _make_harness(tool_registry=reg)
        session = _session_with_config({})

        tool_names = harness._tool_filter_for_session(session)

        assert tool_names is not None
        assert "cron_create" in tool_names
        assert "cron_delete" in tool_names
        assert "cron_list" in tool_names
        # ``loop_wait`` and ``loop_complete`` belong to scheduled-child
        # sessions only; a regular session must not see them.
        assert "loop_wait" not in tool_names
        assert "loop_complete" not in tool_names

    async def test_final_response_needing_user_input_becomes_ask_user_question_call(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "needs_ask_user_question": True,
                            "reason": "user_decision",
                            "question": "Which account should I use?",
                            "context": "I need the user to choose an account.",
                        })
                    )
                )
            ]
        )
        llm_client = AsyncMock()
        llm_client.chat.completions.create.return_value = response
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        reg.register(
            "ask_user_question",
            ToolSchema(name="ask_user_question", description="test", parameters={}),
            lambda _: "{}",
        )
        harness = _make_harness(llm_client=llm_client, tool_registry=reg)
        session = _session_with_config({})
        assistant_message = {
            "role": "assistant",
            "content": "Which account should I use?",
            "tool_calls": None,
        }

        converted = await harness._maybe_convert_final_response_to_ask_user_question(
            session=session,
            messages=[{"role": "user", "content": "Post a status update"}],
            assistant_message=assistant_message,
            model="surogate",
            tool_filter={"ask_user_question", "browser_navigate"},
        )

        assert converted is True
        tool_calls = assistant_message["tool_calls"]
        assert tool_calls is not None
        assert tool_calls[0]["function"]["name"] == "ask_user_question"
        arguments = json.loads(tool_calls[0]["function"]["arguments"])
        assert arguments["questions"][0]["prompt"] == "Which account should I use?"
        assert arguments["context"] == "I need the user to choose an account."

    async def test_final_response_needing_browser_action_emits_action_required(
        self,
    ) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "action_kind": "action_required",
                            "reason": "browser_login",
                            "title": "Sign in required",
                            "instructions": "Open the browser session and complete sign-in.",
                            "context": "The browser is showing a login page.",
                            "action_type": "browser",
                            "target": "browser",
                        })
                    )
                )
            ]
        )
        llm_client = AsyncMock()
        llm_client.chat.completions.create.return_value = response
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        reg.register(
            "ask_user_question",
            ToolSchema(name="ask_user_question", description="test", parameters={}),
            lambda _: "{}",
        )
        store = AsyncMock()
        harness = _make_harness(
            llm_client=llm_client,
            tool_registry=reg,
            session_store=store,
        )
        session = _session_with_config({})
        assistant_message = {
            "role": "assistant",
            "content": "Please sign in in the browser so I can continue.",
            "tool_calls": None,
        }

        routed = await harness._maybe_route_final_response_to_inbox(
            session=session,
            messages=[{"role": "user", "content": "Pay this invoice"}],
            assistant_message=assistant_message,
            model="surogate",
            tool_filter={"ask_user_question", "browser_navigate"},
        )

        assert routed == "action_required"
        assert assistant_message["tool_calls"] is None
        store.emit_event.assert_awaited_once()
        event_args = store.emit_event.await_args.args
        assert event_args[0] == session.id
        assert event_args[1].value == "inbox.action_required"
        assert event_args[2]["title"] == "Sign in required"
        assert event_args[2]["action_type"] == "browser"

    async def test_final_response_routes_to_inbox_for_service_account_session(
        self,
    ) -> None:
        """Ops sessions are service-account-owned (user_id None); they get the
        same judge-rescue routing as user sessions so a plain-text 'needs you'
        final answer still becomes an inbox item."""
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "action_kind": "action_required",
                            "reason": "browser_login",
                            "title": "Sign in required",
                            "instructions": "Open the browser and complete sign-in.",
                            "context": "The browser is showing a login page.",
                            "action_type": "browser",
                            "target": "browser",
                        })
                    )
                )
            ]
        )
        llm_client = AsyncMock()
        llm_client.chat.completions.create.return_value = response
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        reg.register(
            "ask_user_question",
            ToolSchema(name="ask_user_question", description="test", parameters={}),
            lambda _: "{}",
        )
        store = AsyncMock()
        harness = _make_harness(
            llm_client=llm_client,
            tool_registry=reg,
            session_store=store,
        )
        now = datetime.now(timezone.utc)
        session = Session(
            id=uuid4(),
            user_id=None,
            service_account_id=uuid4(),
            org_id=uuid4(),
            agent_id="agent-1",
            # Operator ("ops chat") sessions are service-account owned and
            # carry channel 'studio'.  They were indistinguishable from
            # third-party 'api' traffic when this rescue was added; the
            # 'studio' channel split them apart afterwards.
            channel="studio",
            status="active",
            config={},
            created_at=now,
            updated_at=now,
        )
        assistant_message = {
            "role": "assistant",
            "content": "Please sign in in the browser so I can continue.",
            "tool_calls": None,
        }

        routed = await harness._maybe_route_final_response_to_inbox(
            session=session,
            messages=[{"role": "user", "content": "Pay this invoice"}],
            assistant_message=assistant_message,
            model="surogate",
            tool_filter={"ask_user_question", "browser_navigate"},
        )

        assert routed == "action_required"
        store.emit_event.assert_awaited_once()
        assert store.emit_event.await_args.args[1].value == "inbox.action_required"

    async def test_final_response_judge_skipped_for_api_channel(self) -> None:
        """A third-party API integration reads the response off the API.

        An inbox item minted there is a row nothing ever clears, and the
        judge costs blocking main-model requests at the end of every turn,
        so the whole path must be skipped before the LLM is consulted.
        """
        llm_client = AsyncMock()
        store = AsyncMock()
        harness = _make_harness(llm_client=llm_client, session_store=store)
        now = datetime.now(timezone.utc)
        session = Session(
            id=uuid4(),
            user_id=None,
            service_account_id=uuid4(),
            org_id=uuid4(),
            agent_id="agent-1",
            channel="api",
            status="active",
            config={},
            created_at=now,
            updated_at=now,
        )

        routed = await harness._maybe_route_final_response_to_inbox(
            session=session,
            messages=[{"role": "user", "content": "Pay this invoice"}],
            assistant_message={
                "role": "assistant",
                "content": "Please sign in in the browser so I can continue.",
                "tool_calls": None,
            },
            model="surogate",
            tool_filter={"ask_user_question", "browser_navigate"},
        )

        assert routed is None
        llm_client.chat.completions.create.assert_not_awaited()
        store.emit_event.assert_not_awaited()

    async def test_user_action_judge_prefers_outlines_structured_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[dict[str, Any]] = []

        async def fake_generate_structured(
            *,
            llm_client: Any,
            model: str,
            messages: list[dict[str, str]],
        ) -> dict[str, Any]:
            calls.append({
                "llm_client": llm_client,
                "model": model,
                "messages": messages,
            })
            return {
                "needs_ask_user_question": True,
                "reason": "login_required",
                "question": "Please sign in and tell me when to continue.",
                "context": "The browser is asking the user to sign in.",
            }

        monkeypatch.setattr(
            "surogates.harness.loop._generate_user_action_rescue_structured",
            fake_generate_structured,
            raising=False,
        )
        llm_client = AsyncMock()
        llm_client.chat.completions.create.side_effect = AssertionError(
            "raw JSON fallback should not run when Outlines succeeds"
        )
        harness = _make_harness(llm_client=llm_client)

        decision = await harness._judge_final_response_needs_ask_user_question(
            messages=[{"role": "user", "content": "Pay the invoice"}],
            assistant_content="The page is asking you to sign in before I can continue.",
            model="surogate",
        )

        assert decision["action_kind"] == "action_required"
        assert decision["needs_ask_user_question"] is False
        assert decision["reason"] == "login_required"
        assert decision["question"] == "Please sign in and tell me when to continue."
        assert decision["context"] == "The browser is asking the user to sign in."
        assert decision["action_type"] == "browser"
        assert decision["target"] == "browser"
        assert calls
        assert calls[0]["model"] == "surogate"

    async def test_ask_user_question_judge_falls_back_when_outlines_unavailable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_generate_structured(
            *,
            llm_client: Any,
            model: str,
            messages: list[dict[str, str]],
        ) -> None:
            return None

        monkeypatch.setattr(
            "surogates.harness.loop._generate_user_action_rescue_structured",
            fake_generate_structured,
            raising=False,
        )
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "needs_ask_user_question": False,
                            "reason": "none",
                            "question": None,
                            "context": None,
                        })
                    )
                )
            ]
        )
        llm_client = AsyncMock()
        llm_client.chat.completions.create.return_value = response
        harness = _make_harness(llm_client=llm_client)

        decision = await harness._judge_final_response_needs_ask_user_question(
            messages=[{"role": "user", "content": "Pay the invoice"}],
            assistant_content="Done. I paid the invoice.",
            model="surogate",
        )

        assert decision["needs_ask_user_question"] is False
        llm_client.chat.completions.create.assert_awaited_once()

    async def test_final_response_without_user_input_is_left_alone(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "needs_ask_user_question": False,
                            "reason": "none",
                            "question": None,
                            "context": None,
                        })
                    )
                )
            ]
        )
        llm_client = AsyncMock()
        llm_client.chat.completions.create.return_value = response
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        reg.register(
            "ask_user_question",
            ToolSchema(name="ask_user_question", description="test", parameters={}),
            lambda _: "{}",
        )
        harness = _make_harness(llm_client=llm_client, tool_registry=reg)
        session = _session_with_config({})
        assistant_message = {
            "role": "assistant",
            "content": "Done. I posted the update.",
            "tool_calls": None,
        }

        converted = await harness._maybe_convert_final_response_to_ask_user_question(
            session=session,
            messages=[{"role": "user", "content": "Post a status update"}],
            assistant_message=assistant_message,
            model="surogate",
            tool_filter={"ask_user_question", "browser_navigate"},
        )

        assert converted is False
        assert assistant_message["tool_calls"] is None

    async def test_final_response_ask_user_question_judge_retries_after_empty_response(
        self,
    ) -> None:
        empty_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=""))]
        )
        json_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "needs_ask_user_question": True,
                            "reason": "missing_information",
                            "question": "What account should I use?",
                            "context": "The task cannot continue without an account.",
                        })
                    )
                )
            ]
        )
        llm_client = AsyncMock()
        llm_client.chat.completions.create.side_effect = [
            empty_response,
            json_response,
        ]
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        reg.register(
            "ask_user_question",
            ToolSchema(name="ask_user_question", description="test", parameters={}),
            lambda _: "{}",
        )
        harness = _make_harness(llm_client=llm_client, tool_registry=reg)
        session = _session_with_config({})
        assistant_message = {
            "role": "assistant",
            "content": "What account should I use?",
            "tool_calls": None,
        }

        converted = await harness._maybe_convert_final_response_to_ask_user_question(
            session=session,
            messages=[{"role": "user", "content": "Post this update"}],
            assistant_message=assistant_message,
            model="surogate",
            tool_filter={"ask_user_question"},
        )

        assert converted is True
        assert llm_client.chat.completions.create.await_count == 2
        tool_calls = assistant_message["tool_calls"]
        assert tool_calls is not None
        arguments = json.loads(tool_calls[0]["function"]["arguments"])
        assert arguments["questions"][0]["prompt"] == "What account should I use?"

    async def test_final_response_ask_user_question_judge_reads_reasoning_content(
        self,
    ) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        reasoning_content=json.dumps({
                            "needs_ask_user_question": True,
                            "reason": "user_decision",
                            "question": "Should I continue?",
                            "context": "The assistant needs a decision.",
                        }),
                    )
                )
            ]
        )
        llm_client = AsyncMock()
        llm_client.chat.completions.create.return_value = response
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        reg.register(
            "ask_user_question",
            ToolSchema(name="ask_user_question", description="test", parameters={}),
            lambda _: "{}",
        )
        harness = _make_harness(llm_client=llm_client, tool_registry=reg)
        session = _session_with_config({})
        assistant_message = {
            "role": "assistant",
            "content": "Should I continue?",
            "tool_calls": None,
        }

        converted = await harness._maybe_convert_final_response_to_ask_user_question(
            session=session,
            messages=[{"role": "user", "content": "Run the task"}],
            assistant_message=assistant_message,
            model="surogate",
            tool_filter={"ask_user_question"},
        )

        assert converted is True
        assert llm_client.chat.completions.create.await_count == 1
        tool_calls = assistant_message["tool_calls"]
        assert tool_calls is not None
        arguments = json.loads(tool_calls[0]["function"]["arguments"])
        assert arguments["questions"][0]["prompt"] == "Should I continue?"

    async def test_final_response_fallback_does_not_convert_plain_final_answer(
        self,
    ) -> None:
        empty_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=""))]
        )
        llm_client = AsyncMock()
        llm_client.chat.completions.create.side_effect = [
            empty_response,
            empty_response,
        ]
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        reg.register(
            "ask_user_question",
            ToolSchema(name="ask_user_question", description="test", parameters={}),
            lambda _: "{}",
        )
        harness = _make_harness(llm_client=llm_client, tool_registry=reg)
        session = _session_with_config({})
        assistant_message = {
            "role": "assistant",
            "content": "Done. I posted the update.",
            "tool_calls": None,
        }

        converted = await harness._maybe_convert_final_response_to_ask_user_question(
            session=session,
            messages=[{"role": "user", "content": "Post this update"}],
            assistant_message=assistant_message,
            model="surogate",
            tool_filter={"ask_user_question"},
        )

        assert converted is False
        assert assistant_message["tool_calls"] is None

    async def test_final_response_fallback_does_not_convert_answer_with_headline_question_words(
        self,
    ) -> None:
        empty_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=""))]
        )
        llm_client = AsyncMock()
        llm_client.chat.completions.create.side_effect = [
            empty_response,
            empty_response,
        ]
        from surogates.tools.registry import ToolRegistry, ToolSchema

        reg = ToolRegistry()
        reg.register(
            "ask_user_question",
            ToolSchema(name="ask_user_question", description="test", parameters={}),
            lambda _: "{}",
        )
        harness = _make_harness(llm_client=llm_client, tool_registry=reg)
        session = _session_with_config({})
        assistant_message = {
            "role": "assistant",
            "content": (
                "The main news headline on HotNews.ro is: "
                "\"Ilie Bolojan, mesaj transant despre varianta unui guvern "
                "condus de premierul Grindeanu. Ce va face PNL\". "
                "It is about what the PNL will do next."
            ),
            "tool_calls": None,
        }

        converted = await harness._maybe_convert_final_response_to_ask_user_question(
            session=session,
            messages=[
                {
                    "role": "user",
                    "content": "Open hotnews.ro and tell me the main headline",
                }
            ],
            assistant_message=assistant_message,
            model="surogate",
            tool_filter={"ask_user_question"},
        )

        assert converted is False
        assert assistant_message["tool_calls"] is None

    def test_successful_loop_wait_is_terminal_for_dynamic_loop_run(self) -> None:
        harness = _make_harness()
        session = _session_with_config({
            "scheduled_dynamic_loop": True,
            "scheduled_session_id": str(uuid4()),
        })
        tool_calls = [
            {
                "id": "call-wait",
                "function": {
                    "name": "loop_wait",
                    "arguments": '{"delay_seconds":300,"reason":"done"}',
                },
            }
        ]
        tool_results = [
            {
                "role": "tool",
                "tool_call_id": "call-wait",
                "content": '{"success": true, "delay_seconds": 300}',
            }
        ]

        assert harness._dynamic_loop_wait_succeeded(
            session, tool_calls, tool_results,
        ) is True

    def test_failed_loop_wait_is_not_terminal(self) -> None:
        harness = _make_harness()
        session = _session_with_config({
            "scheduled_dynamic_loop": True,
            "scheduled_session_id": str(uuid4()),
        })
        tool_calls = [
            {
                "id": "call-wait",
                "function": {"name": "loop_wait", "arguments": "{}"},
            }
        ]
        tool_results = [
            {
                "role": "tool",
                "tool_call_id": "call-wait",
                "content": '{"success": false, "error": "bad delay"}',
            }
        ]

        assert harness._dynamic_loop_wait_succeeded(
            session, tool_calls, tool_results,
        ) is False


class TestFindInvalidToolCalls:
    """Tests for _find_invalid_tool_calls."""

    def test_no_invalid_calls(self) -> None:
        from surogates.tools.registry import ToolRegistry, ToolSchema
        reg = ToolRegistry()
        reg.register("my_tool", ToolSchema(name="my_tool", description="test", parameters={}), lambda x: x)
        harness = _make_harness(tool_registry=reg)

        tool_calls = [
            {"id": "1", "function": {"name": "my_tool", "arguments": '{"x": 1}'}},
        ]
        invalid = harness._find_invalid_tool_calls(tool_calls)
        assert invalid == []

    def test_unknown_tool_name(self) -> None:
        harness = _make_harness()
        tool_calls = [
            {"id": "1", "function": {"name": "nonexistent_tool", "arguments": "{}"}},
        ]
        invalid = harness._find_invalid_tool_calls(tool_calls)
        assert len(invalid) == 1
        tc, error_msg = invalid[0]
        assert tc["id"] == "1"
        assert "Unknown tool" in error_msg

    def test_malformed_json_arguments(self) -> None:
        from surogates.tools.registry import ToolRegistry, ToolSchema
        reg = ToolRegistry()
        reg.register("my_tool", ToolSchema(name="my_tool", description="test", parameters={}), lambda x: x)
        harness = _make_harness(tool_registry=reg)

        tool_calls = [
            {"id": "1", "function": {"name": "my_tool", "arguments": "{bad json}"}},
        ]
        invalid = harness._find_invalid_tool_calls(tool_calls)
        assert len(invalid) == 1
        tc, error_msg = invalid[0]
        assert "Malformed JSON" in error_msg

    def test_empty_arguments_is_valid(self) -> None:
        from surogates.tools.registry import ToolRegistry, ToolSchema
        reg = ToolRegistry()
        reg.register("my_tool", ToolSchema(name="my_tool", description="test", parameters={}), lambda x: x)
        harness = _make_harness(tool_registry=reg)

        tool_calls = [
            {"id": "1", "function": {"name": "my_tool", "arguments": ""}},
        ]
        invalid = harness._find_invalid_tool_calls(tool_calls)
        assert invalid == []


class TestInjectBudgetWarning:
    """Tests for _inject_budget_warning (two-tier system)."""

    def test_no_warning_when_budget_healthy(self) -> None:
        harness = _make_harness(budget=IterationBudget(max_total=100))
        results = [{"role": "tool", "tool_call_id": "1", "content": "ok"}]
        out = harness._inject_budget_warning(results)
        assert "[BUDGET" not in out[0]["content"]

    def test_caution_injected_at_70_percent(self) -> None:
        budget = IterationBudget(max_total=100)
        # Consume 80 iterations -> 80% used (caution tier)
        for _ in range(80):
            budget.consume()
        harness = _make_harness(budget=budget)

        results = [{"role": "tool", "tool_call_id": "1", "content": "ok"}]
        out = harness._inject_budget_warning(results)
        assert "[BUDGET:" in out[0]["content"]
        assert "Start consolidating your work" in out[0]["content"]
        assert "20 iterations left" in out[0]["content"]

    def test_warning_injected_at_90_percent(self) -> None:
        budget = IterationBudget(max_total=100)
        # Consume 95 iterations -> 95% used (warning tier)
        for _ in range(95):
            budget.consume()
        harness = _make_harness(budget=budget)

        results = [{"role": "tool", "tool_call_id": "1", "content": "ok"}]
        out = harness._inject_budget_warning(results)
        assert "[BUDGET WARNING:" in out[0]["content"]
        assert "Provide your final response NOW" in out[0]["content"]

    def test_no_warning_on_empty_results(self) -> None:
        budget = IterationBudget(max_total=10)
        for _ in range(9):
            budget.consume()
        harness = _make_harness(budget=budget)
        out = harness._inject_budget_warning([])
        assert out == []

    def test_warning_appended_to_last_result(self) -> None:
        budget = IterationBudget(max_total=10)
        # Consume 9 iterations -> 90% used (warning tier)
        for _ in range(9):
            budget.consume()
        harness = _make_harness(budget=budget)

        results = [
            {"role": "tool", "tool_call_id": "1", "content": "first"},
            {"role": "tool", "tool_call_id": "2", "content": "second"},
        ]
        out = harness._inject_budget_warning(results)
        # Warning only on the last result
        assert "[BUDGET" not in out[0]["content"]
        assert "[BUDGET WARNING:" in out[1]["content"]


class TestTryActivateFallback:
    """Tests for _try_activate_fallback."""

    def test_no_fallbacks_returns_false(self) -> None:
        harness = _make_harness()
        assert harness._try_activate_fallback() is False

    def test_activates_first_fallback(self) -> None:
        harness = _make_harness()
        harness._fallback_chain = [
            {"provider": "anthropic", "model": "claude-sonnet-4-20250514", "api_key": "sk-fb"},
        ]
        result = harness._try_activate_fallback()
        assert result is True
        assert harness._current_model == "claude-sonnet-4-20250514"
        assert harness._fallback_activated is True

    def test_skips_invalid_fallback(self) -> None:
        harness = _make_harness()
        harness._fallback_chain = [
            {"provider": "", "model": ""},  # invalid
            {"provider": "openai", "model": "gpt-4o-mini"},
        ]
        result = harness._try_activate_fallback()
        assert result is True
        assert harness._current_model == "gpt-4o-mini"

    def test_exhausted_fallbacks_returns_false(self) -> None:
        harness = _make_harness()
        harness._fallback_chain = [
            {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
        ]
        harness._try_activate_fallback()  # consume the only fallback
        assert harness._try_activate_fallback() is False


class TestTryRotateCredential:
    """Tests for _try_rotate_credential."""

    def test_no_pool_returns_false(self) -> None:
        harness = _make_harness()
        assert harness._try_rotate_credential(429, Exception("rate limited")) is False

    def test_rotates_to_next_credential(self) -> None:
        harness = _make_harness()
        harness._credential_pool = CredentialPool([
            PooledCredential(id="a", api_key="sk-a", label="key-a"),
            PooledCredential(id="b", api_key="sk-b", label="key-b"),
        ])
        result = harness._try_rotate_credential(429, Exception("rate limited"))
        assert result is True

    def test_no_more_credentials_returns_false(self) -> None:
        harness = _make_harness()
        harness._credential_pool = CredentialPool([
            PooledCredential(id="a", api_key="sk-a"),
        ])
        result = harness._try_rotate_credential(429, Exception("rate limited"))
        assert result is False


class TestHarnessInitNewFields:
    """Verify that new __init__ fields are properly initialized."""

    def test_credential_pool_default_none(self) -> None:
        harness = _make_harness()
        assert harness._credential_pool is None

    def test_fallback_chain_default_empty(self) -> None:
        harness = _make_harness()
        assert harness._fallback_chain == []

    def test_fallback_index_default_zero(self) -> None:
        harness = _make_harness()
        assert harness._fallback_index == 0

    def test_fallback_activated_default_false(self) -> None:
        harness = _make_harness()
        assert harness._fallback_activated is False

    def test_primary_config_default_none(self) -> None:
        harness = _make_harness()
        assert harness._primary_config is None

    def test_current_model_default_none(self) -> None:
        harness = _make_harness()
        assert harness._current_model is None


# ---------------------------------------------------------------------------
# Pro-tier escalation on empty provider responses
# ---------------------------------------------------------------------------


class TestEmptyResponseProEscalation:
    """Some prompts make the provider return a completion with no content.

    Observed reproducibly on GAIA questions: finish_reason="stop", zero
    content, usage accounted. Nudging and retrying on the same model does
    not help -- it is deterministic for that prompt+model pair. Escalating
    to the pro tier is the one lever left before failing the session.
    """

    def test_escalates_to_the_advisor_client_and_model(self) -> None:
        from surogates.harness.resilience import try_activate_pro_fallback

        advisor = SimpleNamespace(name="advisor-client")
        client, model, used = try_activate_pro_fallback(
            advisor_client=advisor,
            advisor_model="surogate-pro",
            current_model="surogate",
            already_used=False,
        )
        assert client is advisor
        assert model == "surogate-pro"
        assert used is True

    def test_does_not_escalate_twice(self) -> None:
        from surogates.harness.resilience import try_activate_pro_fallback

        client, model, used = try_activate_pro_fallback(
            advisor_client=SimpleNamespace(),
            advisor_model="surogate-pro",
            current_model="surogate-pro",
            already_used=True,
        )
        assert client is None
        assert used is True

    def test_no_escalation_when_advisor_is_unconfigured(self) -> None:
        from surogates.harness.resilience import try_activate_pro_fallback

        client, model, used = try_activate_pro_fallback(
            advisor_client=None,
            advisor_model="",
            current_model="surogate",
            already_used=False,
        )
        assert client is None
        assert used is False

    def test_no_escalation_when_already_running_on_pro(self) -> None:
        """Escalating to the model we are already on would just burn a retry."""
        from surogates.harness.resilience import try_activate_pro_fallback

        advisor = SimpleNamespace()
        client, model, used = try_activate_pro_fallback(
            advisor_client=advisor,
            advisor_model="surogate-pro",
            current_model="surogate-pro",
            already_used=False,
        )
        assert client is None
        assert used is False


class TestPunctuationOnlyFinalResponse:
    """A final response of only punctuation is not an answer.

    Observed on GAIA dev-018: six sessions ended on a 2-token "..." after
    several turns of successful tool use. The content was non-empty, so it
    passed the empty-response ladder untouched and completed the session --
    scoring as a wrong answer rather than surfacing as a failure.
    """

    def test_punctuation_only_is_not_information(self) -> None:
        from surogates.harness.loop import _carries_information

        for text in ("...", "......", "…", "-", "  ", "!?", "***"):
            assert _carries_information(text) is False, text

    def test_real_content_is_information(self) -> None:
        from surogates.harness.loop import _carries_information

        # Non-Latin answers must not be mistaken for punctuation.
        for text in ("42", "Paris", "Αθήνα", "北京", "Москва", "...done"):
            assert _carries_information(text) is True, text


class TestUnfinishedFinalResponse:
    """A turn that states an intention is not a final answer.

    GAIA dev-018: 13 sessions ended with the model announcing its next step
    and calling no tool -- "Let me check the page directly via the
    browser...." -- 11 of them one turn after a browser call, in sessions
    that had been using tools successfully until then. The loop took the
    intention as the answer and completed.
    """

    def test_trailing_intent_looks_unfinished(self) -> None:
        """Both signals, taken from real finals across five GAIA runs.

        The ellipsis-only version of this was fitted to dev-018, where every
        instance happened to end in one. On dev-025 they ended in a full
        stop instead and nothing fired.
        """
        from surogates.harness.loop import _looks_unfinished

        for text in (
            # ellipsis (dev-018)
            "Let me check the page directly via the browser....",
            "Now let me look at the results…",
            "...",
            # plain full stop (dev-025) -- missed by the first version
            "Let me check the browser state after the search and also try "
            "the PubChem classification API with a different endpoint.",
            "Taking a screenshot of the expanded Dastardly Mash panel now.",
            "Let me take a screenshot to see the current state of the video.",
        ):
            assert _looks_unfinished(text) is True, text

    def test_long_answers_are_left_alone(self) -> None:
        """Length is what separates an intention from an answer that happens
        to close on a forward-looking sentence. Without the bound this fired
        on 19% of turns that had answered."""
        from surogates.harness.loop import _looks_unfinished

        long_answer = (
            "The enrollment count is 90, taken from the ClinicalTrials.gov "
            "record rather than the abstract, which quotes the planned "
            "figure of 100. I checked both the results section and the "
            "study protocol to confirm the discrepancy is a recruitment "
            "shortfall. Let me know if you need the protocol revision."
        )
        assert len(long_answer) > 200
        assert _looks_unfinished(long_answer) is False

    def test_concluded_answers_are_left_alone(self) -> None:
        from surogates.harness.loop import _looks_unfinished

        for text in (
            "The answer is 42.",
            "Paris",
            "3, 5, 8",
            "I could not find the paper; the archive link is dead.",
        ):
            assert _looks_unfinished(text) is False, text

    def test_retry_budget_is_one(self) -> None:
        """Bounded to a single nudge: the content may be the real answer, so
        an exhausted retry must accept it, not spend more turns on it."""
        from surogates.harness.loop_constants import _MAX_UNFINISHED_RETRIES

        assert _MAX_UNFINISHED_RETRIES == 1


class TestProviderErrorFinishReason:
    """A failed call must not look like a turn with nothing to say.

    A provider that fails mid-call can return a well-formed choice carrying
    finish_reason="error" and no content instead of raising. Nothing in the
    loop read finish_reason, so the turn fell into the empty-content ladder
    -- which can substitute an EARLIER turn's content as the final answer.
    A failed call could therefore end the session with a confident-looking
    reply the model never made.

    GAIA dev-026, task 624cbf11: a 362,319-token request returned
    error/1-token, billed $0.26, and the session completed with nothing.
    """

    def test_retry_budget_is_small(self) -> None:
        """The usual cause -- an over-sized request -- fails identically
        however many times it is re-sent, so the retry covers a blip rather
        than grinding against a hard limit."""
        from surogates.harness.loop_constants import _MAX_PROVIDER_ERROR_RETRIES

        assert 1 <= _MAX_PROVIDER_ERROR_RETRIES <= 3

    def test_error_finish_reason_is_acted_on(self) -> None:
        """Pins that the loop branches on finish_reason == 'error' at all --
        the original bug was that it was recorded on the event and never
        read."""
        import inspect

        from surogates.harness.loop import AgentHarness

        src = inspect.getsource(AgentHarness)
        assert 'finish_reason == "error"' in src
        assert "provider_error" in src

    def test_a_failed_call_is_not_treated_as_an_answer(self) -> None:
        """The guard keys on the finish reason alone (tool calls aside): a
        stream that died with partial text is retried, not accepted."""
        import inspect

        from surogates.harness.loop import AgentHarness

        src = inspect.getsource(AgentHarness)
        i = src.index('finish_reason == "error"')
        window = src[i : i + 120]
        assert "tool_calls_raw" in window and "content" not in window
