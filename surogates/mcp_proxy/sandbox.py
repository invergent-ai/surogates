"""Per-call subprocess sandbox for MCP stdio servers.

Plan 5 / Task 9.  Each MCP call spawns a fresh subprocess that
inherits ONLY the env vars the sandbox explicitly passes (the
parent process's environment is NOT inherited — this is the
isolation primitive that prevents tenant A's credentials from
leaking to tenant B's MCP server).

The Task 10 ``apply_rlimits`` hook is wired into the subprocess's
``preexec_fn`` so RLIMIT_AS + RLIMIT_CPU apply before ``exec()``;
an OOM-bomb or fork-bomb tool cannot take down the proxy pod.

The async context manager always terminates the subprocess on
exit — even on exception — so a tool that hangs (or that the
caller raises against mid-call) does not leak processes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

__all__ = ["MCPCallSandbox"]


class MCPCallSandbox:
    """Per-call stdio subprocess with explicit env + rlimits."""

    def __init__(
        self,
        *,
        command: str,
        args: list[str],
        env: dict[str, str],
        memory_limit_mb: int = 256,
        cpu_seconds: int = 30,
    ) -> None:
        self._command = command
        self._args = list(args)
        self._env = dict(env)
        self._memory_limit_mb = memory_limit_mb
        self._cpu_seconds = cpu_seconds
        self._proc: asyncio.subprocess.Process | None = None

    async def __aenter__(self) -> asyncio.subprocess.Process:
        from surogates.mcp_proxy.rlimits import apply_rlimits

        memory_limit_mb = self._memory_limit_mb
        cpu_seconds = self._cpu_seconds

        def _preexec() -> None:
            apply_rlimits(
                memory_limit_mb=memory_limit_mb,
                cpu_seconds=cpu_seconds,
            )

        self._proc = await asyncio.create_subprocess_exec(
            self._command,
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            preexec_fn=_preexec,
        )
        return self._proc

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            self._proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                self._proc.kill()
                await self._proc.wait()
            except ProcessLookupError:
                pass

    @asynccontextmanager
    async def mcp_session(self) -> AsyncIterator:
        """Yield an ``mcp.ClientSession`` backed by a fresh subprocess.

        Plan 5 / Task 11.  The route's per-call MCP execution path
        uses this method instead of the long-lived
        ``MCPServerTask.session`` reuse pattern.  Each call boundary
        is also a process boundary — a compromised tool cannot
        corrupt subprocess state that persists across calls.

        The mcp SDK's ``stdio_client`` spawns its own subprocess
        from ``StdioServerParameters`` and merges ``env=`` with the
        SDK's ``DEFAULT_INHERITED_ENV_VARS`` allow-list (PATH, HOME,
        etc.) — the parent process's tenant-scoped secrets (vault
        credentials, other agents' env injections) are NOT inherited
        beyond that conservative allow-list.

        Known gap (Plan 6 follow-up): the SDK's ``stdio_client`` does
        not expose ``preexec_fn``, so RLIMIT_AS / RLIMIT_CPU are not
        currently applied to the MCP subprocess (env-isolation is
        the primary defense today).  The lower-level
        ``__aenter__/__aexit__`` API on this class DOES apply the
        rlimits; Plan 6 closes the gap by either forking the SDK or
        wrapping its streams over our own subprocess.
        """
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
