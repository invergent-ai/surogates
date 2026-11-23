"""A test chat's changes run for real only inside its scratch copy."""

from __future__ import annotations

import pytest

from surogates.simulation import side_effects
from surogates.simulation.scratch import inside_scratch
from surogates.simulation.side_effects import decide, is_simulated

ROUTER = "mcp__tool_router__COMPOSIO_MULTI_EXECUTE_TOOL"
SCOPE = {"drive_id": "D", "root_id": "R1", "root_path": "/Surogate - teste/r1/0-1/Fise", "item_ids": ["F1", "F2"]}
RENAME = {"itemId": "F1", "parentReference": {"id": "R1", "driveId": "D"}, "name": "x.pdf"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    side_effects._decided.clear()


def call(slug, arguments):
    return {"tools": [{"tool_slug": slug, "arguments": arguments}]}


def test_rename_inside_copy_is_real():
    assert inside_scratch("ONE_DRIVE_MOVE_ITEM", RENAME, SCOPE)


def test_move_out_of_copy_is_faked():
    moved = {**RENAME, "parentReference": {"id": "ORIG", "driveId": "D"}}
    assert not inside_scratch("ONE_DRIVE_MOVE_ITEM", moved, SCOPE)


def test_unknown_reference_key_is_faked():
    assert not inside_scratch("ONE_DRIVE_MOVE_ITEM", {**RENAME, "targetFolderId": "ORIG"}, SCOPE)
    assert not inside_scratch("ONE_DRIVE_MOVE_ITEM", {**RENAME, "target": "01TVGB2LABCDEFGHIJKLMN"}, SCOPE)
    assert not inside_scratch("ONE_DRIVE_MOVE_ITEM", {**RENAME, "target": "/a/b"}, SCOPE)


def test_other_drive_is_faked():
    other = {**RENAME, "parentReference": {"id": "R1", "driveId": "D2"}}
    assert not inside_scratch("ONE_DRIVE_MOVE_ITEM", other, SCOPE)
    assert not inside_scratch("ONE_DRIVE_MOVE_ITEM", {**RENAME, "drive_id": "D2"}, SCOPE)


def test_sharepoint_path_outside_root_is_faked():
    base = "/Surogate - teste/r1/0-1/Fise"
    ok = {"server_relative_url": f"{base}/a.pdf", "name": "b.pdf", "drive_id": "D"}
    assert inside_scratch("SHARE_POINT_UPDATE_DRIVE_ITEM", ok, SCOPE)
    for bad in ("/Documente/a.pdf", base + "X/a.pdf", base.lower() + "/a.pdf",
                base + "/../../../../Documente/a.pdf", base + "/%2e%2e/%2e%2e/x", base + "/./a.pdf", base + "\\..\\x"):
        assert not inside_scratch("SHARE_POINT_UPDATE_DRIVE_ITEM", {**ok, "server_relative_url": bad}, SCOPE)


def test_onedrive_folder_path_traversal_is_faked():
    base = SCOPE["root_path"]
    up = {"folder_path": base + "/sub", "name": "a", "drive_id": "D"}
    assert inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", up, SCOPE)
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {**up, "folder_path": base + "/../x"}, SCOPE)


def test_path_without_drive_is_faked():
    up = {"folder_path": SCOPE["root_path"] + "/sub", "name": "a"}
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", up, SCOPE)


def test_a_site_name_is_faked_whatever_it_holds():
    for site in ("R1", "F1", SCOPE["root_path"], "", None, 1):
        assert not inside_scratch("SHARE_POINT_UPDATE_DRIVE_ITEM", {**RENAME, "site_name": site}, SCOPE)


def test_known_ids_and_paths_mix_only_with_a_drive():
    up = {"folder_item_id": "F2", "folder_path": SCOPE["root_path"] + "/sub", "name": "a"}
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", up, SCOPE)
    assert inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {**up, "drive_id": "D"}, SCOPE)
    assert inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {"folder_item_id": "F2", "name": "a"}, SCOPE)
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {"drive_id": "D", "name": "a"}, SCOPE)


def test_not_a_file_action_or_no_scope_is_faked():
    assert not inside_scratch("OUTLOOK_SEND_EMAIL", RENAME, SCOPE)
    assert not inside_scratch("ONE_DRIVE_MOVE_ITEM", RENAME, {})
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {"name": "a.pdf"}, SCOPE)


