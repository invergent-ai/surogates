"""``agent_id`` is a signed claim in the sandbox token, and the proxy
binds the per-request ``?agent_id=`` to it — defense-in-depth on top of
the loader/pool per-agent scoping.  A caller cannot request a different
agent's MCP servers than the one its token was minted for.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi import HTTPException

from surogates.mcp_proxy.auth import ProxyAuthContext, get_proxy_auth
from surogates.mcp_proxy.routes import _bind_agent
from surogates.runtime import AgentRuntimeContext
from surogates.tenant.auth.jwt import create_sandbox_token, decode_token

ORG = UUID(int=1)
USER = UUID(int=2)
SESS = UUID(int=3)


def _ctx(agent_id: str) -> AgentRuntimeContext:
    return AgentRuntimeContext(
        agent_id=agent_id, org_id=str(ORG), enabled=True,
        config_version=1, storage_key_prefix="",
    )


def _auth(agent_id: str | None) -> ProxyAuthContext:
    return ProxyAuthContext(
        org_id=ORG, user_id=USER, session_id=SESS, agent_id=agent_id,
    )


class _Req:
    def __init__(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}


def test_token_carries_agent_id_claim():
    tok = create_sandbox_token(ORG, USER, SESS, agent_id="agent-A")
    assert decode_token(tok)["agent_id"] == "agent-A"


def test_token_without_agent_id_has_no_claim():
    tok = create_sandbox_token(ORG, USER, SESS)
    assert "agent_id" not in decode_token(tok)


@pytest.mark.asyncio
async def test_get_proxy_auth_extracts_agent_id():
    tok = create_sandbox_token(ORG, USER, SESS, agent_id="agent-A")
    auth = await get_proxy_auth(_Req(tok))
    assert auth.agent_id == "agent-A"


@pytest.mark.asyncio
async def test_get_proxy_auth_agent_id_none_when_absent():
    tok = create_sandbox_token(ORG, USER, SESS)
    auth = await get_proxy_auth(_Req(tok))
    assert auth.agent_id is None


def test_bind_agent_rejects_mismatch():
    with pytest.raises(HTTPException) as exc:
        _bind_agent(_auth("agent-A"), _ctx("agent-B"))
    assert exc.value.status_code == 403


def test_bind_agent_allows_match():
    _bind_agent(_auth("agent-A"), _ctx("agent-A"))  # must not raise


def test_bind_agent_allows_absent_claim():
    # Backward-compat: a token without the claim is trusted on the query
    # param alone (loader/pool still enforce per-agent scoping).
    _bind_agent(_auth(None), _ctx("agent-B"))  # must not raise
