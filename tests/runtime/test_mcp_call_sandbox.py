"""Tests for MCPCallSandbox.

Plan 5 / Task 9.  Async context manager that spawns a fresh
subprocess per MCP call with an explicit, vault-resolved env-var
allow-list — the parent process's environment is NOT inherited.
"""

from __future__ import annotations

import os
import pytest


@pytest.mark.asyncio
async def test_mcp_call_sandbox_passes_only_allowed_env_vars(monkeypatch):
    """The sandbox spawns the subprocess with env= explicitly set;
    the parent process's secrets (e.g. other tenants' credentials)
    are not inherited."""
    monkeypatch.setenv("PARENT_SECRET", "should-not-leak")

    from surogates.mcp_proxy.sandbox import MCPCallSandbox

    sandbox = MCPCallSandbox(
        command="env",  # POSIX util prints the subprocess env
        args=[],
        env={"ALLOWED_TOKEN": "x"},
        memory_limit_mb=64,
        cpu_seconds=1,
    )
    async with sandbox as proc:
        stdout, _ = await proc.communicate()
    assert b"ALLOWED_TOKEN=x" in stdout
    assert b"PARENT_SECRET" not in stdout


@pytest.mark.asyncio
async def test_mcp_call_sandbox_kills_runaway_subprocess_on_exit():
    """The async context manager always terminates the subprocess
    on exit so a tool that hangs doesn't leak processes."""
    from surogates.mcp_proxy.sandbox import MCPCallSandbox

    sandbox = MCPCallSandbox(
        command="sleep",
        args=["60"],
        env={},
        memory_limit_mb=64,
        cpu_seconds=1,
    )
    async with sandbox as proc:
        pid = proc.pid

    # After the context manager exits, the subprocess is dead.
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_mcp_call_sandbox_kills_on_exception_in_body():
    """If the calling code raises inside the context, the
    subprocess must still be cleaned up — leaking processes on
    error paths is the most common cause of pod resource leaks
    in long-running services."""
    from surogates.mcp_proxy.sandbox import MCPCallSandbox

    sandbox = MCPCallSandbox(
        command="sleep",
        args=["60"],
        env={},
        memory_limit_mb=64,
        cpu_seconds=1,
    )
    pid: int | None = None
    with pytest.raises(RuntimeError):
        async with sandbox as proc:
            pid = proc.pid
            raise RuntimeError("simulated tool error")
    assert pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
