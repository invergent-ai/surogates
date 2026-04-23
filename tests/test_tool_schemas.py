"""Tests for :mod:`surogates.harness.tool_schemas`."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from surogates.harness.prompt import PromptBuilder
from surogates.harness.tool_schemas import filter_schemas_for_tenant
from surogates.tenant.context import TenantContext
from surogates.tools.loader import AgentDef


def _schema(name: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"stub description for {name}",
            "parameters": {"type": "object", "properties": properties},
        },
    }


def _gated_schema(name: str) -> dict[str, Any]:
    return _schema(name, {"goal": {"type": "string"}, "agent_type": {"type": "string"}})


def test_has_agents_true_is_noop() -> None:
    schemas = [_gated_schema("delegate_task"), _schema("read_file", {"path": {"type": "string"}})]
    snapshot = copy.deepcopy(schemas)

    result = filter_schemas_for_tenant(schemas, has_agents=True)

    assert result == snapshot
    assert schemas == snapshot


@pytest.mark.parametrize("tool_name", ["delegate_task", "spawn_worker"])
def test_strips_agent_type_when_no_agents(tool_name: str) -> None:
    result = filter_schemas_for_tenant([_gated_schema(tool_name)], has_agents=False)

    props = result[0]["function"]["parameters"]["properties"]
    assert "agent_type" not in props
    assert "goal" in props


def test_unrelated_tool_agent_type_preserved() -> None:
    # A tool that happens to have an ``agent_type`` property but is not
    # in the gated set must keep it.
    schemas = [_schema("read_file", {"path": {"type": "string"}, "agent_type": {"type": "string"}})]

    result = filter_schemas_for_tenant(schemas, has_agents=False)

    assert result[0] is schemas[0]
    assert "agent_type" in result[0]["function"]["parameters"]["properties"]


def test_input_not_mutated() -> None:
    schemas = [_gated_schema("delegate_task")]
    snapshot = copy.deepcopy(schemas)

    _ = filter_schemas_for_tenant(schemas, has_agents=False)

    assert schemas == snapshot


def test_mixed_list_partial_filtering() -> None:
    schemas = [
        _schema("read_file", {"path": {"type": "string"}}),
        _gated_schema("delegate_task"),
        _gated_schema("spawn_worker"),
        _schema("terminal", {"command": {"type": "string"}}),
    ]

    result = filter_schemas_for_tenant(schemas, has_agents=False)

    assert [s["function"]["name"] for s in result] == [
        "read_file", "delegate_task", "spawn_worker", "terminal",
    ]
    # Unrelated tools returned by reference; gated tools are deep-copied.
    assert result[0] is schemas[0]
    assert result[3] is schemas[3]
    assert result[1] is not schemas[1]
    assert result[2] is not schemas[2]


def test_prompt_builder_has_agents() -> None:
    tenant = TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={"agent_name": "Bot", "default_model": "gpt-4o"},
        user_preferences={},
        permissions=frozenset(),
        asset_root=str(Path("/tmp")),
    )
    enabled = AgentDef(
        name="worker", description="stub", system_prompt="body",
        source="platform", enabled=True,
    )
    disabled = AgentDef(
        name="worker", description="stub", system_prompt="body",
        source="platform", enabled=False,
    )

    assert PromptBuilder(tenant, available_agents=[]).has_agents is False
    assert PromptBuilder(tenant, available_agents=[enabled]).has_agents is True
    # Disabled agents are filtered out in __init__.
    assert PromptBuilder(tenant, available_agents=[disabled]).has_agents is False
