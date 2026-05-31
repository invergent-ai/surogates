"""Tests for AgentRuntimeContext bundle fields.

The runtime-config payload carries a Hub
reference + version that the worker uses to fetch the agent's
file bundle (SOUL.md, AGENT.md, platform skills, etc.).  Both
fields are optional so legacy agents that haven't been onboarded
to Hub-backed bundles yet still work.
"""

from __future__ import annotations

import dataclasses

import pytest

from surogates.runtime import AgentRuntimeContext


def test_agent_runtime_context_bundle_fields_default_to_none():
    """Backwards-compat: an agent with no bundle configured yet
    must construct without raising — the worker treats None as
    'no bundle, fall back to legacy filesystem reads'."""
    ctx = AgentRuntimeContext(
        agent_id="a-1", org_id="o-1", project_id="p-1",
        enabled=True, config_version=1, storage_key_prefix="p/a",
    )
    assert ctx.bundle_hub_ref is None
    assert ctx.bundle_version is None


def test_agent_runtime_context_bundle_fields_accept_values():
    ctx = AgentRuntimeContext(
        agent_id="a-1", org_id="o-1", project_id="p-1",
        enabled=True, config_version=1, storage_key_prefix="p/a",
        bundle_hub_ref="acme/agents",
        bundle_version="v1.2.3",
    )
    assert ctx.bundle_hub_ref == "acme/agents"
    assert ctx.bundle_version == "v1.2.3"


def test_agent_runtime_context_is_still_frozen():
    """adding bundle fields
    must not weaken the frozen=True contract."""
    ctx = AgentRuntimeContext(
        agent_id="a-1", org_id="o-1", project_id="p-1",
        enabled=True, config_version=1, storage_key_prefix="p/a",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.bundle_hub_ref = "x"  # type: ignore[misc]
