"""What a coordinator is told about its sub-agents.

On PROD a Flash coordinator called ``skill_view`` on each sub-agent, then spent
70k tokens exploring an empty workspace for file paths, because the section named
neither ``spawn_task`` nor what the coordinator cannot see.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from surogates.harness.prompt import PromptBuilder
from surogates.tenant.context import TenantContext
from surogates.tools.loader import AgentDef


def _section(tools: set[str]) -> str:
    tenant = TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    builder = PromptBuilder(
        tenant,
        session=SimpleNamespace(config={"coordinator": True}),
        available_tools=tools,
        available_agents=[
            AgentDef(name="analyst", description="Reads the numbers", system_prompt="", source="platform"),
        ],
    )
    return builder._available_agents_section()


def test_it_names_the_spawn_tools_the_session_has() -> None:
    section = _section({"spawn_task", "agent"})

    assert "``spawn_task`` or ``agent``" in section
    assert "spawn_worker" not in section


def test_it_lists_all_three_when_the_session_has_all_three() -> None:
    section = _section({"spawn_task", "spawn_worker", "agent"})

    assert "``spawn_task``, ``spawn_worker`` or ``agent``" in section


def test_one_tool_reads_as_one_tool() -> None:
    section = _section({"spawn_task"})

    assert "to ``spawn_task`` to use" in section


def test_it_says_they_are_not_skills_and_their_instructions_are_unseen() -> None:
    section = _section({"spawn_task"})

    assert "not skills" in section
    assert "not their instructions" in section
