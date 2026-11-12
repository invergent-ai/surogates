"""The Workspace block (`$HOME`, file-tool and terminal rules) is shown only to
a session that has a tool working there.

An agent whose policy denies the file tools and the terminal (a pure MCP
agent) was shown it anyway; the model then looked for the task's data in
`$HOME` and called `terminal`, which it does not have.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from surogates.harness.prompt import WORKSPACE_TOOLS, PromptBuilder
from surogates.session.models import Session
from surogates.tenant.context import TenantContext

MCP_AGENT_TOOLS = {"memory", "todo", "delegate_task", "consult_expert", "create_artifact", "mcp__crm__search"}


def _context(available_tools, workspace_path="/data/ws") -> str:
    now = datetime.now(timezone.utc)
    config = {"workspace_path": workspace_path} if workspace_path else {}
    session = Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel="web",
        status="active", config=config, created_at=now, updated_at=now,
    )
    tenant = TenantContext(
        org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    return PromptBuilder(tenant, session=session, available_tools=available_tools)._context_section()


def test_a_session_without_file_tools_or_terminal_gets_no_workspace_block():
    section = _context(MCP_AGENT_TOOLS)

    assert "**Workspace**" not in section
    assert "## Workspace" not in section
    assert "terminal" not in section


def test_a_session_with_a_file_tool_or_the_terminal_gets_the_workspace_block():
    for tool in sorted(WORKSPACE_TOOLS):
        section = _context(MCP_AGENT_TOOLS | {tool})
        assert "- **Workspace**: `$HOME` (your working directory)" in section, tool
        assert "## Workspace" in section, tool


def test_tools_narrowed_away_take_the_workspace_block_with_them():
    now = datetime.now(timezone.utc)
    session = Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel="web",
        status="active", config={"workspace_path": "/data/ws"}, created_at=now, updated_at=now,
    )
    tenant = TenantContext(
        org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    builder = PromptBuilder(tenant, session=session, available_tools=MCP_AGENT_TOOLS | {"terminal", "read_file"})
    builder.restrict_tools(MCP_AGENT_TOOLS)

    assert "## Workspace" not in builder._context_section()


def test_without_a_workspace_or_a_tool_list_nothing_changes():
    assert "## Workspace" not in _context({"terminal"}, workspace_path=None)
    assert "## Workspace" in _context(None)
