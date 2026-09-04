"""Discover and filter claw-eval tasks for harness-driven runs.

Phase 1 covers *mock-service tasks*: tasks whose whole tool universe is the
``tools`` + ``tool_endpoints`` declared in ``task.yaml``, backed by local
mock services. Tasks needing the upstream sandbox (file snapshots, sandbox
fixtures), a simulated user (multi-turn), or media attachments are reported
as skipped -- supporting them is future work, not silent coverage.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskSelection:
    eligible: list[Any] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # task_id -> reason


def _skip_reason(task: Any) -> str | None:
    if task.user_agent and getattr(task.user_agent, "enabled", False):
        return "multi-turn (simulated user) not supported yet"
    if task.sandbox_files or task.sandbox_grader_files:
        return "needs upstream sandbox fixtures"
    if task.env_snapshot_files or task.env_snapshot_commands:
        return "needs sandbox environment snapshots"
    if task.prompt.attachments:
        return "needs media attachments"
    if not task.tools or not task.tool_endpoints:
        return "no mock-service tools declared"
    return None


def load_tasks(
    tasks_root: pathlib.Path,
    split: str = "general",
    language: str = "en",
) -> TaskSelection:
    """Load every task under *tasks_root* tagged with *split*, partitioned
    into phase-1 eligible tasks and skipped ones (with reasons)."""
    from claw_eval.models.task import TaskDefinition

    selection = TaskSelection()
    for yaml_path in sorted(tasks_root.glob("*/task.yaml")):
        task = TaskDefinition.from_yaml(yaml_path)
        if split not in (task.tags or []):
            continue
        if language and task.prompt.language != language:
            continue
        reason = _skip_reason(task)
        if reason:
            selection.skipped[task.task_id] = reason
        else:
            selection.eligible.append(task)
    return selection


def select_ids(tasks: list[Any], spec: str | None) -> list[Any]:
    """Filter *tasks* to comma-separated ids or unique prefixes in *spec*.

    An unmatched id raises: silently running fewer tasks than requested
    would look like a passing verification of something never exercised.
    """
    if not spec:
        return tasks
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    picked, unmatched = [], []
    for want in wanted:
        hits = [t for t in tasks if t.task_id.startswith(want)]
        if not hits:
            unmatched.append(want)
        picked.extend(hits)
    if unmatched:
        raise SystemExit(f"no eligible task matches: {', '.join(unmatched)}")
    return picked
