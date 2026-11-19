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


def _context(available_tools, workspace_path="/data/ws", execution=None) -> str:
    now = datetime.now(timezone.utc)
    config = {"workspace_path": workspace_path} if workspace_path else {}
    if execution:
        config["execution"] = execution
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


DEVICE = {"kind": "device", "device_id": str(uuid4()), "device_name": "LAPTOP"}


def test_a_device_chat_names_its_folder_not_home():
    section = _context({"read_file"}, workspace_path="/mnt/c/Users/Ana/Scans", execution=DEVICE)
    assert "- **Workspace**: the chat's folder on the user's computer, `C:\\Users\\Ana\\Scans` (your working directory)" in section
    assert "`$HOME` (your working directory)" not in section
    assert "private scratch area" in section
    assert "not the user's Desktop" in section


def test_a_linux_device_chat_shows_its_path_as_it_is():
    section = _context({"read_file"}, workspace_path="/home/ana/proj", execution=DEVICE)
    assert "`/home/ana/proj` (your working directory)" in section


def test_shown_folder():
    from surogates.devices.binding import shown_folder

    assert shown_folder("/mnt/d/Profiles/x y/z") == "D:\\Profiles\\x y\\z"
    assert shown_folder("/mnt/c") == "C:\\"
    assert shown_folder("/home/ana") == "/home/ana"


def test_tools_path_is_shown_folders_inverse_on_a_windows_computer():
    from surogates.devices.binding import shown_folder, tools_path

    work = "/mnt/c/Users/Ana/work"
    for path in ("/mnt/d/Profiles/x y/z", "/mnt/c", "/mnt/c/Users/Ana/work/a.txt"):
        assert tools_path(shown_folder(path), work) == path
    assert tools_path("c:/Scans/", work) == "/mnt/c/Scans"
    # A drive named alone is its root, as shown_folder shows /mnt/d.
    for drive in ("D:", "D:\\", "D:/", "d:"):
        assert tools_path(drive, work) == "/mnt/d"
    # A relative name, and any name on a computer that is not Windows, are left as they are.
    assert tools_path("a\\b.txt", work) == "a\\b.txt"
    assert tools_path("C:\\a", "/home/ana/proj") == "C:\\a"


def test_a_device_chat_is_told_how_to_work_on_files_outside_its_folder():
    said = "Files outside the chat's folder: look in the chat's folder first"
    assert said in _context({"read_file"}, workspace_path="/home/ana/proj", execution=DEVICE)
    context = _context({"read_file"}, workspace_path="/home/ana/proj", execution=DEVICE)
    assert "never ask for the same thing in the chat instead" in context
    # The chat's own folder first, and a folder outside it only as the user named it: never a guessed one.
    assert (
        "Files outside the chat's folder: look in the chat's folder first (list_files on it). Only when what the "
        "user means is not there, ask once with request_folder_access, for the folder the user named (Desktop, "
        "Downloads, Documents, Pictures, or a path); if the user did not say where, ask them where instead of "
        "guessing a folder."
    ) in context
    # Folders outside show in a listing, and one may be a copy's destination.
    assert (
        "There list_files shows folders too, each with a trailing `/` (an empty one as well), and matches its "
        "pattern ignoring case; such a folder may be copy_files' `to`."
    ) in context
    # The direct move between two outside folders is offered, put_back is the agent's to call, and a move trashes
    # only what was copied.
    assert (
        "copy_files copies into the chat's folder, out of it, or straight between two folders outside it; the "
        "destination must be the chat's folder or inside a folder already granted."
    ) in context
    assert "then call put_back (the app asks the user)." in context
    assert "To move files, use move_files (one window" in context
    assert "to make a new file in a folder outside, make it in the chat's folder, then move_files it there" in context
    assert "Use move_to_trash alone only when the user wants files deleted." in context
    assert 'If the access is for "this request", it ends when you finish this task' in context
    assert "ask the user anything you need with ask_user_question" in context
    assert "On a Windows computer the tools name `C:\\a\\b` as `/mnt/c/a/b`; tell the user the `C:\\` form." in context
    assert "offer put_back" not in context
    assert "\u2014" not in context and "\u2013" not in context
    assert said not in _context({"read_file"})
