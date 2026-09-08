"""Drive one ALE task through one agent session.

Workspace-bench shape: stage the task's ``input/`` tree into a fresh
session's workspace (the sandbox mounts it), send the task prompt with
the output conventions, stream to a terminal state with the sibling
benchmarks' reconnect discipline, then download everything the agent
wrote under ``output/``. Grading happens later, offline, against the
reference outputs.
"""
from __future__ import annotations

import asyncio
import json
import os
import posixpath
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from alebench.client import Event
from alebench.dataset import Task, staged_files

_TERMINAL_STATUSES = {"completed", "archived", "failed"}
_MAX_COLLECT_BYTES = 100_000_000
_MAX_COLLECT_FILES = 300

PROMPT_TEMPLATE = """{prompt}

Requirements (each is graded):
{must_do}

Workspace conventions for this task:
- The task's input files are in the `input/` directory of your \
workspace (referred to as `base/input` or `input/` in the brief above).
- Write every required output file into the `output/` directory at the \
workspace root (create it if needed), using exactly the filenames and \
formats the task asks for.
- Software you may need: {software}. Install missing Python packages \
with pip if an import fails.
- Actually produce the files -- a description of what you would do is a \
failed task."""


@dataclass
class RolloutResult:
    task_id: str
    session_id: str
    events: list[Event] = field(default_factory=list)
    wall_clock_s: float = 0.0
    terminal_status: str = ""
    error: str | None = None
    collected: list[dict[str, Any]] = field(default_factory=list)
    collect_notes: list[str] = field(default_factory=list)


def build_prompt(task: Task) -> str:
    must_do = "\n".join(f"- {item}" for item in task.must_do) or "- (see brief)"
    software = ", ".join(task.software) or "standard Python"
    return PROMPT_TEMPLATE.format(
        prompt=task.prompt.strip(), must_do=must_do, software=software
    )


async def _collect_outputs(
    client: Any, session_id: str, task_dir: str, uploaded: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    collected: list[dict[str, Any]] = []
    try:
        tree = await client.get_workspace_tree(session_id)
    except Exception as exc:  # noqa: BLE001 - collection is best-effort
        return collected, [f"workspace tree failed: {exc}"]

    new_files = [f for f in tree
                 if f["path"] not in uploaded
                 and f["path"].startswith("output/")]
    if len(new_files) > _MAX_COLLECT_FILES:
        notes.append(f"collecting first {_MAX_COLLECT_FILES} of "
                     f"{len(new_files)} output files")
        new_files = new_files[:_MAX_COLLECT_FILES]

    out_root = os.path.join(task_dir, "pred")
    for entry in new_files:
        path, size = entry["path"], entry["size"]
        if size > _MAX_COLLECT_BYTES:
            notes.append(f"skipped {path}: over download cap")
            continue
        try:
            blob = await client.download_file(session_id, path)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"download failed for {path}: {exc}")
            continue
        rel = path.removeprefix("output/")
        local = os.path.join(out_root, *rel.split("/"))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as fh:
            fh.write(blob)
        collected.append({"workspace_path": path,
                          "local_relpath": posixpath.join("pred", rel),
                          "size": len(blob)})
    return collected, notes


async def run_task(
    client: Any,
    task: Task,
    task_dir: str,
    wall_clock_cap_s: float = 3600.0,
) -> RolloutResult:
    started = time.monotonic()
    result = RolloutResult(task_id=task.task_id, session_id="")
    status = ""

    try:
        result.session_id = await client.create_session()
        uploaded: set[str] = set()
        for local, rel in staged_files(task):
            subdir, name = posixpath.split(rel)
            key = await client.upload_file(
                result.session_id, local, name, subdir=subdir
            )
            uploaded.add(key)

        await client.send_message(result.session_id, build_prompt(task))

        cursor = 0
        while True:
            try:
                async for ev in client.stream_events(result.session_id, after=cursor):
                    result.events.append(ev)
                    cursor = max(cursor, ev.id)
            except httpx.TransportError:
                pass  # reconnect from cursor; status poll decides

            status = await client.get_session_status(result.session_id)
            if status in _TERMINAL_STATUSES:
                break
            if time.monotonic() - started > wall_clock_cap_s:
                status = "timeout"
                break
            await asyncio.sleep(0)

        result.collected, result.collect_notes = await _collect_outputs(
            client, result.session_id, task_dir, uploaded
        )
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        result.error = f"{type(exc).__name__}: {exc}"
        status = status or "error"

    result.terminal_status = status or "error"
    result.wall_clock_s = time.monotonic() - started
    return result


def write_trace(out_dir: str, result: RolloutResult) -> str:
    task_dir = os.path.join(out_dir, "tasks", result.task_id.replace("/", "__"))
    os.makedirs(task_dir, exist_ok=True)
    with open(os.path.join(task_dir, "events.jsonl"), "w", encoding="utf-8") as fh:
        for ev in result.events:
            fh.write(json.dumps(
                {"id": ev.id, "type": ev.type, "data": ev.data}, default=str
            ) + "\n")
    with open(os.path.join(task_dir, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "task_id": result.task_id,
            "session_id": result.session_id,
            "wall_clock_s": result.wall_clock_s,
            "terminal_status": result.terminal_status,
            "error": result.error,
            "collected": result.collected,
            "collect_notes": result.collect_notes,
        }, fh, indent=2)
    return task_dir


async def run_split(
    client: Any,
    tasks: list[Task],
    out_dir: str,
    concurrency: int = 2,
    wall_clock_cap_s: float = 3600.0,
) -> list[RolloutResult]:
    """Run tasks concurrently, persisting each trace as it completes."""
    sem = asyncio.Semaphore(concurrency)

    async def one(task: Task) -> RolloutResult:
        async with sem:
            task_dir = os.path.join(
                out_dir, "tasks", task.task_id.replace("/", "__")
            )
            os.makedirs(task_dir, exist_ok=True)
            result = await run_task(
                client, task, task_dir, wall_clock_cap_s=wall_clock_cap_s
            )
            write_trace(out_dir, result)
            return result

    return list(await asyncio.gather(*(one(t) for t in tasks)))
