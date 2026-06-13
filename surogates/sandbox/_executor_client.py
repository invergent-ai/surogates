"""Shared worker->executor-daemon HTTP transport.

Both the Kubernetes and Docker sandbox backends run the same in-sandbox
tool-executor daemon (``surogates.sandbox.executor_server``) and reach it
over HTTP with a per-sandbox bearer token.  This client owns that
transport: the pooled ``aiohttp`` session, the ``POST /execute`` call, the
standard result JSON, and the failure taxonomy.

It is deliberately stateless about *which* sandbox it is talking to --
callers pass ``host``/``port``/``token`` per call -- and it never touches
backend bookkeeping.  Fatal conditions (token rejected, daemon
unreachable) raise :class:`SandboxUnavailableError`; the calling backend
catches that, marks its own entry failed, and re-raises.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

from surogates.sandbox.base import SandboxUnavailableError

logger = logging.getLogger(__name__)

# Extra seconds added to the per-tool timeout for the client-side budget,
# and the connect-phase timeout that makes a blackholed host fail fast.
_BUDGET_SLACK = 5
_CONNECT_TIMEOUT = 10


class ExecutorHTTPClient:
    """Pooled HTTP client for the in-sandbox tool-executor daemon."""

    def __init__(self) -> None:
        self._http: aiohttp.ClientSession | None = None

    async def _get_http(self) -> aiohttp.ClientSession:
        """Shared client session -- connection pooling across tool calls."""
        if self._http is None:
            self._http = aiohttp.ClientSession()
        return self._http

    async def aclose(self) -> None:
        """Release the HTTP client session (worker shutdown)."""
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def execute(
        self,
        *,
        host: str,
        port: int,
        token: str,
        name: str,
        args_str: str,
        timeout: int,
    ) -> str:
        """POST one tool call to the daemon and return its JSON result.

        Raises :class:`SandboxUnavailableError` when the daemon rejects the
        token (401) or is unreachable (connection error).  HTTP errors and
        tool-level timeouts come back as a result-JSON string (no raise).
        """
        url = f"http://{host}:{port}/execute"
        try:
            args = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            args = {}

        session = await self._get_http()
        try:
            async with session.post(
                url,
                json={"name": name, "args": args, "timeout": timeout},
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(
                    total=timeout + _BUDGET_SLACK, connect=_CONNECT_TIMEOUT,
                ),
            ) as resp:
                body = await resp.text()
                if resp.status == 401:
                    raise SandboxUnavailableError(
                        f"Executor daemon at {host}:{port} rejected the "
                        f"sandbox token",
                    )
                if resp.status != 200:
                    logger.error(
                        "Executor daemon at %s:%s returned HTTP %s: %s",
                        host, port, resp.status, body[:200],
                    )
                    return self._result_json(
                        exit_code=-1,
                        stdout="",
                        stderr=f"Executor daemon error (HTTP {resp.status})",
                        truncated=False,
                        timed_out=False,
                    )
                return body
        except aiohttp.ClientConnectionError as exc:
            # ORDER MATTERS: aiohttp connect-phase timeouts inherit from both
            # ClientConnectionError and TimeoutError; they mean "unreachable"
            # and must land here, not in the tool-timeout branch below.
            logger.error("Sandbox daemon unreachable at %s:%s: %s", host, port, exc)
            raise SandboxUnavailableError(
                f"Sandbox daemon unreachable at {host}:{port}: {exc}",
            ) from exc
        except asyncio.TimeoutError:
            logger.warning("Sandbox exec timed out at %s:%s", host, port)
            return self._result_json(
                exit_code=-1,
                stdout="",
                stderr="Execution timed out",
                truncated=False,
                timed_out=True,
            )

    @staticmethod
    def _result_json(
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
        truncated: bool,
        timed_out: bool,
    ) -> str:
        """Build the standard sandbox result JSON (shared by all backends)."""
        return json.dumps({
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": truncated,
            "timed_out": timed_out,
        })


__all__ = ["ExecutorHTTPClient"]
