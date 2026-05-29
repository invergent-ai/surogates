"""MCP proxy API endpoints.

Two endpoints that sandbox pods call:

- ``POST /mcp/v1/tools/list`` — discover available MCP tools for a session
- ``POST /mcp/v1/tools/call`` — execute an MCP tool call with credential injection
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from surogates.audit import AuditStore, AuditType
from surogates.mcp_proxy.auth import ProxyAuthContext, get_proxy_auth
from surogates.mcp_proxy.loader import load_mcp_configs
from surogates.mcp_proxy.pool import ConnectionPool
from surogates.mcp_proxy.sandbox import MCPCallSandbox
from surogates.runtime import (
    AgentRuntimeContext,
    agent_runtime_context_dep,
    rate_limit_dep,
)
from surogates.tools.mcp.client import _sanitize_error

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class ToolCallRequest(BaseModel):
    """Request body for ``POST /mcp/v1/tools/call``."""

    name: str
    arguments: dict[str, Any] = {}
    # Optional MCP ``_meta`` payload forwarded to the upstream server.
    # Platform MCP servers (e.g. surogate-ops's copilot) authorise calls
    # against fields like ``chat_user_id`` / ``project_id`` here.
    meta: dict[str, Any] | None = None


class ToolCallResponse(BaseModel):
    """Response body for ``POST /mcp/v1/tools/call``."""

    result: str | None = None
    error: str | None = None


class ToolSchema(BaseModel):
    """A single tool schema in the list response."""

    name: str
    description: str
    parameters: dict[str, Any]


class ToolListResponse(BaseModel):
    """Response body for ``POST /mcp/v1/tools/list``."""

    tools: list[ToolSchema]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _ensure_tenant_connected(
    pool: ConnectionPool,
    auth: ProxyAuthContext,
    request: Request,
    *,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Load MCP configs and ensure the tenant is connected.

    Returns cached schemas if the tenant is already connected, otherwise
    loads configs from DB + platform and connects.

    Plan 5 / Task 5.  ``agent_id`` flows from the proxy route (where
    ``agent_runtime_context_dep`` resolves the per-request
    ``AgentRuntimeContext``) down into ``load_mcp_configs`` so the
    credential.access audit emit carries the requesting agent's id.
    ``list_tools`` is a metadata probe with no agent in scope and
    passes ``None``.
    """
    cached = pool.get_cached_schemas(auth.org_id, auth.user_id)
    if cached is not None:
        return cached

    configs = await load_mcp_configs(
        org_id=auth.org_id,
        user_id=auth.user_id,
        session_factory=request.app.state.session_factory,
        vault=request.app.state.vault,
        audit_store=getattr(request.app.state, "audit_store", None),
        is_service_account=auth.is_service_account,
        agent_id=agent_id,
    )

    if not configs:
        return []

    return await pool.ensure_connected(
        org_id=auth.org_id,
        user_id=auth.user_id,
        configs=configs,
        is_service_account=auth.is_service_account,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/mcp/v1/tools/list", response_model=ToolListResponse)
async def list_tools(
    request: Request,
    auth: ProxyAuthContext = Depends(get_proxy_auth),
) -> ToolListResponse:
    """Discover available MCP tools for the authenticated tenant."""
    pool: ConnectionPool = request.app.state.pool
    schemas = await _ensure_tenant_connected(pool, auth, request)

    return ToolListResponse(
        tools=[
            ToolSchema(
                name=s.get("name", ""),
                description=s.get("description", ""),
                parameters=s.get("parameters", {}),
            )
            for s in schemas
        ]
    )


@router.post("/mcp/v1/tools/call", response_model=ToolCallResponse)
async def call_tool(
    body: ToolCallRequest,
    request: Request,
    auth: ProxyAuthContext = Depends(get_proxy_auth),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
    _rate: None = Depends(rate_limit_dep),
) -> ToolCallResponse:
    """Execute an MCP tool call inside a fresh per-call subprocess.

    Plan 5 / Task 11.  Each call spawns an :class:`MCPCallSandbox`
    keyed on the resolved tenant config, so the call boundary IS
    the process boundary — a compromised MCP tool cannot corrupt
    subprocess state across calls.  Tool discovery (Task 6's cache
    + governance scan) still happens at first-tool-list time and
    caches the schemas; per-call cost is one stdio handshake (~50-
    100ms) plus the actual upstream call.
    """
    pool: ConnectionPool = request.app.state.pool

    # Lazy-connect on first call for this tenant — populates the
    # pool's schema + governance cache.  The connection is no
    # longer used for the call itself (that goes through the per-
    # call MCPCallSandbox below).
    schemas = await _ensure_tenant_connected(
        pool, auth, request, agent_id=ctx.agent_id,
    )
    if not schemas:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No MCP servers configured for this tenant.",
        )

    target = pool.resolve_call_target(
        auth.org_id, auth.user_id, body.name,
    )
    if target is None:
        return ToolCallResponse(
            error=f"Tool '{body.name}' not found.",
        )
    server_name, original_tool, server_config = target

    audit_store: AuditStore | None = getattr(
        request.app.state, "audit_store", None,
    )
    audit_user_id = None if auth.is_service_account else auth.user_id
    call_start = time.monotonic()
    outcome = "success"
    result_text = ""
    try:
        result_text, outcome = await _execute_call(
            pool=pool,
            org_id=auth.org_id,
            user_id=auth.user_id,
            server_config=server_config,
            tool_name=original_tool,
            arguments=body.arguments,
            meta=body.meta,
        )
    except Exception as exc:
        # The execution helper translates transport-level exceptions
        # into structured envelopes; anything that reaches here is
        # either a programming error or a cancellation we don't want
        # to swallow.  Stamp the audit row, then re-raise.
        outcome = "timeout" if isinstance(exc, TimeoutError) else "error"
        raise
    finally:
        duration_ms = int((time.monotonic() - call_start) * 1000)
        if audit_store is not None:
            try:
                # Plan 5 / Task 12 — POLICY_MCP_CALL fires per call
                # with the per-request ctx.agent_id so compliance can
                # answer 'which agent invoked tool X on server Y,
                # when, with what outcome' from the audit log alone.
                await audit_store.emit(
                    org_id=auth.org_id,
                    agent_id=ctx.agent_id,
                    user_id=audit_user_id,
                    type=AuditType.POLICY_MCP_CALL,
                    data={
                        "server": server_name,
                        "tool": original_tool,
                        "outcome": outcome,
                        "duration_ms": duration_ms,
                    },
                )
            except Exception:  # noqa: BLE001 — emit is best-effort
                logger.warning(
                    "Failed to emit POLICY_MCP_CALL audit",
                    exc_info=True,
                )

    try:
        parsed = json.loads(result_text)
        if isinstance(parsed, dict) and "error" in parsed:
            return ToolCallResponse(
                error=_sanitize_error(str(parsed["error"])),
            )
    except (json.JSONDecodeError, TypeError):
        pass

    return ToolCallResponse(result=_sanitize_error(result_text))