@pytest.mark.asyncio
async def test_rename_inside_copy_runs_through_decide():
    assert not await decide(ROUTER, None, llm=None, args=call("ONE_DRIVE_MOVE_ITEM", RENAME), scratch=SCOPE)
    assert await decide(ROUTER, None, llm=None, args=call("ONE_DRIVE_MOVE_ITEM", RENAME), scratch=None)


@pytest.mark.asyncio
async def test_email_is_always_faked():
    assert await decide(ROUTER, None, llm=None, args=call("OUTLOOK_SEND_EMAIL", RENAME), scratch=SCOPE)


@pytest.mark.asyncio
async def test_workbench_is_faked_in_test_mode():
    assert await decide("mcp__tool_router__REMOTE_WORKBENCH", None, llm=None, args={}, scratch=SCOPE)


@pytest.mark.asyncio
async def test_execute_mixed_batch_is_faked():
    args = {"tools": [
        {"tool_slug": "ONE_DRIVE_MOVE_ITEM", "arguments": RENAME},
        {"tool_slug": "ONE_DRIVE_DELETE_ITEM", "arguments": {"item_id": "FOREIGN"}},
        {"tool_slug": "ONE_DRIVE_LIST_ITEMS", "arguments": {}},
    ]}
    assert await decide(ROUTER, None, llm=None, args=args, scratch=SCOPE)
    args["tools"].pop(1)
    assert not await decide(ROUTER, None, llm=None, args=args, scratch=SCOPE)


def test_test_mode_counts_as_simulated():
    assert is_simulated({"test_mode": {"scratch": None}})


@pytest.mark.asyncio
async def test_junk_batch_entry_is_faked_not_a_crash():
    good = {"tool_slug": "ONE_DRIVE_DELETE_ITEM", "arguments": {"item_id": "F1"}}
    assert not await decide(ROUTER, None, llm=None, args={"tools": [good]}, scratch=SCOPE)
    for junk in ("junk", {"arguments": {}}, {"tool_slug": "ONE_DRIVE_DELETE_ITEM", "arguments": "x"}):
        assert await decide(ROUTER, None, llm=None, args={"tools": [good, junk]}, scratch=SCOPE)


# --- the agent's own platform-state tools ------------------------------------


async def _run_builtin(name: str, config: dict, handler, arguments: str = "{}"):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from surogates.harness.tool_exec import execute_single_tool
    from surogates.session.events import EventType
    from surogates.tools.registry import ToolRegistry, ToolSchema

    registry = ToolRegistry()
    registry.register(
        name,
        ToolSchema(name=name, description=name, parameters={"type": "object", "properties": {}}),
        handler=handler,
        toolset="core",
    )
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=[1, 2])
    store.advance_harness_cursor = AsyncMock()
    session = SimpleNamespace(id=uuid4(), config={"workspace_path": "", **config}, agent_id="a")
    result = await execute_single_tool(
        {"id": "c1", "function": {"name": name, "arguments": arguments}},
        session=session,
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store,
        tools=registry,
        tenant=SimpleNamespace(asset_root="/tmp/test"),
    )
    emitted = store.emit_event.await_args_list[-1].args
    assert emitted[1] == EventType.TOOL_RESULT
    return json.loads(result["content"]) if result["content"].startswith("{") else result["content"], emitted[2]


def _state_tool_names():
    from surogates.simulation.scratch import STATE_TOOLS

    return sorted(STATE_TOOLS)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _state_tool_names())
async def test_state_tools_are_faked_in_test_mode(name):
    from unittest.mock import AsyncMock

    handler = AsyncMock(return_value='{"saved": true}')
    result, event = await _run_builtin(name, {"test_mode": {"scratch": None}}, handler)
    handler.assert_not_awaited()
    assert result == {"ok": True}
    assert event["simulated"]["would_have"].startswith(f"would have called {name}")


@pytest.mark.asyncio
async def test_state_tools_run_for_real_outside_test_mode_and_reads_stay_real():
    from unittest.mock import AsyncMock

    from surogates.simulation.scratch import STATE_TOOLS

    handler = AsyncMock(return_value='{"saved": true}')
    _, event = await _run_builtin("memory", {}, handler)
    handler.assert_awaited_once()
    assert "simulated" not in event
    for read in ("kb_search_pages", "cron_list", "skill_view", "read_file", "ask_user_question", "write_file"):
        assert read not in STATE_TOOLS
        handler = AsyncMock(return_value='{"ok": 1}')
        await _run_builtin(read, {"test_mode": {"scratch": None}}, handler)
        handler.assert_awaited_once()


