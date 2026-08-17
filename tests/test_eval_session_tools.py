"""An evaluation session never sees a tool that waits on a human.

Two surfaces have to agree.  ``worker._filter_effective_tools`` builds the
PROMPT surface — the list of tool names the system prompt describes in prose.
``AgentHarness._tool_filter_for_session`` builds the SCHEMA surface — the
tools actually handed to the model, which is the only one the model can call.
They disagreed: the prompt dropped ``ask_user_question`` for an evaluation
session while the schema kept it, because
``_ensure_always_available_tools`` force-adds it back onto every filter
without an explicit ``allowed_tools``.  So an evaluation row could still park
its turn on a question nobody would ever answer.

Every schema-surface test here goes through ``get_schemas`` rather than the
filter set, so a regression that leaves the name in the schema list cannot
pass by satisfying the filter alone.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.prompt import PromptBuilder
from surogates.orchestrator.worker import (
    _filter_effective_tools,
    is_eval_session,
)
from surogates.runtime import SLASH_COMMAND_IDS, SlashCommandConfig
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.builtin.ask_user_question import (
    register as register_ask_user_question,
)
from surogates.tools.builtin.memory import register as register_memory
from surogates.tools.registry import ToolRegistry

_TOOLS = {"ask_user_question", "memory", "web_search", "skill_manage"}

_EVAL_CONFIG = {"memory_boundary": "eval:run-1-a1b2"}


def _tenant():
    return SimpleNamespace(user_id=None, service_account_id="sa-1")


def _session(config):
    return SimpleNamespace(channel="api", config=config)


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


def test_eval_session_is_detected_by_stamped_boundary():
    assert is_eval_session(_session({"memory_boundary": "eval:run-1"})) is True


def test_ordinary_api_session_is_not_an_eval_session():
    assert is_eval_session(_session({})) is False


def test_blank_boundary_is_not_an_eval_session():
    assert is_eval_session(_session({"memory_boundary": "  "})) is False


def test_eval_partition_id_without_stamped_boundary_is_not_an_eval_session():
    # A web client can supply eval_partition_id in config, but without the
    # server-stamped boundary, it's not an isolated session and must keep
    # ask_user_question.
    assert is_eval_session(_session({"eval_partition_id": "run-1-a1b2"})) is False


def test_non_eval_memory_boundary_is_not_an_eval_session():
    # A session with a server-stamped non-eval boundary (e.g., from a Slack channel)
    # is not an evaluation session.
    assert is_eval_session(_session({"memory_boundary": "slack:c:C123"})) is False


# ---------------------------------------------------------------------------
# Prompt surface (worker._filter_effective_tools)
# ---------------------------------------------------------------------------


def test_eval_session_loses_ask_user_question_from_the_prompt():
    result = _filter_effective_tools(
        tools=_TOOLS,
        tenant=_tenant(),
        session=_session(dict(_EVAL_CONFIG)),
        use_api_for_harness_tools=True,
    )
    assert "ask_user_question" not in result


def test_eval_session_keeps_every_other_tool():
    # The point is evaluating the real agent, so only the tool that cannot
    # possibly be answered is removed.
    result = _filter_effective_tools(
        tools=_TOOLS,
        tenant=_tenant(),
        session=_session(dict(_EVAL_CONFIG)),
        use_api_for_harness_tools=True,
    )
    assert {"memory", "web_search", "skill_manage"} <= result


def test_ordinary_api_session_keeps_ask_user_question():
    result = _filter_effective_tools(
        tools=_TOOLS,
        tenant=_tenant(),
        session=_session({}),
        use_api_for_harness_tools=True,
    )
    assert "ask_user_question" in result


def test_session_with_eval_partition_id_but_no_boundary_keeps_ask_user_question():
    # Regression test: a client-supplied eval_partition_id without the
    # server-stamped boundary must NOT strip ask_user_question.
    result = _filter_effective_tools(
        tools=_TOOLS,
        tenant=_tenant(),
        session=_session({"eval_partition_id": "run-1-a1b2"}),
        use_api_for_harness_tools=True,
    )
    assert "ask_user_question" in result


# ---------------------------------------------------------------------------
# Schema surface (AgentHarness._tool_filter_for_session) — what the model sees
# ---------------------------------------------------------------------------


def _harness() -> AgentHarness:
    registry = ToolRegistry()
    register_ask_user_question(registry)
    register_memory(registry)
    return AgentHarness(
        session_store=AsyncMock(),
        tool_registry=registry,
        llm_client=AsyncMock(),
        tenant=TenantContext(
            org_id=UUID("00000000-0000-0000-0000-000000000001"),
            user_id=None,
            org_config={}, user_preferences={}, permissions=frozenset(),
            asset_root="/tmp/test",
            service_account_id=UUID("00000000-0000-0000-0000-000000000003"),
        ),
        worker_id="test-worker",
        budget=IterationBudget(max_total=10),
        context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=MagicMock(spec=PromptBuilder),
        slash_commands=SlashCommandConfig(commands=frozenset(SLASH_COMMAND_IDS)),
    )


def _real_session(config: dict | None = None) -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(), user_id=None, org_id=uuid4(), agent_id="a1",
        channel="api", status="active", config=config or {},
        created_at=now, updated_at=now,
    )


def _model_visible_tools(harness: AgentHarness, session: Session) -> set[str]:
    """The tool names actually shipped to the LLM for *session*."""
    tool_filter = harness._tool_filter_for_session(session)
    return {
        schema["function"]["name"]
        for schema in harness._tools.get_schemas(names=tool_filter)
    }


def test_model_is_not_handed_ask_user_question_in_an_eval_session():
    # The regression that survived review: the prose said the tool was gone
    # while the schema was still shipped, so the model could call it and park
    # the row for the tool's whole 30-minute deadline.
    harness = _harness()
    visible = _model_visible_tools(harness, _real_session(dict(_EVAL_CONFIG)))
    assert "ask_user_question" not in visible
    assert "memory" in visible


def test_model_keeps_ask_user_question_in_an_ordinary_api_session():
    harness = _harness()
    assert "ask_user_question" in _model_visible_tools(harness, _real_session())


def test_forged_eval_partition_id_does_not_strip_the_schema():
    harness = _harness()
    visible = _model_visible_tools(
        harness, _real_session({"eval_partition_id": "run-1-a1b2"}),
    )
    assert "ask_user_question" in visible


def test_eval_row_running_as_a_scheduled_child_also_loses_the_schema():
    # ``_tool_filter_for_session`` returns from two places; the scheduled-child
    # branch returns early, so the rule has to be applied on both paths.
    harness = _harness()
    visible = _model_visible_tools(
        harness,
        _real_session({**_EVAL_CONFIG, "scheduled_session_id": str(uuid4())}),
    )
    assert "ask_user_question" not in visible


def test_an_explicit_allowlist_naming_it_still_loses_it_in_an_eval_session():
    # ``allowed_tools`` is an admin contract that _ensure_always_available_tools
    # respects; the evaluation rule beats it, because the tool is unanswerable
    # in an evaluation whatever the config asks for.
    harness = _harness()
    visible = _model_visible_tools(
        harness,
        _real_session({
            **_EVAL_CONFIG,
            "allowed_tools": ["ask_user_question", "memory"],
        }),
    )
    assert "ask_user_question" not in visible
    assert "memory" in visible


@pytest.mark.parametrize("config", [
    {},
    dict(_EVAL_CONFIG),
    {"eval_partition_id": "run-1-a1b2"},
    {"memory_boundary": "slack:c:C123"},
])
def test_prompt_and_schema_agree_on_ask_user_question(config):
    """The invariant. Either surface alone is not evidence of the other."""
    harness = _harness()
    schema = _model_visible_tools(harness, _real_session(config))
    prompt = _filter_effective_tools(
        tools=set(harness._tools.tool_names),
        tenant=_tenant(),
        session=_session(config),
        use_api_for_harness_tools=True,
    )
    assert ("ask_user_question" in schema) == ("ask_user_question" in prompt)
