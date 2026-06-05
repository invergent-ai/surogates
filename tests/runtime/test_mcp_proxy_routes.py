"""Route-level enforcement for per-agent MCP scoping.

* Strict empty allow-list ⇒ ``tools/call`` returns 404 (no servers).
* The signed ``agent_id`` claim binds ``?agent_id=``: a mismatch is 403.
* A matching (or absent) claim is allowed.

Built on a bare app with the router mounted and the auth / context / rate
dependencies overridden, so no DB, Redis, or lifespan is needed.  The
empty allow-list short-circuits the loader before any DB access.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from surogates.mcp_proxy.auth import ProxyAuthContext, get_proxy_auth
from surogates.mcp_proxy.pool import ConnectionPool
from surogates.mcp_proxy.routes import router
from surogates.runtime import (
    AgentRuntimeContext,
    agent_runtime_context_dep,
    rate_limit_dep,
)

ORG = UUID(int=1)
USER = UUID(int=2)
SESS = UUID(int=3)


def _ctx(agent_id: str = "agent-A", mcp_server_ids=()) -> AgentRuntimeContext:
    return AgentRuntimeContext(
        agent_id=agent_id, org_id=str(ORG), enabled=True,
        config_version=1, storage_key_prefix="",
        mcp_server_ids=tuple(mcp_server_ids),
    )


def _client(auth: ProxyAuthContext, ctx: AgentRuntimeContext) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.pool = ConnectionPool()
    app.state.session_factory = None  # never reached for an empty allow-list
    app.state.vault = None
    app.dependency_overrides[get_proxy_auth] = lambda: auth
    app.dependency_overrides[agent_runtime_context_dep] = lambda: ctx
    app.dependency_overrides[rate_limit_dep] = lambda: None
    return TestClient(app)


def test_empty_allowlist_call_returns_404():
    auth = ProxyAuthContext(org_id=ORG, user_id=USER, session_id=SESS)
    client = _client(auth, _ctx(mcp_server_ids=()))
    resp = client.post(
        "/mcp/v1/tools/call", json={"name": "mcp__x__y", "arguments": {}},
    )
    assert resp.status_code == 404


def test_agent_id_claim_mismatch_returns_403():
    auth = ProxyAuthContext(
        org_id=ORG, user_id=USER, session_id=SESS, agent_id="agent-A",
    )
    client = _client(auth, _ctx(agent_id="agent-B"))
    resp = client.post("/mcp/v1/tools/list", json={})
    assert resp.status_code == 403


def test_matching_claim_empty_allowlist_lists_no_tools():
    auth = ProxyAuthContext(
        org_id=ORG, user_id=USER, session_id=SESS, agent_id="agent-A",
    )
    client = _client(auth, _ctx(agent_id="agent-A", mcp_server_ids=()))
    resp = client.post("/mcp/v1/tools/list", json={})
    assert resp.status_code == 200
    assert resp.json() == {"tools": []}
