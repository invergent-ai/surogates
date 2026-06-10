"""Unit tests for tool registration and per-session gating.

Verifies that:
* ``ToolRuntime.register_builtins`` adds the task tools to the registry.
* ``WORKER_EXCLUDED_TOOLS`` covers the three coordinator-side task tools so
  child workers cannot recursively spawn tasks.
* ``_AGENT_TYPE_GATED_TOOLS`` covers ``spawn_task`` so the ``agent_type``
  param is stripped from its schema when the tenant has no AgentDefs.
* ``_filter_effective_tools`` discards ``worker_block`` /
  ``worker_complete`` / ``worker_context`` when the calling Session has
  no ``task_id`` set, leaving them intact when ``task_id`` is set.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock


def test_register_builtins_includes_task_tools():
    """After register_builtins, all six task tools are in the registry."""
    from surogates.tools.registry import ToolRegistry
    from surogates.tools.runtime import ToolRuntime

    reg = ToolRegistry()
    ToolRuntime(reg).register_builtins()

    names = reg.tool_names
    # Coordinator-side
    assert "spawn_task" in names
    assert "unblock_task" in names
    assert "cancel_task" in names
    # Self-tools (gated on session.task_id)
    assert "worker_block" in names
    assert "worker_complete" in names
    assert "worker_context" in names


def test_task_tools_handlers_are_async_and_callable():
    """The registered handlers actually point at the implemented functions."""
    from surogates.tools.registry import ToolRegistry
    from surogates.tools.runtime import ToolRuntime
    from surogates.tasks import tools as task_tools_module

    reg = ToolRegistry()
    ToolRuntime(reg).register_builtins()

    assert reg.get("spawn_task").handler is task_tools_module._spawn_task_handler
    assert reg.get("unblock_task").handler is task_tools_module._unblock_task_handler
    assert reg.get("cancel_task").handler is task_tools_module._cancel_task_handler
    assert reg.get("worker_block").handler is task_tools_module._worker_block_handler
    assert reg.get("worker_complete").handler is task_tools_module._worker_complete_handler
    assert reg.get("worker_context").handler is task_tools_module._worker_context_handler


def test_worker_excluded_tools_contains_task_layer_tools():
    """Children spawned via spawn_worker cannot recursively spawn tasks."""
    from surogates.tools.builtin.coordinator import WORKER_EXCLUDED_TOOLS

    for name in ("spawn_task", "unblock_task", "cancel_task"):
        assert name in WORKER_EXCLUDED_TOOLS, (
            f"{name} must be in WORKER_EXCLUDED_TOOLS so children inherit "
            f"the same recursion-prevention as spawn_worker"
        )
    # worker_block is a self-tool gated separately on session.task_id —
    # not part of this exclusion (children running for a task SHOULD be
    # able to block themselves).
    # The existing coordinator-family entries must still be there.
    assert "spawn_worker" in WORKER_EXCLUDED_TOOLS


def test_agent_type_gated_includes_spawn_task():
    """spawn_task's agent_type param is stripped when no AgentDefs exist."""
    from surogates.harness.tool_schemas import _AGENT_TYPE_GATED_TOOLS

    assert "spawn_task" in _AGENT_TYPE_GATED_TOOLS
    # Existing entries still present.
    assert "delegate_task" in _AGENT_TYPE_GATED_TOOLS
    assert "spawn_worker" in _AGENT_TYPE_GATED_TOOLS


def test_filter_effective_tools_discards_worker_self_tools_when_no_task_id():
    """Plain (non-task) sessions never see worker_block/complete/context."""
    from surogates.orchestrator.worker import _filter_effective_tools

    tenant = MagicMock(user_id=uuid.uuid4())
    session = MagicMock(
        task_id=None,
        service_account_id=None,
        channel="web",
    )

    out = _filter_effective_tools(
        tools={
            "worker_block", "worker_complete", "worker_context",
            "other_tool", "memory",
        },
        tenant=tenant,
        session=session,
        use_api_for_harness_tools=True,
    )
    assert "worker_block" not in out
    assert "worker_complete" not in out
    assert "worker_context" not in out
    assert "other_tool" in out  # unrelated tools pass through


def test_filter_effective_tools_keeps_worker_self_tools_when_session_has_task_id():
    """Sessions executing a task see all three worker self-tools."""
    from surogates.orchestrator.worker import _filter_effective_tools

    tenant = MagicMock(user_id=uuid.uuid4())
    session = MagicMock(
        task_id=uuid.uuid4(),
        service_account_id=None,
        channel="task",
    )

    out = _filter_effective_tools(
        tools={
            "worker_block", "worker_complete", "worker_context",
            "other_tool",
        },
        tenant=tenant,
        session=session,
        use_api_for_harness_tools=True,
    )
    assert "worker_block" in out
    assert "worker_complete" in out
    assert "worker_context" in out
    assert "other_tool" in out


def test_filter_effective_tools_adds_worker_self_tools_under_restrictive_allowlist():
    """A task worker with a restrictive AgentDef allowlist (worker_* not in
    the starting set) still gets its self-tools — they are execution-context
    tools, not work tools subject to the allowlist."""
    from surogates.orchestrator.worker import _filter_effective_tools

    tenant = MagicMock(user_id=uuid.uuid4())
    session = MagicMock(
        task_id=uuid.uuid4(),
        service_account_id=None,
        channel="task",
    )

    # Mirrors the codex-reviewer / claude-coder AgentDef allowlist.
    out = _filter_effective_tools(
        tools={"run_coding_agent", "read_file", "list_files", "search_files"},
        tenant=tenant,
        session=session,
        use_api_for_harness_tools=True,
    )
    assert "worker_complete" in out
    assert "worker_context" in out
    assert "worker_block" in out
    assert "run_coding_agent" in out  # the allowlist tools remain


def test_filter_effective_tools_worker_self_tools_independent_of_other_gates():
    """Anonymous-channel sessions still get all 3 worker self-tools stripped
    when there's no task_id. The other gates (memory/skill_manage
    exclusion for anonymous channels) operate independently."""
    from surogates.orchestrator.worker import _filter_effective_tools

    tenant = MagicMock(user_id=None)
    session = MagicMock(
        task_id=None,
        service_account_id=None,
        channel="api",  # one of ANONYMOUS_CHANNELS
    )

    out = _filter_effective_tools(
        tools={
            "worker_block", "worker_complete", "worker_context",
            "memory", "skill_manage", "regular_tool",
        },
        tenant=tenant,
        session=session,
        use_api_for_harness_tools=True,
    )
    assert "worker_block" not in out
    assert "worker_complete" not in out
    assert "worker_context" not in out
    # The existing exclusions for anonymous-channel sessions still apply.
