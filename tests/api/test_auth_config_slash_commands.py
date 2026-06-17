"""``GET /v1/auth/config`` exposes the agent's enabled slash commands so
the web SPA can hide disabled ones from the composer menu + navbar."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from surogates.api.routes.auth import auth_config
from surogates.runtime import AgentRuntimeContext, SlashCommandConfig


def _ctx(commands: frozenset[str]) -> AgentRuntimeContext:
    return AgentRuntimeContext(
        agent_id="a-1",
        org_id="o-1",
        project_id="p-1",
        enabled=True,
        config_version=1,
        storage_key_prefix="p-1/a-1",
        slash_commands=SlashCommandConfig(commands=commands),
    )


@pytest.mark.asyncio
async def test_auth_config_exposes_enabled_slash_commands():
    request = MagicMock()
    # No firebase cache → the early-return path; slash_commands must still
    # be populated.
    request.app.state.firebase_config_cache = None

    resp = await auth_config(
        request, agent_runtime=_ctx(frozenset({"clear", "loop", "compress"}))
    )

    assert sorted(resp.slash_commands) == ["clear", "compress", "loop"]


@pytest.mark.asyncio
async def test_auth_config_omits_disabled_slash_commands():
    request = MagicMock()
    request.app.state.firebase_config_cache = None

    resp = await auth_config(request, agent_runtime=_ctx(frozenset({"clear"})))

    assert resp.slash_commands == ["clear"]
