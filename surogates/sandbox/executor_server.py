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
