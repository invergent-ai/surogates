"""Drive one AppWorld task through one agent session.

Sequential by construction: one API server holds one task's database at
a time, and the MCP attachment is agent-scoped. Per task:

1. **Bind.** ``AppWorld(task_id, remote_apis_url=...)`` points the API
   server at the task's initial database and exposes the supervisor
   context (the task instruction lives on ``world.task``).
2. **Expose + register.** The tunnel (started once per run) fronts the
   local AppWorld MCP server; the ops registrar creates a per-task MCP
   row (``aw-<task>``) and attaches it to the agent -- fresh name per
   task, so nothing leaks tools across tasks.
3. **Roll out.** One session: the task instruction plus supervisor
   identity, streamed to a terminal state with the sibling benchmarks'
   reconnect discipline.
4. **Settle.** ``world.save()`` then ``world.evaluate()`` -- upstream's
   own stateful test suite over the final databases. Task success is
   upstream's definition: every task test passes.
5. **Teardown.** MCP row detached + deleted, world context closed, even
   on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from awbench.client import Event

_TERMINAL_STATUSES = {"completed", "archived", "failed"}

PROMPT_TEMPLATE = """You are the personal digital assistant of {supervisor_name} \
({supervisor_email}, phone {supervisor_phone}). You act on their behalf in \
their connected apps, which you operate through your available AppWorld \
tools (each app exposes its APIs as tools; use them to look things up and \
to act -- do not guess app state).

Their account passwords are available via the supervisor app's tools \
whenever an app asks you to log in.

TASK: {instruction}

Complete the task fully using the tools. When something the task needs is \
ambiguous, prefer checking the apps over asking questions -- the \
supervisor is away."""


@dataclass
class RolloutResult:
    task_id: str
    session_id: str
    events: list[Event] = field(default_factory=list)
    wall_clock_s: float = 0.0
    terminal_status: str = ""
    error: str | None = None
    passed: bool | None = None
    test_report: dict[str, Any] = field(default_factory=dict)
    evaluate_error: str | None = None


def build_prompt(instruction: str, supervisor: dict[str, Any]) -> str:
    return PROMPT_TEMPLATE.format(
        supervisor_name=(f"{supervisor.get('first_name', '')} "
                         f"{supervisor.get('last_name', '')}").strip() or "the user",
        supervisor_email=supervisor.get("email", "unknown"),
        supervisor_phone=supervisor.get("phone_number", "unknown"),
        instruction=instruction.strip(),
    )


def evaluation_summary(evaluation: Any) -> tuple[bool | None, dict[str, Any]]:
    """Normalize upstream's evaluation object across minor versions.

    The tracker exposes pass/fail test lists; success means no failures.
    Kept defensive: a missing attribute records what WAS there rather
    than crashing the run.
    """
    for shape in ("to_dict", "dict", "as_dict"):
        method = getattr(evaluation, shape, None)
        if callable(method):
            try:
                report = method()
                break
            except Exception:  # noqa: BLE001
                continue
    else:
        report = {"repr": repr(evaluation)[:2000]}

    if isinstance(report, dict):
        fails = report.get("failures", report.get("failed_tests"))
        passes = report.get("passes", report.get("passed_tests"))
        if fails is not None:
            return len(fails) == 0, report
        if isinstance(passes, list) and not passes:
            return False, report
    success = getattr(evaluation, "success", None)
    if isinstance(success, bool):
        return success, report if isinstance(report, dict) else {}
    return None, report if isinstance(report, dict) else {}


async def run_session(
    client: Any, prompt: str, wall_clock_cap_s: float
) -> tuple[str, list[Event], str]:
    session_id = await client.create_session()
    await client.send_message(session_id, prompt)

    started = time.monotonic()
    events: list[Event] = []
    cursor = 0
    while True:
        try:
            async for ev in client.stream_events(session_id, after=cursor):
                events.append(ev)
                cursor = max(cursor, ev.id)
        except httpx.TransportError:
            pass  # reconnect from cursor; the status poll decides

        status = await client.get_session_status(session_id)
        if status in _TERMINAL_STATUSES:
            return session_id, events, status
        if time.monotonic() - started > wall_clock_cap_s:
            return session_id, events, "timeout"
        await asyncio.sleep(0)


async def run_task(
    client: Any,
    registrar: Any,
    mcp_public_base: str,
    task_id: str,
    apis_url: str,
    experiment_name: str,
    wall_clock_cap_s: float = 1800.0,
) -> RolloutResult:
    from appworld import AppWorld

    started = time.monotonic()
    result = RolloutResult(task_id=task_id, session_id="")
    server_id: str | None = None

    try:
        with AppWorld(
            task_id=task_id,
            experiment_name=experiment_name,
            remote_apis_url=apis_url,
        ) as world:
            supervisor = dict(getattr(world.task, "supervisor", {}) or {})
            prompt = build_prompt(world.task.instruction, supervisor)
            try:
                server_id = registrar.register(
                    task_id.replace("_", "-"), f"{mcp_public_base.rstrip('/')}/mcp"
                )
                (result.session_id, result.events,
                 result.terminal_status) = await run_session(
                    client, prompt, wall_clock_cap_s
                )
            finally:
                if server_id is not None:
                    try:
                        registrar.remove(task_id.replace("_", "-"))
                    except Exception as exc:  # noqa: BLE001
                        result.error = result.error or f"cleanup: {exc}"

            world.save()
            try:
                passed, report = evaluation_summary(world.evaluate())
                result.passed, result.test_report = passed, report
            except Exception as exc:  # noqa: BLE001
                result.evaluate_error = f"{type(exc).__name__}: {exc}"

    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        result.error = f"{type(exc).__name__}: {exc}"
        result.terminal_status = result.terminal_status or "error"

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
            "passed": result.passed,
            "test_report": result.test_report,
            "evaluate_error": result.evaluate_error,
        }, fh, indent=2, default=str)
    return task_dir
