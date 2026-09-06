"""Drive one Agent Leaderboard scenario through one multi-turn session.

A fresh session per scenario, always. The orchestrator plays upstream's
simulation pipeline locally: it sends the preamble plus the persona's
first message, waits for the agent's turn to finish (same stream/status
discipline as the sibling benchmarks -- every send_message starts a turn
whose stream closes on session.done, with a status poll as the ground
truth), then either answers tool calls with simulated results or lets
the user simulator take the next turn. Upstream's caps apply: 5 user
turns, plus a hard ceiling on agent messages so a tool-loop cannot spin
forever.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from galbench.client import Event
from galbench.dataset import Scenario
from galbench.protocol import (
    build_preamble,
    is_complete,
    parse_tool_calls,
    tool_results_message,
)
from galbench.sim import ChatFn, simulate_tool, simulate_user

_TERMINAL_STATUSES = {"completed", "archived", "failed"}

MAX_USER_TURNS = 5  # upstream's MAX_TURNS
MAX_AGENT_MESSAGES = 16  # tool rounds + user turns, loop guard


@dataclass
class ToolCallRecord:
    tool_name: str
    tool_args: dict[str, Any]
    result: dict[str, Any]
    known_tool: bool


@dataclass
class RolloutResult:
    scenario_id: str
    session_id: str
    transcript: list[dict[str, str]] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    user_turns: int = 0
    agent_messages: int = 0
    completed_marker: bool = False
    events: list[Event] = field(default_factory=list)
    wall_clock_s: float = 0.0
    terminal_status: str = ""
    error: str | None = None


async def _await_turn(
    client: Any,
    session_id: str,
    events: list[Event],
    cursor: int,
    deadline: float,
) -> tuple[int, str]:
    """Stream until the turn's stream ends and status is terminal."""
    while True:
        try:
            async for ev in client.stream_events(session_id, after=cursor):
                events.append(ev)
                cursor = max(cursor, ev.id)
        except httpx.TransportError:
            pass  # reconnect from cursor; status poll below decides

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


async def run_scenario(
    client: Any,
    chat: ChatFn,
    scenario: Scenario,
    wall_clock_cap_s: float = 2400.0,
) -> RolloutResult:
    started = time.monotonic()
    deadline = started + wall_clock_cap_s
    result = RolloutResult(scenario_id=scenario.scenario_id, session_id="")
    tools_by_name = {t["title"]: t for t in scenario.tools}
    tool_outputs: list[dict[str, Any]] = []
    cursor = 0
    status = ""

    try:
        result.session_id = await client.create_session()
        result.transcript.append(
            {"role": "user", "content": scenario.first_message.strip()}
        )
        await client.send_message(
            result.session_id, build_preamble(scenario)
        )

        while result.agent_messages < MAX_AGENT_MESSAGES:
            turn_start = cursor
            cursor, status = await _await_turn(
                client, result.session_id, result.events, cursor, deadline
            )
            if status in ("failed", "timeout", "archived"):
                break

            agent_text = _new_agent_text(result.events, turn_start)
            result.agent_messages += 1
            result.transcript.append(
                {"role": "assistant", "content": agent_text}
            )

            calls, malformed = parse_tool_calls(agent_text)
            if calls or malformed:
                results: list[dict[str, Any]] = []
                for call in calls:
                    tool = tools_by_name.get(call["tool_name"])
                    if tool is None:
                        outcome = {"error": f"Unknown tool: {call['tool_name']}"}
                    else:
                        outcome = await simulate_tool(
                            chat, tool, call["tool_args"],
                            result.transcript, agent_text,
                        )
                    results.append(
                        {"tool_name": call["tool_name"], "response": outcome}
                    )
                    tool_outputs.append(results[-1])
                    result.tool_calls.append(ToolCallRecord(
                        tool_name=call["tool_name"],
                        tool_args=call["tool_args"],
                        result=outcome,
                        known_tool=tool is not None,
                    ))
                for err in malformed:
                    results.append({"tool_name": "(malformed)", "response": {"error": err}})
                message = tool_results_message(results)
                result.transcript.append({"role": "tool", "content": message})
                await client.send_message(result.session_id, message)
                continue

            if is_complete(agent_text) or result.user_turns >= MAX_USER_TURNS:
                result.completed_marker = is_complete(agent_text)
                break

            user_text = await simulate_user(
                chat, scenario.persona, scenario.user_goals,
                result.transcript, tool_outputs,
            )
            result.user_turns += 1
            result.transcript.append({"role": "user", "content": user_text})
            await client.send_message(result.session_id, user_text)

    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        result.error = f"{type(exc).__name__}: {exc}"
        status = status or "error"

    result.terminal_status = status or "completed"
    result.wall_clock_s = time.monotonic() - started
    return result


def write_trace(out_dir: str, result: RolloutResult) -> str:
    task_dir = os.path.join(out_dir, "tasks", result.scenario_id)
    os.makedirs(task_dir, exist_ok=True)
    with open(os.path.join(task_dir, "events.jsonl"), "w", encoding="utf-8") as fh:
        for ev in result.events:
            fh.write(json.dumps(
                {"id": ev.id, "type": ev.type, "data": ev.data}, default=str
            ) + "\n")
    with open(os.path.join(task_dir, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "scenario_id": result.scenario_id,
            "session_id": result.session_id,
            "transcript": result.transcript,
            "tool_calls": [
                {
                    "tool_name": c.tool_name,
                    "tool_args": c.tool_args,
                    "result": c.result,
                    "known_tool": c.known_tool,
                }
                for c in result.tool_calls
            ],
            "user_turns": result.user_turns,
            "agent_messages": result.agent_messages,
            "completed_marker": result.completed_marker,
            "wall_clock_s": result.wall_clock_s,
            "terminal_status": result.terminal_status,
            "error": result.error,
        }, fh, indent=2, default=str)
    return task_dir


async def run_split(
    client: Any,
    chat: ChatFn,
    scenarios: list[Scenario],
    out_dir: str,
    concurrency: int = 2,
    wall_clock_cap_s: float = 2400.0,
) -> list[RolloutResult]:
    """Run scenarios concurrently, persisting each trace as it completes.

    Default concurrency is low: every scenario is a long multi-turn
    session plus a stream of simulator calls, and the tier's rate-limit
    behaviour under sustained load is a documented failure mode.
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(scenario: Scenario) -> RolloutResult:
        async with sem:
            result = await run_scenario(
                client, chat, scenario, wall_clock_cap_s=wall_clock_cap_s
            )
            write_trace(out_dir, result)
            return result

    return list(await asyncio.gather(*(one(s) for s in scenarios)))
