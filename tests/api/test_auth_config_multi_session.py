"""``GET /v1/auth/config`` exposes the agent's "multi session" capability
so the web SPA can hide "New chat" and pin the single conversation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from surogates.api.routes.auth import auth_config
from surogates.runtime import AgentRuntimeContext


def _ctx(**overrides) -> AgentRuntimeContext:
    return AgentRuntimeContext(
        agent_id="a-1",
        org_id="o-1",
        project_id="p-1",
        enabled=True,
        config_version=1,
        storage_key_prefix="p-1/a-1",
        **overrides,
    )


@pytest.mark.asyncio
async def test_auth_config_defaults_multi_session_on():
    request = MagicMock()
    request.app.state.firebase_config_cache = None

    resp = await auth_config(request, agent_runtime=_ctx())

    assert resp.multi_session is True


@pytest.mark.asyncio
async def test_auth_config_projects_multi_session_off():
    request = MagicMock()
    request.app.state.firebase_config_cache = None

    resp = await auth_config(request, agent_runtime=_ctx(multi_session=False))

    assert resp.multi_session is False
