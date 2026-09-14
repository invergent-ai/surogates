"""Drive one AutomationBench task through one multi-turn session.

A fresh session and a fresh WorldState per task. The orchestrator holds
the world in-process: after each agent turn it either executes the
emitted tool_call blocks against the world (results sent back as a TOOL
RESULTS message) or ends the episode on TASK_COMPLETE / the round cap
(upstream's max-steps analogue). Scoring runs immediately after the
episode -- the world lives in memory -- and the final world is dumped
into the trace so a re-score is possible offline.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from abbench.bridge import Task, dispatch, dump_world, make_world, score, tool_catalog
from abbench.client import Event
from abbench.protocol import (
    build_preamble,
    is_complete,
    parse_tool_calls,
    tool_results_message,
)

_TERMINAL_STATUSES = {"completed", "archived", "failed"}

MAX_AGENT_MESSAGES = 50  # upstream's default max model steps per task


@dataclass
class RolloutResult:
    task_id: str
    session_id: str
    transcript: list[dict[str, str]] = field(default_factory=list)
    tool_calls: int = 0
    agent_messages: int = 0
    completed_marker: bool = False
    events: list[Event] = field(default_factory=list)
    wall_clock_s: float = 0.0
    terminal_status: str = ""
    error: str | None = None
    partial_credit: float | None = None
    strict: float | None = None
    score_error: str | None = None
    final_world: dict[str, Any] = field(default_factory=dict)


async def _await_turn(client, session_id, events, cursor, deadline):
    while True:
        try:
            async for ev in client.stream_events(session_id, after=cursor):
                events.append(ev)
                cursor = max(cursor, ev.id)
        except httpx.TransportError:
            pass  # reconnect from cursor; status poll decides

        status = await client.get_session_status(session_id)
        if status in _TERMINAL_STATUSES:
            return cursor, status
        if time.monotonic() > deadline:
            return cursor, "timeout"
        await asyncio.sleep(0)


def _new_agent_text(events: list[Event], since: int) -> str:
    for ev in reversed(events):
        if ev.id <= since:
            break
        if ev.type == "llm.response":
            content = (ev.data.get("message") or {}).get("content") or ""
            if content:
                return content
    return ""


async def run_task(
    client: Any,
    task: Task,
    wall_clock_cap_s: float = 2400.0,
) -> RolloutResult:
    started = time.monotonic()
    deadline = started + wall_clock_cap_s
    result = RolloutResult(task_id=task.task_id, session_id="")
    cursor = 0
    status = ""

    try:
        world, info = make_world(task)
        result.session_id = await client.create_session()
        result.transcript.append({"role": "user", "content": task.user_prompt})
        await client.send_message(result.session_id, build_preamble(
            task.system_prompt, task.user_prompt, tool_catalog()
        ))

        while result.agent_messages < MAX_AGENT_MESSAGES:
            turn_start = cursor
            cursor, status = await _await_turn(
                client, result.session_id, result.events, cursor, deadline
            )
            if status in ("failed", "timeout", "archived"):
                break

            agent_text = _new_agent_text(result.events, turn_start)
            result.agent_messages += 1
            result.transcript.append({"role": "assistant", "content": agent_text})

            calls, malformed = parse_tool_calls(agent_text)
            if calls or malformed:
                results = []
                for call in calls:
                    outcome = dispatch(world, call["tool_name"], call["tool_args"])
                    results.append({"tool_name": call["tool_name"],
                                    "response": outcome})
                    result.tool_calls += 1
                for err in malformed:
                    results.append({"tool_name": "(malformed)",
                                    "response": {"error": err}})
                message = tool_results_message(results)
                result.transcript.append({"role": "tool", "content": message})
                await client.send_message(result.session_id, message)
                continue

            # No tool calls: the episode is over -- either the agent
            # declared completion or it stopped acting (scored as-is,
            # like upstream's step-cap terminations).
            result.completed_marker = is_complete(agent_text)
            break

        try:
            result.partial_credit, result.strict = score(world, info)
        except Exception as exc:  # noqa: BLE001
            result.score_error = f"{type(exc).__name__}: {exc}"
        result.final_world = dump_world(world)

    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        result.error = f"{type(exc).__name__}: {exc}"
        status = status or "error"

    result.terminal_status = status or "completed"
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
    with open(os.path.join(task_dir, "final_world.json"), "w", encoding="utf-8") as fh:
        json.dump(result.final_world, fh, default=str)
    with open(os.path.join(task_dir, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "task_id": result.task_id,
            "session_id": result.session_id,
            "transcript": result.transcript,
            "tool_calls": result.tool_calls,
            "agent_messages": result.agent_messages,
            "completed_marker": result.completed_marker,
            "wall_clock_s": result.wall_clock_s,
            "terminal_status": result.terminal_status,
            "error": result.error,
            "partial_credit": result.partial_credit,
            "strict": result.strict,
            "score_error": result.score_error,
        }, fh, indent=2, default=str)
    return task_dir


async def run_split(
    client: Any,
    tasks: list[Task],
    out_dir: str,
    concurrency: int = 2,
    wall_clock_cap_s: float = 2400.0,
) -> list[RolloutResult]:
    """Run tasks concurrently -- worlds are per-task and independent."""
    sem = asyncio.Semaphore(concurrency)

    async def one(task: Task) -> RolloutResult:
        async with sem:
            result = await run_task(
                client, task, wall_clock_cap_s=wall_clock_cap_s
            )
            write_trace(out_dir, result)
            return result

    return list(await asyncio.gather(*(one(t) for t in tasks)))
