"""Drive one TheAgentCompany task through one agent session.

The company's service stack (GitLab, ownCloud, Plane, RocketChat) is
hosted by the operator -- upstream's ``servers/`` compose, on a machine
the agent's tools can reach. The runner's job is only the session: send
the task instruction (hostname-substituted), stream to a terminal state
with the sibling benchmarks' reconnect discipline, then download the
agent's workspace files -- the evaluator later remounts them at
``/workspace`` exactly where upstream's checkpoints expect them.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from tacbench.client import Event
from tacbench.dataset import Task, instruction

_TERMINAL_STATUSES = {"completed", "archived", "failed"}
_MAX_COLLECT_BYTES = 100_000_000
_MAX_COLLECT_FILES = 300

PROMPT_SUFFIX = """

Work until every part of the task is done. Files the task asks you to
create or edit belong in your workspace root (it is mounted as
/workspace for grading). Do the work with your tools -- browsing,
terminal, and the company services above -- do not just describe it."""


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


def build_prompt(task: Task, hostname: str) -> str:
    return instruction(task, hostname).strip() + PROMPT_SUFFIX


async def _collect_workspace(
    client: Any, session_id: str, task_dir: str
) -> tuple[list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    collected: list[dict[str, Any]] = []
    try:
        tree = await client.get_workspace_tree(session_id)
    except Exception as exc:  # noqa: BLE001 - collection is best-effort
        return collected, [f"workspace tree failed: {exc}"]

    if len(tree) > _MAX_COLLECT_FILES:
        notes.append(f"collecting first {_MAX_COLLECT_FILES} of {len(tree)} files")
        tree = tree[:_MAX_COLLECT_FILES]

    out_root = os.path.join(task_dir, "workspace")
    for entry in tree:
        path, size = entry["path"], entry["size"]
        if size > _MAX_COLLECT_BYTES:
            notes.append(f"skipped {path}: over download cap")
            continue
        try:
            blob = await client.download_file(session_id, path)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"download failed for {path}: {exc}")
            continue
        local = os.path.join(out_root, *path.replace("\\", "/").split("/"))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as fh:
            fh.write(blob)
        collected.append({"workspace_path": path, "size": len(blob)})
    return collected, notes


async def run_task(
    client: Any,
    task: Task,
    task_dir: str,
    hostname: str,
    wall_clock_cap_s: float = 3600.0,
) -> RolloutResult:
    started = time.monotonic()
    result = RolloutResult(task_id=task.task_id, session_id="")
    status = ""

    try:
        result.session_id = await client.create_session()
        await client.send_message(
            result.session_id, build_prompt(task, hostname)
        )

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

        result.collected, result.collect_notes = await _collect_workspace(
            client, result.session_id, task_dir
        )
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        result.error = f"{type(exc).__name__}: {exc}"
        status = status or "error"

    result.terminal_status = status or "error"
    result.wall_clock_s = time.monotonic() - started
    return result


def write_trace(out_dir: str, result: RolloutResult) -> str:
    task_dir = os.path.join(out_dir, "tasks", result.task_id)
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
    hostname: str,
    concurrency: int = 1,
    wall_clock_cap_s: float = 3600.0,
) -> list[RolloutResult]:
    """Run tasks, persisting each trace as it completes.

    Sequential by default: tasks mutate shared company services
    (RocketChat threads, GitLab repos), and upstream resets the stack
    between tasks -- parallel sessions would contaminate each other.
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(task: Task) -> RolloutResult:
        async with sem:
            task_dir = os.path.join(out_dir, "tasks", task.task_id)
            os.makedirs(task_dir, exist_ok=True)
            result = await run_task(
                client, task, task_dir, hostname,
                wall_clock_cap_s=wall_clock_cap_s,
            )
            write_trace(out_dir, result)
            return result

    return list(await asyncio.gather(*(one(t) for t in tasks)))
