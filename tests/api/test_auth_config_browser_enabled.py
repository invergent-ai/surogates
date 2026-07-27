"""``GET /v1/auth/config`` exposes the agent's "live browser support"
capability so the web SPA can hide the Browser Profiles tab and the
composer's browser-profile picker when it is off."""

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
async def test_auth_config_defaults_browser_enabled_on():
    request = MagicMock()
    request.app.state.firebase_config_cache = None
    request.app.state.runtime_config_cache = None

    resp = await auth_config(request, agent_runtime=_ctx())

    assert resp.browser_enabled is True


@pytest.mark.asyncio
async def test_auth_config_projects_browser_enabled_off():
    request = MagicMock()
    request.app.state.firebase_config_cache = None
    request.app.state.runtime_config_cache = None

    resp = await auth_config(request, agent_runtime=_ctx(browser_enabled=False))

    assert resp.browser_enabled is False


@pytest.mark.asyncio
async def test_auth_config_linkable_channels_default_empty():
    request = MagicMock()
    request.app.state.firebase_config_cache = None
    request.app.state.runtime_config_cache = None

    resp = await auth_config(request, agent_runtime=_ctx())

    assert resp.linkable_channels == []


@pytest.mark.asyncio
async def test_auth_config_projects_linkable_channels():
    request = MagicMock()
    request.app.state.firebase_config_cache = None
    request.app.state.runtime_config_cache = None

    resp = await auth_config(
        request,
        agent_runtime=_ctx(linkable_channels=("slack", "telegram")),
    )

    assert resp.linkable_channels == ["slack", "telegram"]
