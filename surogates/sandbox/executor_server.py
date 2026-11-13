"""Persistent tool-executor daemon — runs inside the sandbox pod.

Replaces the per-call K8s-exec'd ``tool-executor`` script.  Boot order:
load the tool registry once (the ~7.5 CPU-second import cost is paid
during pod startup, before the port binds), then serve HTTP:

    POST /execute   {"name": ..., "args": {...}, "timeout": 300}
                    ``{"name": "_background"}`` drains the daemon's finished
                    background commands (see below)
    GET  /healthz   -> 200 when $WORKSPACE_DIR has a live FUSE mount; in a
                       thread's pod, when $PROJECT_DIR has one and the
                       thread's copy is made

Each ``/execute`` forks a child process (``multiprocessing`` fork
context — the warm registry is inherited copy-on-write) that runs the
dispatch on a fresh event loop and writes the result JSON to a pipe.
Tool handlers were written for process-per-call execution: they block,
burn CPU, and can crash — none of which may touch the serving loop, or
the readinessProbe would flap, ``_map_pod_status`` would report
``PENDING``, and ``SandboxPool.ensure`` would destroy the sandbox
mid-tool.  On timeout the child is killed (the old exec path abandoned
the remote process and left it running).

A background command (``terminal`` with ``background``) and every
``process`` action run in the daemon instead, on a thread: the process
registry that keeps a background command's output and exit lives in the
process that started it, and a fork's dies with the fork.  Kept in the
daemon, a command lives as long as the pod, ``process`` finds it there, and
``_background`` tells the worker which ones that report back have ended
(``surogates.harness.background_watch``).

The worker authenticates with ``Authorization: Bearer
$TOOL_EXECUTOR_TOKEN``; ``/healthz`` is unauthenticated (the kubelet
probes it).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
import multiprocessing
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response

from surogates.sandbox.base import MAX_FILE_BYTES
from surogates.sandbox.history import UNKNOWN_STEP, History, HistoryError

logger = logging.getLogger("tool-executor")

DEFAULT_PORT = 8071
DEFAULT_TIMEOUT = 300
MAX_CONCURRENCY = 8
# How often a running call looks whether its caller has hung up.
_DISCONNECT_POLL_S = 0.5

# Fork context: children inherit the warm registry copy-on-write.  The
# spawn context would re-import everything and defeat the daemon.
_MP = multiprocessing.get_context("fork")

# Populated once by init_registry() before the port binds; forked
# children read it via module global.
_REGISTRY: Any = None

# Resolved path -> mtime at the moment the agent read it. Lives in the
# parent because the children are forked per call and never write back:
# a tracker entry a handler makes is discarded with the child that made
# it. One daemon serves one sandbox, so a single unkeyed map is the whole
# session's read record.
_READ_TIMESTAMPS: dict[str, float] = {}

# Reads recorded per sandbox, bounding the map for a long session. Same
# order as the in-handler tracker cap.
_MAX_READ_TIMESTAMPS = 1024

# Tools whose success means the agent has now seen the file's content.
_READ_TOOL_NAMES = frozenset({"read_file", "write_file", "patch"})

# A landing's steps, as ``_history`` actions, and the History method each runs.
_HISTORY_STEPS = {
    "fetch": "fetch", "pickup": "pickup", "commit": "commit_turn", "apply": "apply", "unapply": "unapply", "record": "record",
    "keep": "keep", "prune": "prune", "hand_off": "hand_off", "hand_back": "hand_back",
    "keep_apart": "keep_apart", "take_up": "take_up", "drop_hand_off": "drop_hand_off", "opened": "opened",
}
# The steps a master's pod runs, which has no copy: a routine run's pickup,
# what settles a landing left running before it, the look and a put-back,
# and the day's pruning after it, which reads no copy.
_MASTER_STEPS = frozenset({"pickup", "fetch", "unapply", "prune"})


def _record_read(name: str, args: dict, workspace: str, result: str) -> None:
    """Note that *args["path"]* has been seen, if the call succeeded.

    Writes count as reads: after write_file or patch the agent knows the
    content, so a follow-up overwrite is not blind. Failures record
    nothing -- a refused write must not authorise the retry.
    """
    if name not in _READ_TOOL_NAMES:
        return
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return
    try:
        if json.loads(result).get("error"):
            return
    except (TypeError, ValueError, AttributeError):
        return
    try:
        resolved = os.path.realpath(
            os.path.expanduser(path)
            if path.startswith("~") or os.path.isabs(path)
            else os.path.join(workspace, path)
        )
        mtime = os.path.getmtime(resolved)
    except OSError:
        return
    if len(_READ_TIMESTAMPS) >= _MAX_READ_TIMESTAMPS:
        _READ_TIMESTAMPS.clear()
    _READ_TIMESTAMPS[resolved] = mtime


#: Completions a drain leaves for other workers' watches, at most: of commands no live watch asks for, the oldest go.
MAX_KEPT_COMPLETIONS = 256


def runs_in_daemon(name: str, args: dict) -> bool:
    """Whether the call *name* with *args* runs in the daemon, where background commands live, and not in a fork."""
    return name in ("process", "_background", "_monitor") or (name == "terminal" and bool(args.get("background")))


def start_monitor(args: dict, workspace: str) -> str:
    """``_monitor``: start a background command whose output lines are kept for the drain (``drain_background``)."""
    from surogates.tools.workspace_io.local import LocalWorkspaceIO
    from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError

    from surogates.tools.utils.process_registry import process_registry

    task_id = str(args.get("task_id") or "monitor")
    limit = args.get("max_running")
    if isinstance(limit, int) and process_registry.running_count(task_id) >= limit:
        return json.dumps({"error": f"{limit} monitors already run"})
    try:
        started = asyncio.run(LocalWorkspaceIO(workspace_path=workspace).start(
            str(args.get("command") or ""), workdir=args.get("workdir"), task_id=task_id, pty=False,
            notify_on_complete=True, watcher_interval=None, watch_lines=True,
        ))
    except WorkspaceSandboxError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(started)


def drain_background(args: dict | None = None) -> str:
    """``_background``: the completions of the background commands that report back, ended since the last
    drain, the ids of every command the registry knows, running or finished, and the monitors' new lines.

    A command the worker watches that is neither known nor among the
    completions ran in a pod that is gone.  One that finished and whose
    completion is not queued yet is still known: the next drain has it.
    With *args* ``ids``, only those commands' completions and lines are
    taken: the rest stay for the drain of the worker that watches them.
    """
    import queue

    from surogates.tools.utils.process_registry import process_registry

    ids = (args or {}).get("ids")
    wanted = {str(i) for i in ids} if isinstance(ids, list) else None
    events: list[dict] = []
    kept: list[dict] = []
    while True:
        try:
            entry = process_registry.completion_queue.get_nowait()
        except queue.Empty:
            break
        if wanted is None or entry.get("session_id") in wanted:
            events.append(entry)
        else:
            kept.append(entry)
    # Another worker's, put back for its drain.  One of a command the registry
    # has forgotten (``FINISHED_TTL_SECONDS``), or past the bound, has no watch left to ask for it.
    kept = [entry for entry in kept if process_registry.get(str(entry.get("session_id"))) is not None]
    for entry in kept[-MAX_KEPT_COMPLETIONS:]:
        process_registry.completion_queue.put(entry)
    lines = process_registry.drain_lines(wanted)
    known = [entry["session_id"] for entry in process_registry.list_sessions()]
    return json.dumps({"events": events, "known": known, "lines": lines})


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

    if action == "take":
        # Always the workspace: a restore puts back the workspace.
        ok = mgr.ensure_checkpoint(workspace, args.get("reason", "auto"))
        result: dict = {"success": ok, "action": "take"}
        if ok:
            h = mgr.latest_hash(workspace)
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


def _run_copy_checkpoint(args: dict, history: History) -> str:
    """``_checkpoint`` in a thread's pod: snapshots are commits on its
    branch, taken in its copy, and a restore removes the files they lack."""
    action = args.get("action", "take")
    try:
        if action == "take":
            return json.dumps({"success": True, "action": "take", "hash": history.snapshot(args.get("reason", "auto"))})
        if action == "restore":
            history.restore(args.get("hash", ""))
            return json.dumps({"success": True, "restored_to": args.get("hash", "")[:8]})
    except HistoryError as exc:
        return json.dumps({"success": False, "error": str(exc)})
    return json.dumps({"success": False, "error": f"Unknown checkpoint action: {action}"})


def _run_history(args: dict, history: History | None) -> str:
    """``_history``: one step of a landing, run on the pod's history."""
    if history is None:
        return json.dumps({"error": "This pod has no copy of a project's files"})
    step = dict(args)
    action = step.pop("action", None)
    method = _HISTORY_STEPS.get(action)
    if method is None:
        return json.dumps({"error": f"{UNKNOWN_STEP}: {args.get('action')}"})
    if history.copy is None and action not in _MASTER_STEPS:
        return json.dumps({"error": f"This pod has no copy of a project's files: it cannot {action}"})
    try:
        return json.dumps(getattr(history, method)(**step))
    # A real file's write can time out or fail on the mount, past git's own errors.
    except (HistoryError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
        return json.dumps({"error": str(exc)})


def _run_file(args: dict, workspace: str) -> str:
    """``_file``: read or write one file of the workspace, base64, up to 50 MiB.

    In a thread's pod the workspace is its copy: the harness tools that
    would reach the project's files through object storage come here.
    """
    path = str(args.get("path") or "")
    try:
        root = os.path.realpath(workspace)
        target = os.path.realpath(os.path.join(root, path))
        if not path or os.path.commonpath([root, target]) != root or target == root:
            return json.dumps({"error": f"{path} is outside the workspace"})
        too_large = f"{path} is over the 50 MiB a file may be"
        if args.get("action") == "read":
            if not os.path.isfile(target):
                return json.dumps({"error": f"{path} not found"})
            if os.path.getsize(target) > MAX_FILE_BYTES:
                return json.dumps({"error": too_large})
            with open(target, "rb") as fh:
                return json.dumps({"content_b64": base64.b64encode(fh.read()).decode()})
        if args.get("action") == "write":
            content = args.get("content_b64") or ""
            # Refused before it is decoded: base64 spends four characters on three bytes.
            if len(content) > 4 * -(-MAX_FILE_BYTES // 3):
                return json.dumps({"error": too_large})
            data = base64.b64decode(content)
            if len(data) > MAX_FILE_BYTES:
                return json.dumps({"error": too_large})
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # Written beside the file, then renamed over it: a write cut short
            # leaves the file whole.  A short name, so a file's near the length
            # limit fits too; history leaves out the *~ name.
            staged = Path(target).with_name(f".~{os.urandom(4).hex()}.file~")
            try:
                staged.write_bytes(data)
                os.replace(staged, target)
            finally:
                staged.unlink(missing_ok=True)
            return json.dumps({"ok": True, "bytes": len(data)})
        return json.dumps({"error": f"Unknown file action: {args.get('action')}"})
    except (OSError, ValueError) as exc:  # a bad name or encoding, a folder in the way, a full disk
        return json.dumps({"error": str(exc)})


def run_tool(
    name: str,
    args: dict,
    workspace: str,
    read_timestamps: dict | None = None,
    history: History | None = None,
) -> str:
    """Dispatch one tool call through the real handlers.

    Runs inside the forked child — blocking calls and CPU burn are fine
    here.  Result shapes mirror the old CLI exactly.

    *read_timestamps* is the parent's record of files read this session,
    seeded into the child's tracker so read-dependent guards (blind
    overwrite, staleness) can see reads that happened in earlier forks.

    *history* is a thread's pod's: its checkpoints are its copy's.  A
    master's pod's has no copy: its checkpoints stay its workspace's.
    """
    if read_timestamps:
        from surogates.tools.builtin.file_ops import seed_read_timestamps

        seed_read_timestamps(read_timestamps)

    if name == "_checkpoint":
        if history is not None and history.copy is not None:
            return _run_copy_checkpoint(args, history)
        return _run_checkpoint(args, workspace)
    if name == "_history":
        return _run_history(args, history)
    if name == "_file":
        return _run_file(args, workspace)
    if name == "_background":
        return drain_background(args)
    if name == "_monitor":
        return start_monitor(args, workspace)
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


def _child_main(
    conn: Any,
    name: str,
    args: dict,
    workspace: str,
    read_timestamps: dict | None = None,
    history: History | None = None,
) -> None:
    """Entry point of the forked child: run the tool, ship the result."""
    # The forked thread inherits the parent's "running event loop"
    # marker; clear it so run_tool's asyncio.run() can start a fresh
    # loop.  Python 3.12's asyncio also resets this in an at-fork hook —
    # this line is belt-and-braces against that hook changing.
    asyncio._set_running_loop(None)
    try:
        result = run_tool(name, args, workspace, read_timestamps, history)
    except BaseException as exc:  # never die without reporting
        result = json.dumps({"exit_code": 1, "output": "", "error": str(exc)})
    try:
        conn.send_bytes(result.encode("utf-8"))
    finally:
        conn.close()


async def execute_in_child(
    name: str, args: dict, workspace: str, timeout: float, history: History | None = None,
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
        target=_child_main,
        args=(child_conn, name, args, workspace, dict(_READ_TIMESTAMPS), history),
        daemon=True,
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
            result = data.decode("utf-8")
            _record_read(name, args, workspace, result)
            return result
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
    finally:
        parent_conn.close()
        if proc.is_alive():
            proc.kill()
        await asyncio.to_thread(proc.join, 5)


def _give_up(reason: str, log: str | Path = "/dev/termination-log") -> None:
    """Stop the daemon for good, *reason* the pod's termination message.

    The pod fails at once, and its provision with it, rather than staying
    not ready until the ready timeout.
    """
    try:
        Path(log).write_text(reason)
    except OSError:
        logger.warning("Could not write the pod's termination message", exc_info=True)
    os._exit(1)


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
    history: History | None = None,
    give_up: Callable[[str], None] | None = None,
) -> FastAPI:
    """Build the daemon's FastAPI app.

    ``token`` and ``workspace`` are injected (instead of read from env
    inside the handlers) so tests can construct isolated apps.

    A thread's pod has a *history*: the project's real files are mounted at
    its ``project``, and ``workspace`` is the thread's copy, made from them
    before the pod reports ready.  When it can never be made in this pod,
    *give_up* is called with the reason.
    """
    app = FastAPI()
    sem = asyncio.Semaphore(max_concurrency)
    mount = str(history.project) if history is not None else workspace
    # A master's pod has nothing to open: its workspace is the real files.
    opened = history is None or history.copy is None
    # Why the copy can never be made in this pod: it stays not ready.
    failed: str | None = None
    open_lock = asyncio.Lock()

    @app.get("/healthz")
    async def healthz() -> Response:
        nonlocal opened, failed
        # When the workspace is not a FUSE mount (Docker bind-mount or
        # ephemeral), the FUSE check is the wrong readiness signal; the
        # backend disables it via require_fuse=False.
        if require_fuse and not workspace_mounted(mount, mounts_path):
            return Response(content="workspace not mounted", status_code=503)
        async with open_lock:
            if failed is None and not opened:
                try:
                    await asyncio.to_thread(history.open)
                except (HistoryError, OSError) as exc:
                    logger.error("The thread's copy was not made: %s", exc)
                    # open() starts over only while it has no repository:
                    # once made, a copy may be half checked out, so a
                    # retry cannot make it again.
                    if (history.repo / "HEAD").exists():
                        failed = str(exc)
                        if give_up is not None:
                            give_up(f"copy not made: {failed}")
                    return Response(content=f"copy not made: {exc}", status_code=503)
                opened = True
        if failed is not None:
            return Response(content=f"copy not made: {failed}", status_code=503)
        return Response(content="ok", status_code=200)

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

        # The _code payload may carry a credential, and _file's a whole file:
        # never log their args.
        if name in ("_code", "_file"):
            logger.info("→ %s", name)
        else:
            preview = json.dumps(args, default=str)[:200]
            logger.info("→ %s %s", name, preview)

        if name == "_history" and history is not None and require_fuse and not workspace_mounted(mount, mounts_path):
            # The sidecar went, and /project is an empty folder: a landing
            # written there would never reach the bucket.
            return Response(
                content=json.dumps({"error": f"The project's files are not mounted at {mount}"}),
                media_type="application/json",
            )

        if name == "_background":
            # A drain reads memory and never waits on a tool: no slot, so the
            # worker's watch is answered while long calls fill them.
            result = drain_background(args)
            return Response(content=result, media_type="application/json")
        async def run() -> str:
            async with sem:
                if runs_in_daemon(name, args):
                    try:
                        return await asyncio.wait_for(
                            asyncio.to_thread(run_tool, name, args, workspace, None, history), timeout,
                        )
                    except TimeoutError:
                        return _timed_out_result()
                return await execute_in_child(name, args, workspace, timeout, history)

        # A caller that hangs up (a stopped agent's call, cancelled) wants no result: the call stops, its child
        # killed, and frees its slot, instead of running on to its own timeout.
        work = asyncio.ensure_future(run())
        while not work.done():
            await asyncio.wait({work}, timeout=_DISCONNECT_POLL_S)
            if not work.done() and await request.is_disconnected():
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work
                logger.info("× %s: its caller hung up", name)
                return Response(status_code=499)
        result = work.result()
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
    # Out of the environment once read: the commands the daemon starts, and its
    # forks' tools, inherit it, and none of them is to call the daemon.
    token = os.environ.pop("TOOL_EXECUTOR_TOKEN", "")
    if not token:
        logger.error("TOOL_EXECUTOR_TOKEN is required")
        sys.exit(1)

    # Docker/local backends bind-mount (or skip) the workspace instead of
    # FUSE-mounting it, so they set TOOL_EXECUTOR_REQUIRE_FUSE=0 to make
    # /healthz ready once the registry has loaded. Defaults on for K8s.
    require_fuse = os.environ.get("TOOL_EXECUTOR_REQUIRE_FUSE", "1") != "0"

    # A thread's pod: the real files at PROJECT_DIR, its copy at the
    # workspace, and the history in the pod's home.
    project = os.environ.get("PROJECT_DIR")
    history = None
    if project:
        from surogates.tools.utils.checkpoint_manager import _shadow_repo_path

        user = os.environ.get("USER_ID")
        if not user:
            logger.error("USER_ID is required in a thread's pod: the project's history is made as its user")
            sys.exit(1)
        helper = os.environ.get("HISTORY_HELPER")
        if not helper and not os.environ.get("HISTORY_TURN"):
            logger.error("HISTORY_TURN is required in a thread's pod: its copy is opened for a turn of the thread")
            sys.exit(1)
        history = History(
            repo=_shadow_repo_path(project, base=Path.home() / ".surogates" / "history"),
            project=Path(project), copy=Path(workspace),
            thread=os.environ["HISTORY_THREAD"], user=user, helper=helper, turn=os.environ.get("HISTORY_TURN"),
        )
    elif os.environ.get("HISTORY_MAIN"):
        from surogates.tools.utils.checkpoint_manager import _shadow_repo_path

        # A project's master pod: the real files are its workspace, and a
        # routine run's end picks their changes up as the routine's.
        history = History(
            repo=_shadow_repo_path(workspace, base=Path.home() / ".surogates" / "history"),
            project=Path(workspace), copy=None, thread=None, user=os.environ.get("USER_ID", ""),
        )

    logger.info("Loading tool registry...")
    init_registry()
    logger.info("Registry loaded; serving on 0.0.0.0:%d", port)

    import uvicorn

    uvicorn.run(
        create_app(token=token, workspace=workspace, require_fuse=require_fuse, history=history, give_up=_give_up),
        host="0.0.0.0",
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
