"""Drive one GAIA task through one agent session.

A fresh session per task, always. Sharing a session across questions would
let one answer contaminate the next and make the whole run meaningless.

Completion needs two signals. The SSE stream ends on session.done, but the
harness treats "failed" as non-terminal (users retry by sending another
message), so a failed session never closes its stream. We poll status
alongside. The server also closes every stream after 300s regardless of
state, so we reconnect from the last seen event id until one of the real
terminal conditions fires.
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import time

import httpx
from dataclasses import dataclass, field
from typing import Any

from gaia_bench.client import Event
from gaia_bench.dataset import Task, resolve_attachment
from gaia_bench.scorer import extract_final_answer

# Verbatim from the GAIA paper (Figure 2). Appended per task so the agent
# under test keeps its stock system prompt.
GAIA_FORMAT_BLOCK = """

Report your thoughts, and finish your answer with the following template:
FINAL ANSWER: [YOUR FINAL ANSWER].
YOUR FINAL ANSWER should be a number OR as few words as possible OR a comma
separated list of numbers and/or strings.
If you are asked for a number, don't use comma to write your number neither
use units such as $ or percent sign unless specified otherwise.
If you are asked for a string, don't use articles, neither abbreviations
(e.g. for cities), and write the digits in plain text unless specified
otherwise.
If you are asked for a comma separated list, apply the above rules depending
of whether the element to be put in the list is a number or a string.
"""

_TERMINAL_STATUSES = {"completed", "archived", "failed"}


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
    """The question plus GAIA's prescribed answer format."""
    return task.question + GAIA_FORMAT_BLOCK


def _final_answer_from(events: list[Event]) -> str | None:
    for ev in reversed(events):
        if ev.type != "llm.response":
            continue
        content = (ev.data.get("message") or {}).get("content") or ""
        answer = extract_final_answer(content)
        if answer is not None:
            return answer
    return None


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

        attachments = None
        local = resolve_attachment(task)
        if local:
            workspace_path = await client.upload_file(
                session_id, local, task.file_name
            )
            attachments = [{
                "path": workspace_path,
                "filename": task.file_name,
                "mime_type": mimetypes.guess_type(task.file_name)[0],
                "size": os.path.getsize(local),
            }]

        await client.send_message(session_id, build_prompt(task), attachments)

        cursor = 0
        while True:
            try:
                async for ev in client.stream_events(session_id, after=cursor):
                    events.append(ev)
                    cursor = max(cursor, ev.id)
            except httpx.TransportError:
                # A slow model turn can emit nothing for longer than the
                # client's read timeout, killing the stream mid-iteration
                # while the session is still running server-side. Same
                # remedy as the server's own 300s close: reconnect from
                # the cursor. A genuinely dead platform still terminates --
                # the status poll below exhausts its retry window and
                # raises out of the task.
                pass

            # The stream ended: either the session finished, or the server
            # hit its 300s cap. Status tells us which.
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
        answer=_final_answer_from(events),
        events=events,
        wall_clock_s=time.monotonic() - started,
        terminal_status=status,
        error=error,
    )


def _write_trace(out_dir: str, result: RolloutResult) -> None:
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


async def run_split(
    client: Any,
    tasks: list[Task],
    out_dir: str,
    concurrency: int = 4,
    wall_clock_cap_s: float = 1800.0,
) -> list[RolloutResult]:
    """Run tasks concurrently, persisting each trace as it completes.

    Keep concurrency at or below the worker's own limit (10 in
    config.dev.yaml) -- beyond that, tasks queue rather than run and
    per-task wall-clock readings stop meaning anything.
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(task: Task) -> RolloutResult:
        async with sem:
            result = await run_task(
                client, task, wall_clock_cap_s=wall_clock_cap_s,
            )
            _write_trace(out_dir, result)
            return result

    return list(await asyncio.gather(*(one(t) for t in tasks)))