def test_experts_of_a_test_chat_get_no_state_tools(tmp_path):
    import contextvars

    from surogates.simulation.scratch import STATE_TOOLS
    from surogates.tools.builtin import expert_loop as el
    from tests.test_expert_tool_scope import LocalBackend, MediaGenConfig, _harness, _session, _StepStore

    session = _session("web")
    harness = _harness(_StepStore(session, LocalBackend(str(tmp_path))), media_gen=MediaGenConfig(), gate=None)
    usable = harness._tool_usability(session, harness._tools.tool_names)

    def scoped(config):
        session.config = config
        harness._withhold_from_experts(usable, None, session)
        return el.expert_scope.get().withheld

    assert STATE_TOOLS <= contextvars.copy_context().run(scoped, {"test_mode": {"scratch": None}})
    assert "memory" not in contextvars.copy_context().run(scoped, {})


# --- final review fixes ------------------------------------------------------


@pytest.mark.parametrize("seg", ["..", ".", ".. ", "...", " .", ". ."])
def test_near_dot_path_segments_are_faked(seg):
    base = SCOPE["root_path"]
    up = {"folder_path": f"{base}/{seg}/x", "name": "a", "drive_id": "D"}
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", up, SCOPE)


@pytest.mark.parametrize("seg", [
    "\u00a0..\u00a0", "\u2024\u2024", ".\t.", "\u3000", "..\u200b", "\ufeff..",
    "a\uff0f..", "a\u2215b", "a\u2044b", "x\u200cy", "x\u200dy",
])
def test_lookalike_path_segments_are_faked(seg):
    up = {"folder_path": f"{SCOPE['root_path']}/{seg}/x", "name": "a", "drive_id": "D"}
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", up, SCOPE)


@pytest.mark.parametrize("bad", ["a\u200bb.pdf", "\ufeff", "x\u200c.pdf", "x\u200d.pdf", ".\u200b."])
def test_a_name_with_a_zero_width_character_is_faked(bad):
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {**_UP, "name": bad}, SCOPE)


def test_a_romanian_path_and_name_still_pass():
    up = {"folder_path": SCOPE["root_path"] + "/Situații/Țară", "name": "ședință.pdf", "drive_id": "D"}
    assert inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", up, SCOPE)


def test_a_dotted_name_segment_still_passes():
    up = {"folder_path": SCOPE["root_path"] + "/.config/a..b", "name": "a", "drive_id": "D"}
    assert inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", up, SCOPE)


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", [
    "GOOGLECALENDAR_ACCEPT_EVENT", "OUTLOOK_DECLINE_EVENT", "ONE_DRIVE_COPY_ITEM",
    "ONE_DRIVE_RENAME_ITEM", "ONE_DRIVE_RESTORE_ITEM", "COMPOSIO_MANAGE_CONNECTIONS",
])
async def test_unknown_inner_slug_is_faked_in_test_mode_even_if_the_llm_said_read(slug):
    from unittest.mock import AsyncMock

    from types import SimpleNamespace

    from surogates.harness.tool_exec import _simulation_changes

    reads = AsyncMock(return_value=False)
    # A non-test simulation asked the LLM first and cached "read" for this slug.
    assert not await decide(ROUTER, None, llm=reads, args=call(slug, {}))
    reads.assert_awaited_once()
    session = SimpleNamespace(config={"test_mode": {"scratch": SCOPE}})
    tools = SimpleNamespace(get=lambda _n: None)
    assert await _simulation_changes(ROUTER, call(slug, {}), session, tools, object(), "m") is True
    session.config = {"test_mode": {"scratch": None}}
    assert await _simulation_changes(ROUTER, call(slug, {}), session, tools, object(), "m") is True
    assert await decide(ROUTER, None, llm=reads, args=call(slug, {}), scratch=SCOPE)
    reads.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_is_a_read_in_test_mode():
    assert not await decide(ROUTER, None, llm=None, args=call("ONE_DRIVE_DOWNLOAD_FILE", {"item_id": "X"}), scratch=SCOPE)


