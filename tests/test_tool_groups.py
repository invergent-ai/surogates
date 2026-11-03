"""On-demand tool groups: held-back schemas, load_tools, and what a session shows."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from surogates.harness.prompt import PromptBuilder
from surogates.harness.prompt_profile import SUROGATE_PROFILE
from surogates.harness.tool_groups import (
    LOAD_TOOLS,
    defer_tool_groups,
    loaded_by,
    loaded_in_history,
    refresh_load_tools,
    tool_groups,
)
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.builtin.tool_groups import _load_tools_handler
from surogates.tools.loader import ResourceLoader
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime

SKILLS = Path(__file__).parents[1] / "skills"
STUDIO = [
    "read_file", "write_file", "terminal", "web_search", "web_extract", "memory", "todo",
    "skill_view", "skill_manage", "ask_user_question", "create_artifact", "generate_image",
    "browser_navigate", "browser_click", "browser_type", "cron_create", "cron_list", "github",
    LOAD_TOOLS,
]


def _schemas(names=STUDIO) -> list[dict]:
    reg = ToolRegistry()
    ToolRuntime(reg).register_builtins()
    return reg.get_schemas(set(names))


def _names(schemas) -> set[str]:
    return {s["function"]["name"] for s in schemas}


def test_load_tools_is_registered():
    reg = ToolRegistry()
    ToolRuntime(reg).register_builtins()
    assert reg.has(LOAD_TOOLS)


def test_groups_are_held_back_and_listed_on_load_tools():
    groups = tool_groups(SUROGATE_PROFILE)
    shown, deferred = defer_tool_groups(_schemas(), groups)
    names = _names(shown)
    assert not {n for n in names if n.startswith("browser_")}
    assert not names & {"cron_create", "cron_list", "github", "skill_manage"}
    assert {"read_file", "terminal", "web_search", "skill_view", "generate_image", LOAD_TOOLS} <= names
    # Held back, still callable.
    assert {"browser_navigate", "cron_create", "github", "skill_manage"} <= deferred.names
    load = next(s for s in shown if s["function"]["name"] == LOAD_TOOLS)["function"]
    assert set(load["parameters"]["properties"]["groups"]["items"]["enum"]) == set(deferred.held)
    assert "- browser:" in load["description"] and "- scheduling:" in load["description"]


def test_no_groups_or_no_load_tools_shows_everything_and_drops_load_tools():
    everything = _schemas()
    shown, deferred = defer_tool_groups(everything, ())
    assert LOAD_TOOLS not in _names(shown) and not deferred.held
    assert _names(shown) == _names(everything) - {LOAD_TOOLS}
    # An explicit allow-list without load_tools keeps every schema shown.
    without = _schemas([n for n in STUDIO if n != LOAD_TOOLS])
    shown, deferred = defer_tool_groups(without, tool_groups(SUROGATE_PROFILE))
    assert _names(shown) == _names(without) and not deferred.held


def test_loaded_groups_stay_shown_and_load_tools_goes_when_nothing_is_left():
    groups = tool_groups(SUROGATE_PROFILE)
    shown, deferred = defer_tool_groups(_schemas(), groups, loaded={"browser"})
    assert "browser_navigate" in _names(shown) and "browser" not in deferred.held
    released = deferred.take(list(deferred.held))
    shown = refresh_load_tools([*shown, *released], deferred)
    assert LOAD_TOOLS not in _names(shown)
    assert {"cron_create", "github", "generate_image"} <= _names(shown)


def test_loading_from_results_direct_calls_and_history():
    groups = tool_groups(SUROGATE_PROFILE)
    calls = [{"id": "c1", "function": {"name": LOAD_TOOLS, "arguments": '{"groups":["browser"]}'}},
             {"id": "c2", "function": {"name": "cron_create", "arguments": "{}"}}]
    results = [{"role": "tool", "tool_call_id": "c1", "content": json.dumps({"success": True, "loaded": ["browser"]})},
               {"role": "tool", "tool_call_id": "c2", "content": "{}"}]
    assert loaded_by(calls, results) == {"browser"}
    _, deferred = defer_tool_groups(_schemas(), groups)
    assert deferred.groups_of_calls(["cron_create", "read_file"]) == {"scheduling"}
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "", "tool_calls": calls}, *results]
    assert loaded_in_history(history, groups) == {"browser", "scheduling"}
    failed = [{"role": "tool", "tool_call_id": "c1", "content": json.dumps({"success": False})}]
    assert loaded_by(calls[:1], failed) == set()


async def test_load_tools_handler():
    ok = json.loads(await _load_tools_handler({"groups": ["browser", "nope"]}))
    assert ok["success"] and ok["loaded"] == ["browser"] and ok["unknown"] == ["nope"]
    assert "@eN" in ok["guide"]["browser"]
    bad = json.loads(await _load_tools_handler({"groups": ["nope"]}))
    assert not bad["success"] and "browser" in bad["groups"]


def _prompt(tools: set[str]) -> str:
    now = datetime.now(timezone.utc)
    session = Session(id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="a", channel="web", status="active",
                      model="surogate/Surogate3.7-35B-A3B", config={}, created_at=now, updated_at=now)
    tenant = TenantContext(org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
                           permissions=frozenset(), asset_root="/tmp/test")
    skills = ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")
    return PromptBuilder(tenant, skills=skills, session=session, available_tools=tools).build()


def test_browser_guidance_moves_into_the_group_when_held_back(monkeypatch):
    monkeypatch.delenv("SUROGATES_PROMPT_PROFILES", raising=False)
    base = {"read_file", "skill_view", "browser_navigate", "browser_click"}
    assert "**Browser.**" in _prompt(base)
    assert "**Browser.**" not in _prompt(base | {LOAD_TOOLS})
