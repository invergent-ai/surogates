"""Discover ALE tasks from the vendored checkout and the data archive.

A task is a directory ``tasks/<domain>/<name>/`` holding
``task_card.json`` (id, prompt, must-do list, input/reference manifests,
software list) and a grader under ``scripts/``. The gated data archive
contributes, per task, an ``input/`` directory to stage and a reference
``output/`` directory the grader compares against.

Upstream runs tasks in cua_bench VM snapshots; here the platform's own
session sandbox is the OS sandbox, so only tasks whose staged inputs
fit the workspace upload caps are eligible -- decided here, before any
session exists, and reported rather than silently dropped.
"""
from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass

from alebench import vendor

# Harness upload caps (see benchmarks/workspace_bench).
MAX_FILE_BYTES = 45_000_000
MAX_TOTAL_BYTES = 400_000_000
MAX_FILES = 500

_GRADER_PREFERENCE = ("score", "verify", "evaluate")


@dataclass(frozen=True)
class Task:
    task_id: str  # "<domain>/<name>"
    domain: str
    title: str
    prompt: str
    must_do: tuple[str, ...]
    software: tuple[str, ...]
    grader_script: str  # absolute path, "" when none found
    input_dir: str  # absolute path, "" when data absent
    reference_dir: str  # absolute path, "" when data absent


def _find_grader(scripts_dir: pathlib.Path) -> str:
    if not scripts_dir.is_dir():
        return ""
    candidates = sorted(scripts_dir.glob("*.py"))
    for prefix in _GRADER_PREFERENCE:
        for path in candidates:
            if path.name.startswith(prefix):
                return str(path)
    return str(candidates[0]) if candidates else ""


def _task_data(task_id: str) -> tuple[str, str]:
    base = vendor.data_dir() / task_id
    input_dir = base / "input"
    reference_dir = base / "output"
    return (
        str(input_dir) if input_dir.is_dir() else "",
        str(reference_dir) if reference_dir.is_dir() else "",
    )


def load_tasks(domains: tuple[str, ...] | None = None) -> list["Task"]:
    root = vendor.home() / "tasks"
    tasks: list[Task] = []
    for card_path in sorted(root.glob("*/*/task_card.json")):
        task_dir = card_path.parent
        domain = task_dir.parent.name
        if domains and domain not in domains:
            continue
        with open(card_path, encoding="utf-8") as fh:
            card = json.load(fh)
        task_id = str(card.get("taskId") or f"{domain}/{task_dir.name}")
        input_dir, reference_dir = _task_data(task_id)
        tasks.append(Task(
            task_id=task_id,
            domain=domain,
            title=str(card.get("title") or ""),
            prompt=str(card.get("taskPrompt") or ""),
            must_do=tuple(map(str, card.get("agentMustDo") or [])),
            software=tuple(map(str, card.get("software") or [])),
            grader_script=_find_grader(task_dir / "scripts"),
            input_dir=input_dir,
            reference_dir=reference_dir,
        ))
    if not tasks:
        raise SystemExit(f"no task_card.json files under {root}")
    return tasks


def eligibility(task: Task) -> str | None:
    """Reason the task cannot run here, or None when it can."""
    if not task.prompt:
        return "task card has no taskPrompt"
    if not task.grader_script:
        return "no grader script in scripts/"
    if not task.input_dir:
        return "input data not present (fetch the gated data archive)"
    if not task.reference_dir:
        return "reference outputs not present in the data archive"

    total = 0
    count = 0
    for dirpath, _dirnames, filenames in os.walk(task.input_dir):
        for fname in filenames:
            size = os.path.getsize(os.path.join(dirpath, fname))
            count += 1
            if size > MAX_FILE_BYTES:
                return f"{fname} is {size / 1e6:.1f} MB, over the upload cap"
            total += size
    if count == 0:
        return "input directory is empty"
    if count > MAX_FILES:
        return f"{count} input files exceeds cap of {MAX_FILES}"
    if total > MAX_TOTAL_BYTES:
        return f"total input size {total / 1e6:.0f} MB exceeds cap"
    return None


def staged_files(task: Task) -> list[tuple[str, str]]:
    """(local_path, workspace_relpath) pairs -- inputs land under input/."""
    plan: list[tuple[str, str]] = []
    base = pathlib.Path(task.input_dir)
    for dirpath, _dirnames, filenames in os.walk(base):
        for fname in sorted(filenames):
            local = pathlib.Path(dirpath) / fname
            rel = local.relative_to(base)
            plan.append((str(local), str(pathlib.PurePosixPath("input") / rel.as_posix())))
    return plan