@pytest.mark.asyncio
async def test_a_non_test_simulation_still_asks_the_llm():
    from unittest.mock import AsyncMock

    reads = AsyncMock(return_value=False)
    assert not await decide(ROUTER, None, llm=reads, args=call("GOOGLECALENDAR_ACCEPT_EVENT", {}))
    reads.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_ssh_access_in_a_test_chat(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from surogates.harness import tool_exec
    from surogates.ssh_access import resolve

    bundle = SimpleNamespace(private_key="KEY", passphrase=None)
    monkeypatch.setattr(resolve, "resolve_ssh_key", AsyncMock(return_value=bundle))
    target = {"alias": "srv", "host": "srv.example.com", "key_name": "k", "host_key": "ssh-ed25519 AAAA"}
    config = {"ssh_targets": [target], "agent_service_account_id": "00000000-0000-0000-0000-000000000001"}

    def spec():
        return SimpleNamespace(env={}, ssh_targets=None, ssh_key_material=None)

    real = spec()
    await tool_exec._apply_ssh_access(SimpleNamespace(config=config), SimpleNamespace(org_id=None), real, object())
    assert real.ssh_key_material  # the setup above does reach a server outside a test chat
    test = spec()
    await tool_exec._apply_ssh_access(
        SimpleNamespace(config={**config, "test_mode": {"scratch": None}}), SimpleNamespace(org_id=None), test, object(),
    )
    assert test.ssh_targets is None and test.ssh_key_material is None and test.env == {}


@pytest.mark.parametrize("channel", ["web", "api"])
def test_a_client_cannot_put_a_session_in_test_mode(channel):
    from surogates.api.routes.sessions import apply_eval_isolation

    config = apply_eval_isolation(
        {"test_mode": {"scratch": None}, "pipeline_metadata": {"skillopt_run_id": "r1"}, "x": 1}, channel=channel,
    )
    assert config == {"x": 1}


# --- code-review fixes: a name is one path segment ---------------------------

_UP = {"folder_path": SCOPE["root_path"], "drive_id": "D"}
_BAD_NAMES = ["../../../../Documente/a.pdf", "a/b.pdf", "..\\..\\x.pdf", "%2e%2e", "", "..", ".", " . ", None, 1]


@pytest.mark.parametrize("key", ["name", "file_name"])
@pytest.mark.parametrize("bad", _BAD_NAMES)
def test_a_name_that_is_not_one_segment_is_faked(key, bad):
    assert inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {**_UP, key: "a.pdf"}, SCOPE)
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {**_UP, key: bad}, SCOPE)


# U+FF0F fullwidth slash, U+2215 division slash, NBSP-padded dots, U+2024 one dot leader.
_LOOKALIKE_NAMES = ["a\uff0fb.pdf", "a\u2215b.pdf", "\u00a0..\u00a0", "\u2024\u2024", ".\t.", "\u3000"]


@pytest.mark.parametrize("key", ["name", "file_name"])
@pytest.mark.parametrize("bad", _LOOKALIKE_NAMES)
def test_a_lookalike_name_is_faked(key, bad):
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {**_UP, key: bad}, SCOPE)


def test_composio_nested_upload_inside_the_copy_is_real():
    up = {**_UP, "file": {"name": "a.pdf", "mimetype": "application/pdf", "s3key": "k/a.pdf"}}
    assert inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", up, SCOPE)


@pytest.mark.parametrize("bad", _BAD_NAMES)
def test_composio_nested_upload_with_a_bad_name_is_faked(bad):
    up = {**_UP, "file": {"name": bad, "mimetype": "application/pdf", "s3key": "k/a.pdf"}}
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", up, SCOPE)


def test_composio_nested_upload_with_an_unknown_key_is_faked():
    up = {**_UP, "file": {"name": "a.pdf", "mimetype": "application/pdf", "s3key": "k"}, "user_id": "u1"}
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", up, SCOPE)
    up = {**_UP, "file": {"name": "a.pdf", "mimetype": "application/pdf", "s3key": "k", "parent": "ORIG"}}
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", up, SCOPE)


def test_a_test_chats_boundary_is_memory_only():
    from types import SimpleNamespace

    from surogates.channels.memory_boundary import SKILLOPT_BOUNDARY_PREFIX, session_memory_boundary
    from surogates.storage.tenant import workspace_boundary

    boundary = f"{SKILLOPT_BOUNDARY_PREFIX}8f1c2d3e-4b5a-4c6d-8e7f-901234567890:s1"
    session = SimpleNamespace(id="s1", channel="api", config={"memory_boundary": boundary})
    assert session_memory_boundary(session) == boundary  # memory stays partitioned per run and chat
    assert workspace_boundary(session) is None  # the workspace stays the per-session one


