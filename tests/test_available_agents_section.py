"""What a session offered ``agent`` is told about its sub-agent types.

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


def _section(tools: set[str], config: dict | None = None) -> str:
    tenant = TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    builder = PromptBuilder(
        tenant,
        session=SimpleNamespace(config={"coordinator": True} if config is None else config),
        available_tools=tools,
        available_agents=[
            AgentDef(name="analyst", description="Reads the numbers", system_prompt="", source="platform"),
        ],
    )
    return builder._available_agents_section()


def test_one_tool_reads_as_one_tool() -> None:
    section = _section({"agent"})

    assert "to ``agent`` to use" in section


def test_a_plain_chat_offered_agent_sees_the_sub_agent_types() -> None:
    section = _section({"agent"}, config={})

    assert "analyst" in section and "``agent``" in section and "spawn_worker" not in section


def test_a_session_without_agent_sees_none() -> None:
    assert _section({"read_file"}, config={}) == ""


def test_a_project_master_sees_no_sub_agent_types() -> None:
    master = {"coordinator": True, "workstream_role": "coordinator", "workstream_id": "p1"}
    assert _section({"agent"}, config=master) == ""


def test_it_says_they_are_not_skills_and_their_instructions_are_unseen() -> None:
    section = _section({"agent"})

    assert "not skills" in section
    assert "not their instructions" in section
