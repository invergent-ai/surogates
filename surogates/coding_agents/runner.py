"""Worker-side orchestration of a single /code run.

Drives the pod-local launcher (``_code`` command) through the sandbox exec
boundary: launch the detached vendor CLI, poll its log in short bursts (each
well under the ~305 s exec ceiling), stream coalesced progress, honour
interrupts, and parse the final stream-json into a :class:`CodeResult`.

The sandbox boundary is a single injected ``execute(name, input_json)``
coroutine so this module is fully unit-testable without a real pod.  No
credential is ever placed in an emitted event — it travels only inside the
launch payload sent over the exec channel.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from surogates.coding_agents.agents import CodeInvocation, CodeResult, parse_stream

# The pod enforces a 1 h ``activeDeadlineSeconds``; cap a run just under it.
_DEFAULT_MAX_WAIT = 3600.0


@dataclass
class RunnerConfig:
    poll_interval: float = 2.0
    max_wait: float = _DEFAULT_MAX_WAIT


def _parse_exec_result(raw: str) -> dict[str, Any]:
    """Parse the sandbox exec result, unwrapping a tool-result envelope.

    ``sandbox_pool.execute`` returns whatever the pod printed on stdout — for
    ``_code`` that is the launcher's JSON.  Some sandbox backends wrap stdout
    in ``{"output": "...", "exit_code": ...}``; tolerate both.
    """
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "error": "unparseable sandbox result"}
    if isinstance(obj, dict) and "ok" not in obj and isinstance(obj.get("output"), str):
        try:
            inner = json.loads(obj["output"])
            if isinstance(inner, dict):
                return inner
        except json.JSONDecodeError:
            pass
    return obj if isinstance(obj, dict) else {"ok": False, "error": "bad result"}


async def run_code_agent(
    *,
    run_id: str,
    agent: str,
    invocation: CodeInvocation,
    env: dict[str, str],
    codex_auth_json: str | None,
    execute: Callable[[str, str], Awaitable[str]],
    emit_progress: Callable[[str], Awaitable[None] | Any],
    should_cancel: Callable[[], bool],
    sleep: Callable[[float], Awaitable[None]],
    now: Callable[[], float] = time.monotonic,
    config: RunnerConfig | None = None,
) -> CodeResult:
    """Launch, poll, stream, and parse a single coding-agent run."""
    cfg = config or RunnerConfig()

    launch_payload = {
        "action": "launch",
        "run_id": run_id,
        "argv": invocation.argv,
        "stdin": invocation.stdin,
        "env": dict(env),
    }
    if codex_auth_json is not None:
        launch_payload["codex_auth_json"] = codex_auth_json

    launched = _parse_exec_result(await execute("_code", json.dumps(launch_payload)))
    if not launched.get("ok"):
        return CodeResult(
            final_message="",
            error=f"Failed to start {agent}: {launched.get('error', 'unknown error')}",
        )

    async def _emit(chunk: str) -> None:
        maybe = emit_progress(chunk)
        if hasattr(maybe, "__await__"):
            await maybe

    async def _cancel() -> None:
        try:
            await execute("_code", json.dumps({"action": "cancel", "run_id": run_id}))
        except Exception:  # cancellation is best-effort
            pass

    transcript: list[str] = []
    offset = 0
    started = now()

    while True:
        if should_cancel():
            await _cancel()
            return CodeResult(
                final_message="",
                error=f"{agent} run interrupted.",
            )

        if now() - started > cfg.max_wait:
            await _cancel()
            return CodeResult(
                final_message="",
                error=f"{agent} run exceeded the maximum runtime (deadline).",
            )

        poll = _parse_exec_result(
            await execute(
                "_code", json.dumps({"action": "poll", "run_id": run_id, "offset": offset}),
            )
        )

        chunk = poll.get("new_output") or ""
        if chunk:
            transcript.append(chunk)
            await _emit(chunk)
        if "offset" in poll and isinstance(poll["offset"], int):
            offset = poll["offset"]

        if poll.get("done"):
            exit_code = poll.get("exit_code")
            break

        await sleep(cfg.poll_interval)

    result = parse_stream(agent, "".join(transcript))
    if result.error and exit_code not in (0, None):
        result.error = f"{agent} exited with code {exit_code}: {result.error}"
    return result