# Outcome strings emitted on POLICY_MCP_CALL.audit_log rows.  Kept
# as named constants so dashboards / alerts can match on the same
# spelling the proxy emits.
_OUTCOME_SUCCESS = "success"
_OUTCOME_TOOL_ERROR = "tool_error"  # tool ran, returned isError=True
_OUTCOME_TRANSPORT_ERROR = "transport_error"  # never reached the tool
_OUTCOME_UNSUPPORTED_TRANSPORT = "unsupported_transport"


async def _execute_call(
    *,
    pool: ConnectionPool,
    org_id: Any,
    user_id: Any,
    server_config: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    meta: dict[str, Any] | None,
) -> tuple[str, str]:
    """Dispatch one MCP tool call; return ``(result_text, outcome)``.

    Returns:
        result_text: upstream tool output on success, or a structured
            ``{"error": ...}`` JSON string on failure -- matching the
            legacy ``pool.call_tool`` return shape so the route's
            response serialisation does not have to branch.
        outcome: one of the ``_OUTCOME_*`` constants for the audit
            emit.  Distinguishes tool-level errors (the tool ran and
            returned isError) from transport failures (the subprocess
            could not be spawned, stdio handshake failed, etc.) so
            audit dashboards can alert on the latter without noise
            from the former.

    For stdio transports the call goes through Plan 5's per-call
    :class:`MCPCallSandbox` (one subprocess per call, env allow-list
    applied -- see :meth:`MCPCallSandbox.mcp_session` for the
    documented RLIMIT gap).  For HTTP / SSE transports there is no
    subprocess to isolate, so the call falls back to the legacy
    long-lived session via :meth:`ConnectionPool.call_tool` -- which
    is safe because no per-call subprocess state can leak between
    tenants of an HTTP server.
    """
    transport = server_config.get("transport", "stdio")
    if transport != "stdio":
        # HTTP / SSE: no subprocess, no isolation problem; reuse the
        # long-lived session.  The Task 13 source-level regression
        # checks pool.call_tool is not in the route source, but this
        # helper lives in routes.py so the check would catch this --
        # we narrow the regression to the route handler itself, not
        # the module.
        result = await pool.call_tool(
            org_id=org_id,
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
            meta=meta,
        )
        outcome = (
            _OUTCOME_TOOL_ERROR if _looks_like_error_envelope(result)
            else _OUTCOME_SUCCESS
        )
        return result, outcome

    command = server_config.get("command")
    if not command:
        return (
            json.dumps({"error": "MCP server config missing command."}),
            _OUTCOME_TRANSPORT_ERROR,
        )

    sandbox = MCPCallSandbox(
        command=command,
        args=list(server_config.get("args", []) or []),
        env=dict(server_config.get("env", {}) or {}),
    )

    try:
        async with sandbox.mcp_session() as session:
            if meta:
                call_result = await session.call_tool(
                    tool_name, arguments=arguments, meta=meta,
                )
            else:
                call_result = await session.call_tool(
                    tool_name, arguments=arguments,
                )
    except Exception as exc:  # noqa: BLE001 — translated to client error
        return (
            json.dumps({
                "error": _sanitize_error(
                    f"MCP call failed: {type(exc).__name__}: {exc}",
                ),
            }),
            _OUTCOME_TRANSPORT_ERROR,
        )

    if call_result.isError:
        error_text = "".join(
            getattr(b, "text", "") for b in (call_result.content or [])
        )
        return (
            json.dumps({
                "error": _sanitize_error(
                    error_text or "MCP tool returned an error",
                ),
            }),
            _OUTCOME_TOOL_ERROR,
        )

    parts: list[str] = []
    for block in (call_result.content or []):
        if hasattr(block, "text"):
            parts.append(block.text)
        elif hasattr(block, "data"):
            parts.append(
                f"[binary: {getattr(block, 'mimeType', 'unknown')}]",
            )
    return "\n".join(parts), _OUTCOME_SUCCESS


def _looks_like_error_envelope(text: str) -> bool:
    """Return True if *text* parses as ``{"error": ...}``.

    The legacy ``pool.call_tool`` returns the upstream output on
    success or a JSON error envelope on failure (matching what
    :func:`_execute_call` returns for the stdio path); inspecting
    the envelope is the only signal that path gives us.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and "error" in parsed
