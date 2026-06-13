"""Persistent tool-executor daemon — runs inside the sandbox pod.

Replaces the per-call K8s-exec'd ``tool-executor`` script.  Boot order:
load the tool registry once (the ~7.5 CPU-second import cost is paid
during pod startup, before the port binds), then serve HTTP:

    POST /execute   {"name": ..., "args": {...}, "timeout": 300}
    GET  /healthz   -> 200 when $WORKSPACE_DIR has a live FUSE mount

Each ``/execute`` forks a child process (``multiprocessing`` fork
context — the warm registry is inherited copy-on-write) that runs the
dispatch on a fresh event loop and writes the result JSON to a pipe.
Tool handlers were written for process-per-call execution: they block,
burn CPU, and can crash — none of which may touch the serving loop, or
the readinessProbe would flap, ``_map_pod_status`` would report
``PENDING``, and ``SandboxPool.ensure`` would destroy the sandbox
mid-tool.  On timeout the child is killed (the old exec path abandoned
the remote process and left it running).

The worker authenticates with ``Authorization: Bearer
$TOOL_EXECUTOR_TOKEN``; ``/healthz`` is unauthenticated (the kubelet
probes it).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import multiprocessing
import os
import sys
from typing import Any

from fastapi import FastAPI, Request, Response

logger = logging.getLogger("tool-executor")

DEFAULT_PORT = 8071
DEFAULT_TIMEOUT = 300
MAX_CONCURRENCY = 8

# Fork context: children inherit the warm registry copy-on-write.  The
# spawn context would re-import everything and defeat the daemon.
_MP = multiprocessing.get_context("fork")

# Populated once by init_registry() before the port binds; forked
# children read it via module global.
_REGISTRY: Any = None


def init_registry() -> Any:
    """Import and build the tool registry (the expensive part, run once)."""
    global _REGISTRY
    from surogates.tools.registry import ToolRegistry
    from surogates.tools.runtime import ToolRuntime

    registry = ToolRegistry()
    runtime = ToolRuntime(registry)
    runtime.register_builtins()
    _REGISTRY = registry
    return registry


def workspace_mounted(workspace: str, mounts_path: str = "/proc/mounts") -> bool:
    """Return ``True`` when *workspace* has a live FUSE mount.

    ``os.path.ismount`` is not usable here: the emptyDir volumeMount
    already makes the workspace a mount point before the geesefs
    sidecar's FUSE mount propagates in, so the fstype must be checked.
    The ``.s3fs-mounted`` sentinel is fleet-mode-only and never written
    by the legacy entrypoint sandbox pods run.
    """
    target = workspace.rstrip("/") or "/"
    try:
        with open(mounts_path, encoding="utf-8") as fh:
            for line in fh:
                fields = line.split()
                if (
                    len(fields) >= 3
                    and fields[1] == target
                    and fields[2].startswith("fuse")
                ):
                    return True
    except OSError:
        return False
    return False


def _run_checkpoint(args: dict, workspace: str) -> str:
    """Handle ``_checkpoint`` internal commands (ported from the old CLI)."""
    from surogates.tools.utils.checkpoint_manager import CheckpointManager

    action = args.get("action", "take")
    mgr = CheckpointManager(enabled=True)
    logger.info("checkpoint action=%s", action)

    if action == "new_turn":
        mgr.new_turn()
        return json.dumps({"success": True, "action": "new_turn"})
    if action == "take":
        reason = args.get("reason", "auto")
        file_path = args.get("file_path")
        workdir = workspace
        if file_path:
            workdir = mgr.get_working_dir_for_path(file_path)
        ok = mgr.ensure_checkpoint(workdir, reason)
        result: dict = {"success": ok, "action": "take"}
        if ok:
            h = mgr.latest_hash(workdir)
            if h:
                result["hash"] = h
        logger.info("checkpoint take: %s", "ok" if ok else "skipped")
        return json.dumps(result)
    if action == "latest_hash":
        return json.dumps({"success": True, "hash": mgr.latest_hash(workspace)})
    if action == "list":
        return json.dumps({
            "success": True,
            "checkpoints": mgr.list_checkpoints(workspace),
        })
    if action == "restore":
        return json.dumps(
            mgr.restore(workspace, args.get("hash", ""), args.get("file_path")),
        )
    return json.dumps({
        "success": False,
        "error": f"Unknown checkpoint action: {action}",
    })


def run_tool(name: str, args: dict, workspace: str) -> str:
    """Dispatch one tool call through the real handlers.

    Runs inside the forked child — blocking calls and CPU burn are fine
    here.  Result shapes mirror the old CLI exactly.
    """
    if name == "_checkpoint":
        return _run_checkpoint(args, workspace)
    if name == "_code":
        # The payload may carry a credential on launch; never log args.
        from surogates.coding_agents.pod_runner import dispatch as code_dispatch

        return json.dumps(code_dispatch(args))

    async def _dispatch() -> str:
        return await _REGISTRY.dispatch(
            name, args, workspace_path=workspace, tools=_REGISTRY,
        )

    try:
        return asyncio.run(_dispatch())
    except KeyError:
        return json.dumps({
            "exit_code": 1,
            "output": "",
            "error": f"Unknown tool: {name}",
        })
    except Exception as exc:
        logger.error("Tool %s raised: %s", name, exc, exc_info=True)
        return json.dumps({
            "exit_code": 1,
            "output": "",
            "error": str(exc),
        })


def _timed_out_result() -> str:
    """Transport-level timeout result — mirrors K8sSandbox._result_json."""
    return json.dumps({
        "exit_code": -1,
        "stdout": "",
        "stderr": "Execution timed out",
        "truncated": False,
        "timed_out": True,
    })


def _child_main(conn: Any, name: str, args: dict, workspace: str) -> None:
    """Entry point of the forked child: run the tool, ship the result."""
    # The forked thread inherits the parent's "running event loop"
    # marker; clear it so run_tool's asyncio.run() can start a fresh
    # loop.  Python 3.12's asyncio also resets this in an at-fork hook —
    # this line is belt-and-braces against that hook changing.
    asyncio._set_running_loop(None)
    try:
        result = run_tool(name, args, workspace)
    except BaseException as exc:  # never die without reporting
        result = json.dumps({"exit_code": 1, "output": "", "error": str(exc)})
    try:
        conn.send_bytes(result.encode("utf-8"))
    finally:
        conn.close()


async def execute_in_child(
    name: str, args: dict, workspace: str, timeout: float,
) -> str:
    """Fork a child, run *name* in it, and return its result JSON.

    On timeout the child is SIGKILLed and the standard ``timed_out``
    result is returned — unlike the old exec transport, no orphaned
    process keeps running.  A child that dies without reporting
    (segfault, ``os._exit``) yields an error result and the daemon
    keeps serving.
    """
    parent_conn, child_conn = _MP.Pipe(duplex=False)
    proc = _MP.Process(
        target=_child_main, args=(child_conn, name, args, workspace), daemon=True,
    )
    proc.start()
    child_conn.close()
    try:
        ready = await asyncio.to_thread(parent_conn.poll, timeout)
        if not ready:
            logger.warning(
                "Tool %s timed out after %.0fs; killing child %s",
                name, timeout, proc.pid,
            )
            proc.kill()
            return _timed_out_result()
        try:
            data = await asyncio.to_thread(parent_conn.recv_bytes)
        except EOFError:
            await asyncio.to_thread(proc.join, 5)
            logger.error(
                "Tool %s child died without a result (exit code %s)",
                name, proc.exitcode,
            )
            return json.dumps({
                "exit_code": 1,
                "output": "",
                "error": f"Tool process died unexpectedly (exit code {proc.exitcode})",
            })
        return data.decode("utf-8")
    finally:
        parent_conn.close()
        if proc.is_alive():
            proc.kill()
        await asyncio.to_thread(proc.join, 5)


def _token_ok(auth_header: str, token: str) -> bool:
    if not auth_header.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth_header[len("Bearer "):], token)


def create_app(
    *,
    token: str,
    workspace: str,
    mounts_path: str = "/proc/mounts",
    max_concurrency: int = MAX_CONCURRENCY,
    default_timeout: int = DEFAULT_TIMEOUT,
    require_fuse: bool = True,
) -> FastAPI:
    """Build the daemon's FastAPI app.

    ``token`` and ``workspace`` are injected (instead of read from env
    inside the handlers) so tests can construct isolated apps.
    """
    app = FastAPI()
    sem = asyncio.Semaphore(max_concurrency)

    @app.get("/healthz")
    async def healthz() -> Response:
        # When the workspace is not a FUSE mount (Docker bind-mount or
        # ephemeral), the FUSE check is the wrong readiness signal; the
        # backend disables it via require_fuse=False.
        if not require_fuse or workspace_mounted(workspace, mounts_path):
            return Response(content="ok", status_code=200)
        return Response(content="workspace not mounted", status_code=503)

    @app.post("/execute")
    async def execute(request: Request) -> Response:
        auth = request.headers.get("authorization", "")
        if not _token_ok(auth, token):
            return Response(content="unauthorized", status_code=401)

        payload = await request.json()
        name = payload.get("name") or ""
        args = payload.get("args") or {}
        timeout = float(payload.get("timeout") or default_timeout)

        if not name:
            return Response(
                content=json.dumps({
                    "exit_code": 1,
                    "output": "",
                    "error": "No tool name provided",
                }),
                media_type="application/json",
            )

        # The _code payload may carry a credential — never log its args.
        if name == "_code":
            logger.info("→ _code")
        else:
            preview = json.dumps(args, default=str)[:200]
            logger.info("→ %s %s", name, preview)

        async with sem:
            result = await execute_in_child(name, args, workspace, timeout)
        logger.info("← %s (%d bytes)", name, len(result))
        return Response(content=result, media_type="application/json")

    return app


def main() -> None:
    """Daemon entry point — sandbox container main process."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    workspace = os.environ.get("WORKSPACE_DIR", "/workspace")
    port = int(os.environ.get("TOOL_EXECUTOR_PORT", str(DEFAULT_PORT)))
    token = os.environ.get("TOOL_EXECUTOR_TOKEN", "")
    if not token:
        logger.error("TOOL_EXECUTOR_TOKEN is required")
        sys.exit(1)

    # Docker/local backends bind-mount (or skip) the workspace instead of
    # FUSE-mounting it, so they set TOOL_EXECUTOR_REQUIRE_FUSE=0 to make
    # /healthz ready once the registry has loaded. Defaults on for K8s.
    require_fuse = os.environ.get("TOOL_EXECUTOR_REQUIRE_FUSE", "1") != "0"

    logger.info("Loading tool registry...")
    init_registry()
    logger.info("Registry loaded; serving on 0.0.0.0:%d", port)

    import uvicorn

    uvicorn.run(
        create_app(token=token, workspace=workspace, require_fuse=require_fuse),
        host="0.0.0.0",
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
