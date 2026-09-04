"""Drive one claw-eval task through one agent session.

Sequential by design: each task gets its own mock services, its own MCP
adapter process, and its own MCP server registration, all torn down before
the next task starts. Concurrency would need per-session tool scoping the
platform does not have (MCP rows are org-scoped), and 4-way parallelism is
not worth cross-contaminated tool lists.

Completion mirrors gaia_bench: the SSE stream ends on session.done or the
server's 300s cap, status is polled alongside because "failed" never closes
the stream, and a mid-stream transport timeout reconnects from the cursor
instead of failing the task.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from claweval_bench.client import Event
from claweval_bench.services import collect_audits, start_services
from claweval_bench.tunnel import Exposure

_TERMINAL_STATUSES = {"completed", "archived", "failed"}

ADAPTER_READY_TIMEOUT_S = 15.0


def was_rate_limited(events: list[Event]) -> bool:
    """True when a session died to a provider rate-limit.

    The harness surfaces it as a ``harness.crash`` / ``session.fail``
    carrying "Provider is rate-limited for N more seconds". A rate-limit
    failure is an infra outcome, not a harness-quality one, and it means
    the model tier needs time to recover before the next task starts --
    the runner backs off on it (see ``cli._cmd_run``).
    """
    for ev in events:
        if ev.type in ("harness.crash", "session.fail"):
            if "rate-limited" in json.dumps(ev.data, default=str):
                return True
    return False
PUBLIC_READY_TIMEOUT_S = 30.0

# The attach lands in the agent's runtime config synchronously, but the
# runtime pods refresh their config caches on a published invalidation;
# give the broadcast a moment before the session starts so the first
# tool discovery already sees the task's server.
ATTACH_SETTLE_S = 3.0


@dataclass
class RolloutResult:
    task_id: str
    session_id: str
    events: list[Event] = field(default_factory=list)
    terminal_status: str = ""
    wall_clock_s: float = 0.0
    error: str | None = None
    audit_data: dict[str, dict] = field(default_factory=dict)


def build_prompt(task: Any) -> str:
    """The task instruction, verbatim, plus the mock date when one exists.

    Upstream injects ``mock_today`` into its own system prompt; our agent
    keeps the harness's system prompt (that IS the thing under test), so
    the date the fixtures assume must travel with the user message or
    date-dependent tasks fail for a reason that is neither the harness nor
    the task.
    """
    text = task.prompt.text
    mock_today = getattr(task.environment, "mock_today", None)
    if mock_today:
        text += f"\n\n(For this task, assume today's date is {mock_today}.)"
    return text


async def _wait_healthy(url: str, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=5.0) as http:
        while True:
            try:
                resp = await http.get(url)
                if resp.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"{what} at {url} never became healthy")
            await asyncio.sleep(0.5)


_DNS_FAILURE_RE = re.compile(
    r"nodename|getaddrinfo|name or service|temporary failure in name",
    re.IGNORECASE,
)

# _wait_public outcomes.
PUBLIC_OK = "ok"              # healthy through the tunnel
PUBLIC_DNS_LOCAL = "dns"     # only THIS machine cannot resolve it -> proceed
PUBLIC_DEAD = "dead"         # unreachable -> the tunnel needs re-provisioning


async def _wait_public(url: str, timeout_s: float) -> tuple[str, str]:
    """Health-check the adapter through the tunnel. Returns ``(status, detail)``.

    - ``PUBLIC_OK``: reachable and healthy.
    - ``PUBLIC_DNS_LOCAL``: only THIS machine cannot resolve the hostname
      (a stale NXDOMAIN cache — 30-min TTL on trycloudflare.com). The
      platform resolves it with its own resolvers, so this must not cost a
      task; the caller proceeds.
    - ``PUBLIC_DEAD``: a real broken path (connection refused, timeout, a
      502 from the edge because cloudflared cannot reach the adapter, or
      the tunnel process is gone). The caller re-provisions the tunnel.
    """
    deadline = time.monotonic() + timeout_s
    dns_error: str | None = None
    async with httpx.AsyncClient(timeout=5.0) as http:
        while True:
            try:
                resp = await http.get(url)
                dns_error = None
                if resp.status_code == 200:
                    return PUBLIC_OK, ""
            except httpx.TransportError as exc:
                dns_error = (
                    str(exc) if _DNS_FAILURE_RE.search(str(exc)) else None
                )
            if time.monotonic() > deadline:
                if dns_error:
                    return PUBLIC_DNS_LOCAL, (
                        f"local DNS cannot resolve the tunnel ({dns_error}); "
                        "proceeding -- the platform resolves it independently"
                    )
                return PUBLIC_DEAD, f"adapter tunnel at {url} unreachable"
            await asyncio.sleep(0.5)


def _spawn_adapter(
    task: Any, port: int, dispatch_log: pathlib.Path,
) -> subprocess.Popen:
    # stderr goes to a per-task log, not DEVNULL: a crash on startup is
    # otherwise indistinguishable from a health-check timeout.
    log_path = dispatch_log.parent / "adapter.log"
    with open(log_path, "ab") as log_file:
        return subprocess.Popen(
            [
                sys.executable, "-m", "claweval_bench.mcp_adapter",
                "--task-yaml", str(task.task_file),
                "--port", str(port),
                "--dispatch-log", str(dispatch_log),
            ],
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            env=dict(os.environ),
        )


async def _drive_session(
    client: Any, task: Any, wall_clock_cap_s: float,
) -> RolloutResult:
    started = time.monotonic()
    session_id = ""
    events: list[Event] = []
    status = ""
    error: str | None = None

    try:
        session_id = await client.create_session()
        await client.send_message(session_id, build_prompt(task))

        cursor = 0
        while True:
            try:
                async for ev in client.stream_events(session_id, after=cursor):
                    events.append(ev)
                    cursor = max(cursor, ev.id)
            except httpx.TransportError:
                # Slow model turns outlive the read timeout mid-iteration
                # while the session is alive server-side; reconnect from
                # the cursor. A dead platform still fails the task: the
                # status poll exhausts its retry window and raises.
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
        events=events,
        terminal_status=status,
        wall_clock_s=time.monotonic() - started,
        error=error,
    )


async def run_task(
    client: Any,
    task: Any,
    *,
    vendor_root: pathlib.Path,
    registrar: Any,
    out_dir: pathlib.Path,
    exposure: Exposure | None = None,
    adapter_port: int = 8321,
    wall_clock_cap_s: float = 900.0,
) -> RolloutResult:
    """Services up -> adapter up -> MCP registered -> session -> teardown."""
    task_dir = out_dir / "tasks" / task.task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    dispatch_log = task_dir / "dispatches.jsonl"
    exposure = exposure or Exposure(public_base=None)

    adapter: subprocess.Popen | None = None
    try:
        with start_services(task, vendor_root):
            adapter = _spawn_adapter(task, adapter_port, dispatch_log)
            await _wait_healthy(
                f"http://127.0.0.1:{adapter_port}/healthz",
                ADAPTER_READY_TIMEOUT_S, "MCP adapter",
            )
            if exposure.public_base:
                # The harness fetches tools through the tunnel; prove that
                # path end to end before spending a session on it, and heal
                # a dropped quick tunnel so a multi-hour run survives it.
                # A restart mints a NEW hostname, which is why the
                # registration below reads exposure.mcp_url AFTER healing.
                if not exposure.is_alive():
                    print("    tunnel process gone -- re-provisioning",
                          flush=True)
                    exposure.restart(adapter_port)
                status, detail = await _wait_public(
                    exposure.health_url(adapter_port), PUBLIC_READY_TIMEOUT_S,
                )
                if status == PUBLIC_DEAD:
                    print(f"    tunnel unreachable ({detail}) -- "
                          "re-provisioning", flush=True)
                    exposure.restart(adapter_port)
                    status, detail = await _wait_public(
                        exposure.health_url(adapter_port),
                        PUBLIC_READY_TIMEOUT_S,
                    )
                    if status == PUBLIC_DEAD:
                        raise RuntimeError(
                            f"tunnel unreachable after re-provision: {detail}"
                        )
                if status == PUBLIC_DNS_LOCAL:
                    print(f"    note: {detail}", flush=True)
            registrar.register(task.task_id, exposure.mcp_url(adapter_port))
            try:
                await asyncio.sleep(ATTACH_SETTLE_S)
                result = await _drive_session(client, task, wall_clock_cap_s)
                # Audits must be read while the services are still up.
                result.audit_data = collect_audits(task)
            finally:
                registrar.remove(task.task_id)
    except Exception as exc:  # noqa: BLE001 - env failure is a task outcome
        result = RolloutResult(
            task_id=task.task_id, session_id="",
            terminal_status="env_error",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if adapter is not None:
            adapter.terminate()
            try:
                adapter.wait(timeout=5)
            except subprocess.TimeoutExpired:
                adapter.kill()

    _persist(task_dir, result)
    return result


def _persist(task_dir: pathlib.Path, result: RolloutResult) -> None:
    with open(task_dir / "events.jsonl", "w", encoding="utf-8") as fh:
        for ev in result.events:
            fh.write(json.dumps(
                {"id": ev.id, "type": ev.type, "data": ev.data},
                ensure_ascii=False,
            ) + "\n")
    with open(task_dir / "meta.json", "w", encoding="utf-8") as fh:
        json.dump({
            "task_id": result.task_id,
            "session_id": result.session_id,
            "terminal_status": result.terminal_status,
            "wall_clock_s": result.wall_clock_s,
            "error": result.error,
        }, fh, indent=2)
    with open(task_dir / "audit.json", "w", encoding="utf-8") as fh:
        json.dump(result.audit_data, fh, ensure_ascii=False, indent=2)
