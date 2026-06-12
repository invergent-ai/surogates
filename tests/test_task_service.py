"""Unit tests for the factored task-spawn service + AgentDef skill preload."""
from __future__ import annotations

from types import SimpleNamespace

from surogates.tasks.spawn import _build_task_worker_config
from surogates.tools.loader import AgentDef


def _agent_def(**kw) -> AgentDef:
    return AgentDef(
        name="arbor-executor", description="d", system_prompt="body",
        source="platform", **kw,
    )


def _task() -> SimpleNamespace:
    return SimpleNamespace(agent_def_name="arbor-executor")


def test_agent_def_preloaded_skills_reach_worker_config():
    cfg = _build_task_worker_config(
        _agent_def(preloaded_skills=["arbor-executor"]), _task(),
    )
    assert "arbor-executor" in cfg["preloaded_skills"]
    assert "subagent-task-worker" in cfg["preloaded_skills"]


def test_no_preloaded_skills_keeps_default_only():
    cfg = _build_task_worker_config(_agent_def(), _task())
    assert cfg["preloaded_skills"] == ["subagent-task-worker"]


def test_preloaded_skills_not_duplicated():
    cfg = _build_task_worker_config(
        _agent_def(preloaded_skills=["arbor-executor", "subagent-task-worker"]),
        _task(),
    )
    assert cfg["preloaded_skills"].count("subagent-task-worker") == 1
    assert cfg["preloaded_skills"].count("arbor-executor") == 1


def test_create_task_and_spawn_is_importable_with_expected_signature():
    # The dispatch tool (Task 6) calls this; lock its keyword contract.
    import inspect

    from surogates.tasks.service import TaskSpawnError, create_task_and_spawn

    params = set(inspect.signature(create_task_and_spawn).parameters)
    assert {
        "goal", "context", "agent_def_name", "max_attempts", "parent_ids",
        "parent_session_id", "org_id", "mission_id",
        "session_store", "session_factory", "redis", "tenant",
    } <= params
    assert issubclass(TaskSpawnError, Exception)
