"""Where the office tools are offered."""

from types import SimpleNamespace

from surogates.harness.tool_schemas import drop_unusable_tools
from surogates.tools.builtin import office as office_tools
from surogates.tools.registry import ToolRegistry


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}


def test_only_chats_with_a_file_pane_get_them():
    names = ["office_read", "office_edit", "web_search"]
    kept = drop_unusable_tools([_schema(n) for n in names], has_kbs=True, has_channel=False, makes_routines=False, has_file_pane=False)
    assert [s["function"]["name"] for s in kept] == ["web_search"]
    kept = drop_unusable_tools([_schema(n) for n in names], has_kbs=True, has_channel=False, makes_routines=False)
    assert [s["function"]["name"] for s in kept] == names


def test_a_file_pane_is_a_web_chat_or_a_projects_thread():
    from surogates.harness.tool_schemas import has_file_pane
    from surogates.workstreams import THREAD

    device = {"execution": {"kind": "device", "device_id": "00000000-0000-0000-0000-000000000001"}}
    assert has_file_pane(SimpleNamespace(channel="web", config={})) is True
    assert has_file_pane(SimpleNamespace(channel="worker", config={"workstream_role": THREAD})) is True
    assert has_file_pane(SimpleNamespace(channel="studio", config={})) is True
    assert has_file_pane(SimpleNamespace(channel="api", config={})) is False
    assert has_file_pane(SimpleNamespace(channel="voice", config={})) is False
    assert has_file_pane(SimpleNamespace(channel="website", config={})) is False
    # An app that shows the session's workspace and editor beside its own chat says so when it creates the session.
    assert has_file_pane(SimpleNamespace(channel="api", config={"file_pane": True})) is True
    assert has_file_pane(SimpleNamespace(channel="api", config={"file_pane": "yes"})) is False
    assert has_file_pane(SimpleNamespace(channel="voice", config={"file_pane": True})) is False
    assert has_file_pane(SimpleNamespace(channel="website", config={"file_pane": True})) is False
    # Surogate Desktop shows the pane beside a chat on a folder of the computer too.
    assert has_file_pane(SimpleNamespace(channel="web", config=device)) is True


def test_registered_only_where_the_agent_edits_live(monkeypatch):
    registry = ToolRegistry()
    monkeypatch.setattr(office_tools, "load_settings", lambda: SimpleNamespace(office=SimpleNamespace(agent_editing=False)))
    office_tools.register(registry)
    assert "office_read" not in registry.tool_names
    monkeypatch.setattr(office_tools, "load_settings", lambda: SimpleNamespace(office=SimpleNamespace(agent_editing=True)))
    office_tools.register(registry)
    assert {"office_read", "office_edit"} <= registry.tool_names


_DEVICE = {"execution": {"kind": "device", "device_id": "00000000-0000-0000-0000-000000000001"}}
#: (channel, config, sent the office tools)
_SESSIONS = [
    ("web", {}, True),
    ("studio", {}, True),
    ("worker", {"workstream_role": "thread"}, True),
    ("api", {}, False),
    ("api", {"file_pane": True}, True),
    ("voice", {}, False),
    # A project's master and a /mission chat are strict coordinators: they give the work out.
    ("web", {"coordinator": True, "strict_coordinator": True, "workstream_role": "coordinator",
             "workstream_id": "00000000-0000-0000-0000-000000000002"}, False),
    ("web", {"coordinator": True, "strict_coordinator": True, "workstream_role": "coordinator",
             "workstream_id": "00000000-0000-0000-0000-000000000002", **_DEVICE}, False),
    ("web", {"coordinator": True, "strict_coordinator": True}, False),
    # A coordinator that is not strict keeps every tool, whatever it excludes.
    ("web", {"coordinator": True, "excluded_tools": ["office_edit"]}, True),
    ("web", {"allowed_tools": ["read_file", "office_read", "office_edit"]}, True),
    ("web", {"allowed_tools": ["read_file", "office_read"]}, False),
    ("web", {"allowed_tools": []}, True),
    ("web", {"excluded_tools": ["office_edit"]}, False),
    ("web", {"excluded_tools": ["web_search"]}, True),
]


def _chat(channel, config):
    from datetime import datetime, timezone
    from uuid import uuid4

    from surogates.session.models import Session

    now = datetime.now(timezone.utc)
    return Session(id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="writer", channel=channel, status="active",
                   config=config, created_at=now, updated_at=now)


def test_a_session_is_offered_the_office_tools_by_the_rules_that_take_them_away():
    from surogates.office.guard import office_tools_offered

    for channel, config, offered in _SESSIONS:
        assert office_tools_offered(_chat(channel, dict(config))) is offered, (channel, config)


def test_the_rule_agrees_with_the_tools_the_worker_sends(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from uuid import UUID

    from surogates.harness.budget import IterationBudget
    from surogates.harness.context import ContextCompressor
    from surogates.harness.loop import AgentHarness
    from surogates.harness.prompt import PromptBuilder
    from surogates.harness.tool_schemas import has_file_pane
    from surogates.office.guard import office_tools_offered
    from surogates.sandbox.pool import SandboxPool
    from surogates.tenant.context import TenantContext

    registry = ToolRegistry()
    monkeypatch.setattr(office_tools, "load_settings", lambda: SimpleNamespace(office=SimpleNamespace(agent_editing=True)))
    office_tools.register(registry)
    tenant = TenantContext(org_id=UUID("00000000-0000-0000-0000-000000000001"), user_id=None, org_config={},
                           user_preferences={}, permissions=frozenset(), asset_root="/tmp/test")
    harness = AgentHarness(session_store=AsyncMock(), tool_registry=registry, llm_client=AsyncMock(), tenant=tenant,
                           worker_id="w", budget=IterationBudget(max_total=10),
                           context_compressor=MagicMock(spec=ContextCompressor), prompt_builder=MagicMock(spec=PromptBuilder),
                           sandbox_pool=MagicMock(spec=SandboxPool), vision_client=None, vision_model="")
    for channel, config, _offered in _SESSIONS:
        session = _chat(channel, dict(config))
        sent = harness._tool_filter_for_session_impl(session)
        worker_sends = has_file_pane(session) and (sent is None or {"office_read", "office_edit"} <= sent)
        assert office_tools_offered(_chat(channel, dict(config))) is worker_sends, (channel, config)
