"""Tests for build_agent_runtime_context's bundle-field projection.

Plan 3 / Task 2.  The runtime-config payload carries optional
bundle_hub_ref / bundle_version; the projection plumbs them onto
the AgentRuntimeContext.
"""

from __future__ import annotations

from surogates.runtime import build_agent_runtime_context


def _base_payload(**overrides) -> dict:
    return {
        "agent_id": "a-1",
        "org_id": "o-1",
        "project_id": "p-1",
        "enabled": True,
        "version": 1,
        "api_web_url": None,
        "llm_main": {"model": "m", "base_url": "u", "api_key_ref": "vault://k"},
        "llm_summary": None,
        "llm_vision": None,
        "llm_advisor": None,
        "mcp_server_ids": [],
        "governance": {},
        "storage_key_prefix": "p-1/a-1",
        **overrides,
    }


def test_build_extracts_bundle_fields_when_present():
    ctx = build_agent_runtime_context(_base_payload(
        bundle_hub_ref="acme/agents",
        bundle_version="v1.2.3",
    ))
    assert ctx.bundle_hub_ref == "acme/agents"
    assert ctx.bundle_version == "v1.2.3"


def test_build_defaults_bundle_fields_to_none_when_absent():
    """Plan 1's payload didn't include these fields; payloads from
    older surogate-ops versions must still project cleanly."""
    ctx = build_agent_runtime_context(_base_payload())
    assert ctx.bundle_hub_ref is None
    assert ctx.bundle_version is None


def test_build_treats_empty_strings_as_none():
    """A misconfigured payload that ships empty strings for the
    bundle fields must be treated as 'no bundle' so the worker
    falls back to the legacy filesystem reads instead of trying
    to fetch from an empty Hub ref."""
    ctx = build_agent_runtime_context(_base_payload(
        bundle_hub_ref="",
        bundle_version="",
    ))
    assert ctx.bundle_hub_ref is None
    assert ctx.bundle_version is None
