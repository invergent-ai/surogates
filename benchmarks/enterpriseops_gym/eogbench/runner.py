"""Drive one EnterpriseOps-Gym task through one agent session.

Sequential by construction (like benchmarks/claweval): the MCP
registration is agent-scoped and the gym proxy injects one database id
at a time. Per task:

1. **Seed.** ``create_database_from_file`` on the local gym server (the
   vendored upstream helper) makes a fresh task database; the proxy
   starts injecting its id.
2. **Expose.** The tunnel (started once per run) fronts the proxy; the
   ops registrar creates the MCP row and attaches it to the agent --
   fresh name per task so no proxy-side cache can leak tools across
   tasks.
3. **Roll out.** One session: the task's policy system prompt plus the
   user prompt in the first message, streamed to a terminal state with
   the sibling benchmarks' reconnect discipline.
4. **Verify.** The vendored ``VerifierEngine`` runs the task's hidden
   SQL against the same database, directly on the local gym URL -- the
   agent's writes and the verifier read one state. Task success = every
   verifier passed (upstream's definition).
5. **Teardown.** Detach + delete the MCP row, delete the database --
   even on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from eogbench import vendor
from eogbench.client import Event
from eogbench.dataset import Task

_TERMINAL_STATUSES = {"completed", "archived", "failed"}

PROMPT_TEMPLATE = """{system_prompt}

---

{user_prompt}"""


@dataclass
class RolloutResult:
    task_id: str
    session_id: str
    database_id: str = ""
    events: list[Event] = field(default_factory=list)
    wall_clock_s: float = 0.0
    terminal_status: str = ""
    error: str | None = None
    verifier_results: list[dict[str, Any]] = field(default_factory=list)
    verify_error: str | None = None

    @property
    def passed(self) -> bool | None:
        if self.verify_error or not self.verifier_results:
            return None
        return all(bool(v.get("passed")) for v in self.verifier_results)


def build_prompt(task: Task) -> str:
    return PROMPT_TEMPLATE.format(
        system_prompt=task.system_prompt.strip(),
        user_prompt=task.user_prompt.strip(),
    )


def seed_database(task: Task) -> str:
    """Create the task's fresh database on the local gym server."""
    vendor.pythonpath()
    from benchmark.mcp_client import create_database_from_file  # type: ignore

    seed = task.seed_database_file
    if seed and not os.path.isabs(seed):
        seed = str(pathlib.Path(os.environ.get("EOG_SEED_ROOT") or vendor.home()) / seed)
    database_id = create_database_from_file(task.gym_url, seed)
    if not database_id:
        raise RuntimeError(
            f"gym at {task.gym_url} refused to create a database "
            f"(seed: {task.seed_database_file or 'server default'})"
        )
    return database_id


def drop_database(task: Task, database_id: str) -> None:
    vendor.pythonpath()
    from benchmark.mcp_client import delete_database  # type: ignore

    if not delete_database(task.gym_url, database_id):
        raise RuntimeError("Gym did not confirm task database deletion")


async def verify(task: Task, database_id: str) -> list[dict[str, Any]]:
    """Run the task's hidden SQL verifiers against the final state."""
    vendor.pythonpath()
    from benchmark.mcp_client import MCPClient  # type: ignore
    from benchmark.models import VerifierConfig  # type: ignore
    from benchmark.verifier import VerifierEngine  # type: ignore

    client = MCPClient(
        base_url=task.gym_url,
        mcp_endpoint=task.mcp_endpoint,
        database_id=database_id,
        context=task.context,
        auth_config=task.auth_config or None,
    )
    # llm_client=None: every public task verifies via pure-SQL
    # database_state; a response_check task would fail loudly here
    # rather than pass silently.
    engine = VerifierEngine(
        mcp_clients={task.gym_name: client}, llm_client=None
    )
    results = []
    for spec in task.verifiers:
        config = VerifierConfig(
            verifier_type=str(spec.get("verifier_type") or ""),
            validation_config=dict(spec.get("validation_config") or {}),
            name=spec.get("name"),
            description=spec.get("description"),
            gym_name=spec.get("gym_name"),
        )
        outcome = await engine.execute_verifier(
            config,
            model_response={},
            database_id=database_id,
            gym_name=config.gym_name or task.gym_name,
        )
        if outcome.get("error") or not isinstance(outcome.get("passed"), bool):
            raise RuntimeError("Verifier infrastructure failed or returned no boolean verdict")
        results.append({
            "name": spec.get("name"),
            "passed": bool(outcome.get("passed")),
            "detail": {k: v for k, v in outcome.items() if k != "passed"},
        })
    return results


async def run_session(
    client: Any,
    task: Task,
    wall_clock_cap_s: float,
) -> tuple[str, list[Event], str, str | None]:
    """One message, streamed to a terminal state. Returns
    (session_id, events, terminal_status, error)."""
    session_id = await client.create_session()
    await client.send_message(session_id, build_prompt(task))

    started = time.monotonic()
    events: list[Event] = []
    cursor = 0
    while True:
        try:
            async for ev in client.stream_events(session_id, after=cursor):
                events.append(ev)
                cursor = max(cursor, ev.id)
        except httpx.TransportError:
            pass  # reconnect from cursor; status poll decides

        status = await client.get_session_status(session_id)
        if status in _TERMINAL_STATUSES:
            return session_id, events, status, None
        if time.monotonic() - started > wall_clock_cap_s:
            return session_id, events, "timeout", None
        await asyncio.sleep(0)


async def run_task(
    client: Any,
    registrar: Any,
    tunnel_url: str,
    proxy: Any,
    task: Task,
    wall_clock_cap_s: float = 1800.0,
) -> RolloutResult:
    started = time.monotonic()
    result = RolloutResult(task_id=task.task_id, session_id="")
    server_id: str | None = None

    try:
        try:
            result.database_id = seed_database(task)
            proxy.configure_task(result.database_id, task.selected_tools, task.restricted_tools,
                                 context=task.context, auth_config=task.auth_config, endpoint=task.mcp_endpoint)
            server_id = registrar.register(
                task.task_id.replace("/", "-"),
                f"{tunnel_url.rstrip('/')}{task.mcp_endpoint}",
            )

            (result.session_id, result.events,
             result.terminal_status, result.error) = await run_session(
                client, task, wall_clock_cap_s
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            result.error = f"{type(exc).__name__}: {exc}"
            result.terminal_status = result.terminal_status or "error"
        finally:
            proxy.set_database_id(None)
            if server_id is not None:
                try:
                    registrar.remove(task.task_id.replace("/", "-"))
                except Exception as exc:  # noqa: BLE001 - cleanup must not mask
                    result.error = result.error or f"cleanup: {exc}"

        # Verify against whatever state exists -- a failed session grades as
        # its verifiers find it, same as upstream's max-steps terminations.
        if result.database_id:
            try:
                result.verifier_results = await verify(task, result.database_id)
            except Exception as exc:  # noqa: BLE001
                result.verify_error = f"{type(exc).__name__}: {exc}"
    finally:
        if result.database_id:
            try:
                drop_database(task, result.database_id)
            except Exception as exc:  # noqa: BLE001 - retain cleanup failure
                result.error = result.error or f"database cleanup: {exc}"

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
            "database_id": result.database_id,
            "wall_clock_s": result.wall_clock_s,
            "terminal_status": result.terminal_status,
            "error": result.error,
            "passed": result.passed,
            "verifier_results": result.verifier_results,
            "verify_error": result.verify_error,
        }, fh, indent=2, default=str)
    return task_dir
