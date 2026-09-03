"""Drive one Workspace-Bench task through one agent session.

A fresh session per task, always: the session's workspace *is* the task's
filesystem, so sharing one would leak files between tasks.

Flow per task: create session -> upload the staged inputs (the sandbox
mounts the same workspace at /workspace, so uploads are simply files the
agent can open) -> send the prompt -> stream events to a terminal state
(same reconnect discipline as benchmarks/gaia: the server closes every
SSE stream after 300s and a failed session never closes its own) ->
diff the workspace tree against what we uploaded and download every new
file. Grading happens later, offline, from those downloads.
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

from wsbench.client import Event
from wsbench.dataset import Task
from wsbench.staging import OUTPUT_DIR, WORKDIR, stage_plan

_TERMINAL_STATUSES = {"completed", "archived", "failed"}

# Files the agent wrote that we refuse to pull back (workspace download
# route also caps at 100 MB server-side).
_MAX_COLLECT_BYTES = 100_000_000
_MAX_COLLECT_FILES = 200

PROMPT_TEMPLATE = """You are working as: {persona}.

{instruction}

Workspace conventions for this task:
- The task's input files are in the `{workdir}/` directory of your \
workspace. Explore it and read whatever you need; some files matter and \
some do not.
- Write every required output file into the `{output_dir}/` directory at \
the root of your workspace (create it if it does not exist), using exactly \
the filenames the task asks for.
- Actually produce the files -- a description of what you would do is a \
failed task."""


@dataclass
class CollectedFile:
    workspace_path: str
    local_relpath: str  # under runs/<id>/tasks/<task>/outputs/
    size: int


@dataclass
class RolloutResult:
    task_id: str
    session_id: str
    events: list[Event] = field(default_factory=list)
    wall_clock_s: float = 0.0
    terminal_status: str = ""
    error: str | None = None
    collected: list[CollectedFile] = field(default_factory=list)
    missing_outputs: list[str] = field(default_factory=list)
    collect_notes: list[str] = field(default_factory=list)


def build_prompt(task: Task) -> str:
    return PROMPT_TEMPLATE.format(
        persona=task.persona or "a knowledge worker",
        instruction=task.instruction.strip(),
        workdir=WORKDIR,
        output_dir=OUTPUT_DIR,
    )


def final_assistant_message(events: list[Event]) -> str:
    for ev in reversed(events):
        if ev.type == "llm.response":
            content = (ev.data.get("message") or {}).get("content") or ""
            if content:
                return content
    return ""


def match_expected_outputs(
    expected: tuple[str, ...], collected_paths: list[str]
) -> list[str]:
    """Expected output names with no collected counterpart.

    Matching is by basename: the prompt asks for outputs/ but an agent
    that wrote `report.md` at the root or inside workdir/ still produced
    the artifact, and the judge -- not path pedantry -- decides whether
    it satisfies the rubric.
    """
    produced = {posixpath.basename(p) for p in collected_paths}
    return [e for e in expected if posixpath.basename(e) not in produced]


async def _collect_outputs(
    client: Any,
    session_id: str,
    task: Task,
    uploaded: set[str],
    task_dir: str,
) -> tuple[list[CollectedFile], list[str], list[str]]:
    """Download every workspace file we did not upload."""
    notes: list[str] = []
    tree = await client.get_workspace_tree(session_id)
    new_files = [f for f in tree if f["path"] not in uploaded]

    if len(new_files) > _MAX_COLLECT_FILES:
        notes.append(
            f"agent produced {len(new_files)} files; collecting first "
            f"{_MAX_COLLECT_FILES}"
        )
        new_files = new_files[:_MAX_COLLECT_FILES]

    out_root = os.path.join(task_dir, "outputs")
    collected: list[CollectedFile] = []
    for entry in new_files:
        path, size = entry["path"], entry["size"]
        if size > _MAX_COLLECT_BYTES:
            notes.append(f"skipped {path}: {size} bytes over download cap")
            continue
        try:
            blob = await client.download_file(session_id, path)
        except Exception as exc:  # noqa: BLE001 - one bad file, not the task
            notes.append(f"download failed for {path}: {exc}")
            continue
        rel = path.replace("\\", "/").lstrip("/")
        local = os.path.join(out_root, *rel.split("/"))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as fh:
            fh.write(blob)
        collected.append(
            CollectedFile(
                workspace_path=path,
                local_relpath=posixpath.join("outputs", rel),
                size=len(blob),
            )
        )

    missing = match_expected_outputs(
        task.output_files, [c.workspace_path for c in collected]
    )
    return collected, missing, notes


async def run_task(
    client: Any,
    task: Task,
    task_dir: str,
    wall_clock_cap_s: float = 1800.0,
) -> RolloutResult:
    started = time.monotonic()
    session_id = ""
    events: list[Event] = []
    status = ""
    error: str | None = None
    collected: list[CollectedFile] = []
    missing: list[str] = list(task.output_files)
    notes: list[str] = []

    try:
        plan = stage_plan(task)  # validated pre-run; raises on infeasible
        session_id = await client.create_session()

        uploaded: set[str] = set()
        for staged in plan:
            key = await client.upload_file(
                session_id, staged.local_path, staged.name, subdir=staged.subdir
            )
            uploaded.add(key)

        await client.send_message(session_id, build_prompt(task))

        cursor = 0
        while True:
            try:
                async for ev in client.stream_events(session_id, after=cursor):
                    events.append(ev)
                    cursor = max(cursor, ev.id)
            except httpx.TransportError:
                # A slow model turn can outlast the read timeout while the
                # session is still running server-side. Same remedy as the
                # server's own 300s close: reconnect from the cursor.
                pass

            status = await client.get_session_status(session_id)
            if status in _TERMINAL_STATUSES:
                break
            if time.monotonic() - started > wall_clock_cap_s:
                status = "timeout"
                break
            await asyncio.sleep(0)

        # Collect whatever exists even after a failed or timed-out
        # session: partial outputs are grading evidence, and rubrics
        # score them fairly (mostly as failures with a visible cause).
        collected, missing, notes = await _collect_outputs(
            client, session_id, task, uploaded, task_dir
        )

    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        error = f"{type(exc).__name__}: {exc}"
        status = status or "error"

    return RolloutResult(
        task_id=task.task_id,
        session_id=session_id,
        events=events,
        wall_clock_s=time.monotonic() - started,
        terminal_status=status,
        error=error,
        collected=collected,
        missing_outputs=missing,
        collect_notes=notes,
    )


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
            "collected": [vars(c) for c in result.collected],
            "missing_outputs": result.missing_outputs,
            "collect_notes": result.collect_notes,
        }, fh, indent=2)
    return task_dir


async def run_split(
    client: Any,
    tasks: list[Task],
    out_dir: str,
    concurrency: int = 3,
    wall_clock_cap_s: float = 1800.0,
) -> list[RolloutResult]:
    """Run tasks concurrently, persisting each trace as it completes.

    Sessions are fully independent (per-session workspaces), so unlike
    benchmarks/claweval nothing here needs to be sequential. Keep
    concurrency at or below the worker's own limit.
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(task: Task) -> RolloutResult:
        async with sem:
            task_dir = os.path.join(out_dir, "tasks", task.task_id)
            os.makedirs(task_dir, exist_ok=True)
            result = await run_task(
                client, task, task_dir, wall_clock_cap_s=wall_clock_cap_s
            )
            write_trace(out_dir, result)
            return result

    return list(await asyncio.gather(*(one(t) for t in tasks)))
