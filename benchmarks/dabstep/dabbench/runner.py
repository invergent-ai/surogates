"""Drive one DABstep task through one agent session.

A fresh session per task, always -- one answer must not contaminate the
next. Per task: create session -> upload the 7 shared context files
under ``data/`` (the sandbox mounts the workspace, so the agent reads
them like local files) -> send the question with its answer-format
guidelines and the FINAL ANSWER template -> stream to a terminal state
with the sibling benchmarks' reconnect discipline -> pull the final
answer out of the last assistant message. Grading happens offline.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from dabbench.client import Event
from dabbench.dataset import Task, context_paths

_TERMINAL_STATUSES = {"completed", "archived", "failed"}

CONTEXT_SUBDIR = "data"

PROMPT_TEMPLATE = """You are a data analyst. Answer the question below \
using the documents in the `data/` directory of your workspace \
(`manual.md` defines every domain concept -- read it before touching \
the data; `payments-readme.md` describes the payments dataset).

QUESTION: {question}

ANSWER GUIDELINES: {guidelines}

Work the data with your tools -- do not answer from intuition. Then \
finish your reply with exactly one line in this format:
FINAL ANSWER: [YOUR FINAL ANSWER]

YOUR FINAL ANSWER must follow the guidelines above. If the question has \
no relevant or applicable answer, reply with FINAL ANSWER: Not Applicable"""

_FINAL_RE = re.compile(r"FINAL ANSWER:\s*(.+?)\s*$",
                       re.IGNORECASE | re.MULTILINE)


@dataclass
class RolloutResult:
    task_id: str
    session_id: str
    answer: str | None
    events: list[Event] = field(default_factory=list)
    wall_clock_s: float = 0.0
    terminal_status: str = ""
    error: str | None = None


def build_prompt(task: Task) -> str:
    return PROMPT_TEMPLATE.format(
        question=task.question.strip(), guidelines=task.guidelines.strip()
    )


def extract_final_answer(text: str) -> str | None:
    matches = _FINAL_RE.findall(text or "")
    if not matches:
        return None
    answer = matches[-1].strip()
    return answer.strip("[]").strip() or None


def final_answer_from(events: list[Event]) -> str | None:
    for ev in reversed(events):
        if ev.type != "llm.response":
            continue
        content = (ev.data.get("message") or {}).get("content") or ""
        answer = extract_final_answer(content)
        if answer is not None:
            return answer
    return None


async def upload_context(client: Any, session_id: str) -> None:
    for path in context_paths():
        await client.upload_file(
            session_id, path, os.path.basename(path), subdir=CONTEXT_SUBDIR
        )


async def run_task(
    client: Any,
    task: Task,
    wall_clock_cap_s: float = 1800.0,
) -> RolloutResult:
    started = time.monotonic()
    session_id = ""
    events: list[Event] = []
    status = ""
    error: str | None = None

    try:
        session_id = await client.create_session()
        await upload_context(client, session_id)
        await client.send_message(session_id, build_prompt(task))

        cursor = 0
        while True:
            try:
                async for ev in client.stream_events(session_id, after=cursor):
                    events.append(ev)
                    cursor = max(cursor, ev.id)
            except httpx.TransportError:
                # Reconnect from the cursor; the status poll below is
                # the real liveness check.
                pass

            status = await client.get_session_status(session_id)
            if status in _TERMINAL_STATUSES:
                break
            if time.monotonic() - started > wall_clock_cap_s:
                status = "timeout"
                break
            await asyncio.sleep(0)

    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        error = f"{type(exc).__name__}: {exc}"
        status = status or "error"

    return RolloutResult(
        task_id=task.task_id,
        session_id=session_id,
        answer=final_answer_from(events),
        events=events,
        wall_clock_s=time.monotonic() - started,
        terminal_status=status,
        error=error,
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
            "answer": result.answer,
            "wall_clock_s": result.wall_clock_s,
            "terminal_status": result.terminal_status,
            "error": result.error,
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

    Sessions are independent; keep concurrency modest -- the
    workspace-bench dev-001 run documented the tier rate-limiting under
    sustained load, and every kill here wastes a full context upload.
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(task: Task) -> RolloutResult:
        async with sem:
            result = await run_task(
                client, task, wall_clock_cap_s=wall_clock_cap_s
            )
            write_trace(out_dir, result)
            return result

    return list(await asyncio.gather(*(one(t) for t in tasks)))
