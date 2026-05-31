"""Tests for ``surogates.runtime.build_agent_runtime_context``.

Pure projection helper that turns the JSON payload
from the management plane into an :class:`AgentRuntimeContext`.  Lives
separate from the FastAPI dependency so it is unit-testable
without an app context.
"""

from __future__ import annotations

import pytest


def _minimum_payload() -> dict:
    return {
        "agent_id": "a-1",
        "org_id": "o-1",
        "project_id": "p-1",
        "enabled": True,
        "version": 3,
        "api_web_url": None,
        "llm_main": {"model": "gpt", "base_url": "u", "api_key_ref": "v"},
        "llm_summary": None,
        "llm_vision": None,
        "llm_advisor": None,
        "mcp_server_ids": [],
        "governance": {},
        "storage_key_prefix": "p-1/a-1",
    }


def test_projects_required_fields():
    from surogates.runtime import build_agent_runtime_context

    ctx = build_agent_runtime_context(_minimum_payload())
    assert ctx.agent_id == "a-1"
    assert ctx.org_id == "o-1"
    assert ctx.project_id == "p-1"
    assert ctx.enabled is True
    assert ctx.config_version == 3
    assert ctx.storage_key_prefix == "p-1/a-1"


def test_projects_llm_main_into_endpoint():
    from surogates.runtime import build_agent_runtime_context

    ctx = build_agent_runtime_context(_minimum_payload())
    assert ctx.llm_main is not None
    assert ctx.llm_main.model == "gpt"
    assert ctx.llm_main.base_url == "u"
    assert ctx.llm_main.api_key_ref == "v"


def test_optional_llm_clients_remain_none_when_payload_omits_them():
    from surogates.runtime import build_agent_runtime_context

    ctx = build_agent_runtime_context(_minimum_payload())
    assert ctx.llm_summary is None
    assert ctx.llm_vision is None
    assert ctx.llm_advisor is None


def test_optional_llm_clients_project_when_present():
    from surogates.runtime import build_agent_runtime_context

    payload = _minimum_payload()
    payload["llm_summary"] = {
        "model": "gpt-mini", "base_url": "u", "api_key_ref": "v2",
    }
    payload["llm_vision"] = {
        "model": "gpt-vision", "base_url": "u", "api_key_ref": "v3",
    }
    payload["llm_advisor"] = {
        "model": "gpt-advisor", "base_url": "u", "api_key_ref": "v4",
    }
    ctx = build_agent_runtime_context(payload)
    assert ctx.llm_summary is not None and ctx.llm_summary.model == "gpt-mini"
    assert ctx.llm_vision is not None and ctx.llm_vision.model == "gpt-vision"
    assert ctx.llm_advisor is not None and ctx.llm_advisor.model == "gpt-advisor"


def test_mcp_server_ids_become_a_tuple_not_a_list():
    from surogates.runtime import build_agent_runtime_context

    payload = _minimum_payload()
    payload["mcp_server_ids"] = ["m1", "m2", "m3"]
    ctx = build_agent_runtime_context(payload)
    assert isinstance(ctx.mcp_server_ids, tuple)
    assert ctx.mcp_server_ids == ("m1", "m2", "m3")


def test_governance_is_independent_copy():
    """Caller's payload mutations after projection must not leak."""
    from surogates.runtime import build_agent_runtime_context

    payload = _minimum_payload()
    payload["governance"] = {"enabled": True}
    ctx = build_agent_runtime_context(payload)
    payload["governance"]["enabled"] = False
    assert ctx.governance == {"enabled": True}


def test_api_web_url_passes_through():
    from surogates.runtime import build_agent_runtime_context

    payload = _minimum_payload()
    payload["api_web_url"] = "https://web.example.com"
    ctx = build_agent_runtime_context(payload)
    assert ctx.api_web_url == "https://web.example.com"


def test_disabled_payload_projects_into_context():
    """``enabled=False`` is a valid intermediate state — the resolver
    decides what to do with it, the projection just carries the bit
    through."""
    from surogates.runtime import build_agent_runtime_context

    payload = _minimum_payload()
    payload["enabled"] = False
    ctx = build_agent_runtime_context(payload)
    assert ctx.enabled is False


def test_missing_required_field_raises_key_error():
    """The projection trusts the schema — surogate-ops always sends a
    well-formed payload.  A missing required field is a schema drift
    bug; surface it loudly rather than silently default."""
    from surogates.runtime import build_agent_runtime_context

    payload = _minimum_payload()
    del payload["storage_key_prefix"]
    with pytest.raises(KeyError):
        build_agent_runtime_context(payload)


def test_missing_optional_collections_default_safely():
    """If surogate-ops omits an optional list/dict (because it is
    absent rather than empty), the projection must still produce a
    valid context."""
    from surogates.runtime import build_agent_runtime_context

    payload = _minimum_payload()
    del payload["mcp_server_ids"]
    del payload["governance"]
    del payload["api_web_url"]
    ctx = build_agent_runtime_context(payload)
    assert ctx.mcp_server_ids == ()
    assert ctx.governance == {}
    assert ctx.api_web_url is None
