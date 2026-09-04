"""Load a task's vendored grader and produce dimension scores.

Grading is upstream code end to end: the per-task ``grader.py`` from the
pinned claw-eval checkout, fed our bridged trace, with the upstream
``LLMJudge`` for rubric items. The judge speaks OpenAI-compatible chat, so
the platform's model proxy works as its endpoint.
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import pathlib
from typing import Any


def load_grader(task_dir: pathlib.Path) -> Any:
    """Instantiate the AbstractGrader subclass from ``task_dir/grader.py``."""
    from claw_eval.graders.base import AbstractGrader

    grader_path = task_dir / "grader.py"
    if not grader_path.exists():
        raise FileNotFoundError(f"no grader.py in {task_dir}")
    spec = importlib.util.spec_from_file_location(
        f"claweval_grader_{task_dir.name}", grader_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for _name, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, AbstractGrader) and obj is not AbstractGrader:
            return obj()
    raise ValueError(f"no AbstractGrader subclass in {grader_path}")


def build_judge() -> Any | None:
    """Judge from CLAWEVAL_JUDGE_* env; None when unconfigured."""
    base_url = os.environ.get("CLAWEVAL_JUDGE_BASE_URL")
    if not base_url:
        return None
    from claw_eval.graders.llm_judge import LLMJudge

    return LLMJudge(
        model_id=os.environ.get("CLAWEVAL_JUDGE_MODEL", "claude-sonnet-5"),
        api_key=os.environ.get("CLAWEVAL_JUDGE_KEY", ""),
        base_url=base_url,
    )


def grade_task(
    task: Any,
    messages: list[Any],
    dispatches: list[Any],
    audit_data: dict[str, dict],
    judge: Any | None,
) -> dict[str, Any]:
    """Run the task's grader; scores plus any grader crash, never a raise.

    A grader written against sandbox-era expectations may break on a
    bridged trace; that is a finding about coverage, not a run-stopper.
    """
    task_dir = pathlib.Path(task.task_file).parent
    try:
        grader = load_grader(task_dir)
        kwargs: dict[str, Any] = {"audit_data": audit_data, "judge": judge}
        params = inspect.signature(grader.grade).parameters
        if "env_snapshot" in params:
            kwargs["env_snapshot"] = None
        if "media_events" in params:
            kwargs["media_events"] = None
        scores = grader.grade(messages, dispatches, task, **kwargs)
        return {"scores": scores.model_dump(), "grader_error": None}
    except Exception as exc:  # noqa: BLE001 - recorded, never fatal
        return {
            "scores": None,
            "grader_error": f"{type(exc).__name__}: {exc}",
        }
