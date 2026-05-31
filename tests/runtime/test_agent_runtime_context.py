"""Tests for ``surogates.runtime.AgentRuntimeContext``.

``AgentRuntimeContext`` carries the per-session
agent configuration the shared surogates runtime resolves from the
management plane's ``/api/agents/{id}/runtime-config`` endpoint.

It is *distinct* from ``surogates.tenant.TenantContext``, which carries
the request-time authentication principal (user / service-account /
channel-session).  The two compose at the call site:

  - ``TenantContext.org_id`` ↔ ``AgentRuntimeContext.org_id`` (always
    equal after resolution; an invariant the resolver enforces).
  - ``AgentRuntimeContext`` adds the agent identity, version,
    LLM endpoints, MCP allow-list, governance overlay, and the
    storage key prefix.
"""

from __future__ import annotations

import dataclasses

import pytest


def test_agent_runtime_context_required_fields():
    from surogates.runtime import AgentRuntimeContext

    ctx = AgentRuntimeContext(
        agent_id="a-1",
        org_id="o-1",
        project_id="p-1",
        enabled=True,
        config_version=1,
        storage_key_prefix="p-1/a-1",
    )
    assert ctx.agent_id == "a-1"
    assert ctx.org_id == "o-1"
    assert ctx.project_id == "p-1"
    assert ctx.enabled is True
    assert ctx.config_version == 1
    assert ctx.storage_key_prefix == "p-1/a-1"


def test_agent_runtime_context_optional_fields_default():
    from surogates.runtime import AgentRuntimeContext

    ctx = AgentRuntimeContext(
        agent_id="a-1",
        org_id="o-1",
        project_id="p-1",
        enabled=True,
        config_version=1,
        storage_key_prefix="p-1/a-1",
    )
    assert ctx.api_web_url is None
    assert ctx.llm_main is None
    assert ctx.llm_summary is None
    assert ctx.llm_vision is None
    assert ctx.llm_advisor is None
    assert ctx.mcp_server_ids == ()
    assert ctx.governance == {}


def test_agent_runtime_context_is_frozen():
    from surogates.runtime import AgentRuntimeContext

    ctx = AgentRuntimeContext(
        agent_id="a-1",
        org_id="o-1",
        project_id="p-1",
        enabled=True,
        config_version=1,
        storage_key_prefix="p-1/a-1",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.agent_id = "a-2"  # type: ignore[misc]


def test_llm_endpoint_fields_and_immutability():
    from surogates.runtime import LLMEndpoint

    ep = LLMEndpoint(
        model="gpt-4o",
        base_url="https://proxy/v1",
        api_key_ref="vault://primary",
    )
    assert ep.model == "gpt-4o"
    assert ep.base_url == "https://proxy/v1"
    assert ep.api_key_ref == "vault://primary"
    with pytest.raises(dataclasses.FrozenInstanceError):
        ep.model = "x"  # type: ignore[misc]


def test_asset_root_derives_from_org_id():
    """``asset_root`` mirrors the legacy on-disk layout used by the
    helm-deployed tenant_assets PVC: ``/data/tenant-assets/{org_id}``.

    Lift per-user mutable memory off this PVC and onto R2;
    until then the convention stays so existing harness code paths
    (memory tools, skill loaders) keep working when they read this
    property.
    """
    from surogates.runtime import AgentRuntimeContext

    ctx = AgentRuntimeContext(
        agent_id="a-1",
        org_id="org-abc",
        project_id="p-1",
        enabled=True,
        config_version=1,
        storage_key_prefix="p-1/a-1",
    )
    assert ctx.asset_root == "/data/tenant-assets/org-abc"


def test_llm_endpoint_attached_to_context():
    from surogates.runtime import AgentRuntimeContext, LLMEndpoint

    main = LLMEndpoint(model="m", base_url="u", api_key_ref="r")
    ctx = AgentRuntimeContext(
        agent_id="a-1",
        org_id="o-1",
        project_id="p-1",
        enabled=True,
        config_version=1,
        storage_key_prefix="p-1/a-1",
        llm_main=main,
    )
    assert ctx.llm_main is main


def test_mcp_server_ids_must_be_tuple():
    """``mcp_server_ids`` is a tuple, not a list — the dataclass is
    frozen so a list field would still be mutable in practice."""
    from surogates.runtime import AgentRuntimeContext

    ctx = AgentRuntimeContext(
        agent_id="a-1",
        org_id="o-1",
        project_id="p-1",
        enabled=True,
        config_version=1,
        storage_key_prefix="p-1/a-1",
        mcp_server_ids=("m1", "m2"),
    )
    assert isinstance(ctx.mcp_server_ids, tuple)
    assert ctx.mcp_server_ids == ("m1", "m2")


def test_governance_default_is_independent_per_instance():
    """The default-factory must not return a shared mutable dict."""
    from surogates.runtime import AgentRuntimeContext

    a = AgentRuntimeContext(
        agent_id="a",
        org_id="o",
        project_id="p",
        enabled=True,
        config_version=1,
        storage_key_prefix="p/a",
    )
    b = AgentRuntimeContext(
        agent_id="b",
        org_id="o",
        project_id="p",
        enabled=True,
        config_version=1,
        storage_key_prefix="p/b",
    )
    assert a.governance == {}
    assert b.governance == {}
    assert a.governance is not b.governance