def test_every_builtin_tool_is_classified_once(monkeypatch):
    from types import SimpleNamespace

    from surogates.simulation import scratch
    from surogates.tools.builtin import office
    from surogates.tools.registry import ToolRegistry
    from surogates.tools.runtime import ToolRuntime

    # office_* register only with live editing on; build the full set.
    monkeypatch.setattr(office, "load_settings", lambda: SimpleNamespace(office=SimpleNamespace(agent_editing=True)))
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    registered = set(registry.tool_names)
    local = getattr(scratch, "CHAT_LOCAL_OR_READ_TOOLS", frozenset())
    assert {"memory", "office_edit", "terminal"} <= registered  # the registry really is the full one
    assert not scratch.STATE_TOOLS & local, "a tool is in both STATE_TOOLS and CHAT_LOCAL_OR_READ_TOOLS"
    assert registered - scratch.STATE_TOOLS - local == set(), "new builtin: classify it in simulation/scratch.py"
    assert (scratch.STATE_TOOLS | local) - registered == set(), "a classified tool is no longer registered"


def _call(slug, args=None):
    return {"tools": [{"tool_slug": slug, "arguments": args or {}}]}


def _cfg(**tm):
    return {"test_mode": {"scratch": SCOPE, **tm}}


def _advertised(tool: str) -> str:
    """The name the proxy advertises for a router tool: debranded, as pool.py builds it."""
    from surogates.mcp_proxy.loader import COMPOSIO_SERVER_NAME, debrand_composio_text
    from surogates.tools.mcp.names import fit_tool_name

    return fit_tool_name(debrand_composio_text(COMPOSIO_SERVER_NAME.replace("-", "_")), debrand_composio_text(tool))


def test_unchosen_composio_call_rules():
    from surogates.simulation.side_effects import unchosen_composio_call as u

    execute = _advertised("COMPOSIO_MULTI_EXECUTE_TOOL")
    assert execute == "mcp__tool_router__MULTI_EXECUTE_TOOL"
    cfg = ["one_drive"]
    assert u(execute, _call("ONE_DRIVE_LIST_FOLDER_CHILDREN"), cfg) is None
    assert u(execute, _call("OUTLOOK_LIST_MESSAGES"), cfg) == "OUTLOOK_LIST_MESSAGES"
    mixed = {"tools": [{"tool_slug": "ONE_DRIVE_LIST_FOLDER_CHILDREN"}, {"tool_slug": "OUTLOOK_LIST_MESSAGES"}]}
    assert u(execute, mixed, cfg) == "OUTLOOK_LIST_MESSAGES"
    assert u(execute, {"tools": "x"}, cfg) is not None
    assert u("mcp__tool_router__OUTLOOK_SEND_EMAIL", {}, cfg) == "OUTLOOK_SEND_EMAIL"
    assert u(execute, _call("ONE_DRIVE_LIST_FOLDER_CHILDREN"), []) is not None
    assert u(execute, _call("OUTLOOK_LIST_MESSAGES"), None) is None
    for meta in ("SEARCH_TOOLS", "GET_TOOL_SCHEMAS", "MANAGE_CONNECTIONS", "REMOTE_WORKBENCH", "REMOTE_BASH_TOOL"):
        for branded in (False, True):
            name = _advertised(("COMPOSIO_" if branded else "") + meta)
            assert u(name, {}, cfg) is None and u(name, {}, []) is None
    assert u("mcp__tool_router__COMPOSIO_SEARCH_TOOLS", {}, cfg) is None


@pytest.mark.asyncio
async def test_decide_still_fakes_the_debranded_workbench_and_bash():
    for tool in ("REMOTE_WORKBENCH", "REMOTE_BASH_TOOL"):
        assert await decide(_advertised(tool), None, llm=None, args={}, scratch=SCOPE)


@pytest.mark.asyncio
async def test_dispatch_refuses_an_unchosen_account_without_calling_it():
    import json
    from unittest.mock import AsyncMock

    handler = AsyncMock(return_value="{}")
    args = json.dumps(_call("OUTLOOK_LIST_MESSAGES"))
    result, event = await _run_builtin(ROUTER, _cfg(toolkits=["one_drive"]), handler, args)
    handler.assert_not_awaited()
    assert result == {"error": "No connected account for OUTLOOK_LIST_MESSAGES in this chat."}
    assert "simulated" not in event
    await _run_builtin(ROUTER, _cfg(toolkits=["one_drive"]), handler, json.dumps(_call("ONE_DRIVE_LIST_FOLDER_CHILDREN")))
    handler.assert_awaited_once()
