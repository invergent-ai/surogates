"""Where the office tools are offered."""

from types import SimpleNamespace

from surogates.harness.tool_schemas import drop_unusable_tools
from surogates.tools.builtin import office as office_tools
from surogates.tools.builtin.coordinator import COORDINATOR_IMPLEMENTATION_TOOLS
from surogates.tools.registry import ToolRegistry
from surogates.tools.router import TOOL_LOCATIONS, ToolLocation


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}


def test_only_chats_with_a_file_pane_get_them():
    names = ["office_read", "office_edit", "web_search"]
    kept = drop_unusable_tools([_schema(n) for n in names], has_kbs=True, has_channel=False, makes_routines=False, has_file_pane=False)
    assert [s["function"]["name"] for s in kept] == ["web_search"]
    kept = drop_unusable_tools([_schema(n) for n in names], has_kbs=True, has_channel=False, makes_routines=False)
    assert [s["function"]["name"] for s in kept] == names


def test_a_file_pane_is_a_web_chat_or_a_projects_thread_in_the_cloud():
    from surogates.harness.tool_schemas import has_file_pane
    from surogates.workstreams import THREAD

    device = {"execution": {"kind": "device", "device_id": "00000000-0000-0000-0000-000000000001"}}
    assert has_file_pane(SimpleNamespace(channel="web", config={})) is True
    assert has_file_pane(SimpleNamespace(channel="worker", config={"workstream_role": THREAD})) is True
    assert has_file_pane(SimpleNamespace(channel="studio", config={})) is True
    assert has_file_pane(SimpleNamespace(channel="api", config={})) is False
    assert has_file_pane(SimpleNamespace(channel="voice", config={})) is False
    assert has_file_pane(SimpleNamespace(channel="website", config={})) is False
    assert has_file_pane(SimpleNamespace(channel="web", config=device)) is False


def test_strict_coordinators_do_not_get_them_and_they_run_in_the_worker():
    assert {"office_read", "office_edit"} <= COORDINATOR_IMPLEMENTATION_TOOLS
    assert TOOL_LOCATIONS["office_read"] is ToolLocation.HARNESS
    assert TOOL_LOCATIONS["office_edit"] is ToolLocation.HARNESS


def test_registered_only_where_the_agent_edits_live(monkeypatch):
    registry = ToolRegistry()
    monkeypatch.setattr(office_tools, "load_settings", lambda: SimpleNamespace(office=SimpleNamespace(agent_editing=False)))
    office_tools.register(registry)
    assert "office_read" not in registry.tool_names
    monkeypatch.setattr(office_tools, "load_settings", lambda: SimpleNamespace(office=SimpleNamespace(agent_editing=True)))
    office_tools.register(registry)
    assert {"office_read", "office_edit"} <= registry.tool_names
