"""A skill that needs or names a tool the session is not sent is hidden: from the skill index, skills_list and skill_view.

HO-X run (surogates 15af7f4e0, template eog-lean-govoff-fixed, 14 tools in ``excluded_tools``): the model called
``read_file`` and ``terminal`` 26 times in 10 of 763 sessions, and each call was refused "Unknown tool".  It read the
names in skill texts: ``skills_list`` gave pdf-reading's "Read a PDF when read_file fails ...", ``skill_view`` gave
office's, xlsx's, pdf's, executing-plans' bodies.  The prompt's index hid a skill only by its ``requires_tools``,
and the skill tools did not even do that.  On master, 10 of the 17 skills skills_list gave such a session named one.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import surogates.harness.loop as loop_module
from surogates.harness.prompt import PromptBuilder
from surogates.session.events import EventType
from surogates.storage.backend import LocalBackend
from surogates.tools.builtin.skills import _skill_view_handler, _skills_list_handler
from surogates.tools.skill_visibility import SkillScope, needs_unavailable_tool, skill_scope, tools_named
from tests.test_case_step_tools import _StepStore
from tests.test_case_tool_gate import _registry
from tests.test_expert_tool_scope import _harness as _turn_harness
from tests.test_expert_tool_scope import _session
from tests.test_fallback_skills_hidden import TENANT, catalog, the_worker_reads_the_repositorys_skills  # noqa: F401
from tests.test_steer_loop import _final_response

# The HO-X template's excluded_tools (eog-lean's 14 denied tools); its sessions are eval rows.
HOX_EXCLUDED = [
    "terminal", "process", "read_file", "write_file", "patch", "search_files", "list_files", "web_search",
    "web_extract", "vision_analyze", "generate_image", "generate_video", "github", "run_coding_agent",
]
HOX = {"excluded_tools": HOX_EXCLUDED, "eval_partition_id": "hox-1"}

# Where a visible skill's text has one of these words as prose, not as the tool: read, and allowed, one by one.
PROSE = {
    ("schedule", "terminal"),  # "in the sandbox or terminal: none is there"
    ("doc-coauthoring", "process"), ("writing-plans", "process"), ("writing-skills", "process"),
    ("dispatching-parallel-agents", "process"), ("consolidate-memory", "process"), ("import-memory", "process"),
}


def _skill(name: str):
    return next(skill for skill in catalog() if skill.name == name)


async def _session_view(config: dict, *, names=None) -> tuple[AgentHarness, set[str], list[dict], dict[str, dict], str]:
    """For a session with *config* (its prompt builder given every registry tool, not the worker's narrowed set):
    what it may not be sent, its prompt, its skills_list and skill_view of every catalog skill, in the turn's scope."""
    harness, session = _harness(config)
    prompt = await harness._build_system_prompt(session)
    harness._scope_skills(session)
    on = {"tenant": TENANT, "session_config": config, "channel": session.channel}  # as the prompt's session
    listed = json.loads(await _skills_list_handler({}, **on))["skills"]
    views = {skill.name: json.loads(await _skill_view_handler({"name": skill.name}, **on)) for skill in catalog()}
    return harness, set(harness._prompt.unavailable_tools), listed, views, prompt


def _harness(config: dict):
    from tests.test_case_tool_gate import _chat
    from tests.test_prompt_honours_excluded_tools import _harness as harness_for

    session = _chat(config)
    registry = _registry()
    builder = PromptBuilder(
        TENANT, skills=catalog(), session=session, available_tools=set(registry.tool_names),
    )
    harness = harness_for(session, builder)
    harness._tools = registry
    return harness, session


def _indexed(prompt: str) -> set[str]:
    index = prompt[prompt.index("<available_skills>"):prompt.index("</available_skills>")]
    return {skill.name for skill in catalog() if f"    - {skill.name}: " in index or index.endswith(f"    - {skill.name}")}


AgentHarness = object  # for the annotation above


async def test_a_session_with_excluded_tools_gets_no_skill_text_naming_one():
    _, unavailable, listed, views, prompt = await _session_view(HOX)
    assert set(HOX_EXCLUDED) <= unavailable
    shown = {entry["name"] for entry in listed}
    for name in shown:
        skill, view = _skill(name), views[name]
        text = " ".join(filter(None, (skill.description, skill.trigger, view.get("content"), view.get("description"))))
        allowed = set(skill.optional_tools or ()) | set(skill.fallback_for_tools or ())
        named = {tool for tool in HOX_EXCLUDED if re.search(rf"\b{re.escape(tool)}\b", text)} - allowed
        assert {(name, tool) for tool in named} <= PROSE, name
    # Master listed pdf-reading, pdf, xlsx, docx, pptx, pdf-forms, office, executing-plans, maps, brainstorming.
    assert not {"pdf-reading", "pdf", "xlsx", "executing-plans", "maps", "brainstorming"} & shown
    assert {"schedule", "writing-plans", "doc-coauthoring"} <= shown  # schedule names ask_user_question as optional


async def test_the_index_and_the_skill_tools_show_the_same_skills():
    for config in (HOX, {}, {"excluded_tools": ["office_edit"]}, {"allowed_tools": ["skills_list", "skill_view", "web_search"]}):
        _, _, listed, _, prompt = await _session_view(config)
        assert _indexed(prompt) == {entry["name"] for entry in listed if entry.get("type", "skill") == "skill"}, config


async def test_a_hidden_skill_viewed_by_name_is_refused_without_naming_a_tool():
    _, _, _, views, _ = await _session_view(HOX)
    assert views["pdf-reading"] == {
        "success": False,
        "error": "The pdf-reading skill needs tools this session does not have. Do the work with the tools you have.",
    }


async def test_a_chat_with_every_tool_keeps_every_skill_it_had():
    _, _, listed, views, _ = await _session_view({})
    shown = {entry["name"] for entry in listed}
    assert {"pdf-reading", "pdf", "executing-plans", "maps", "brainstorming", "schedule"} <= shown
    assert views["pdf-reading"].get("success") is not False


async def test_a_tool_named_only_where_the_chat_has_it_hides_nothing():
    """pdf and pdf-reading send a form to office_edit "where the chat has it": a chat without office_edit keeps them."""
    _, _, listed, _, _ = await _session_view({"excluded_tools": ["office_edit"]})
    assert {"pdf", "pdf-reading"} <= {entry["name"] for entry in listed}


async def test_a_tool_the_policy_refuses_hides_the_skills_naming_it():
    """With governance on, eog-lean's denials come from the policy, not excluded_tools: the same skills are hidden."""
    from surogates.governance.policy import GovernanceGate

    harness, session = _harness({})
    harness._governance_gate = GovernanceGate(denied_tools=set(HOX_EXCLUDED))
    await harness._build_system_prompt(session)
    harness._scope_skills(session)
    assert set(HOX_EXCLUDED) <= harness._prompt.unavailable_tools
    assert {"pdf-reading", "executing-plans"} <= skill_scope.get().hidden


async def test_an_allowed_tools_list_hides_the_skills_naming_what_it_leaves_out():
    _, _, listed, _, _ = await _session_view({"allowed_tools": ["skills_list", "skill_view", "web_search"]})
    shown = {entry["name"] for entry in listed}
    assert "pdf-reading" not in shown and "maps" in shown  # maps names web_search, which this session is sent


async def test_through_the_api_the_list_leaves_them_out_too():
    harness, session = _harness(HOX)
    harness._scope_skills(session)
    api = SimpleNamespace(list_skills=AsyncMock(return_value="{}"), view_skill=AsyncMock(return_value="{}"))
    await _skills_list_handler({}, api_client=api, session_config=HOX)
    assert {"pdf-reading", "pdf", "xlsx", "executing-plans"} <= api.list_skills.await_args.kwargs["leave_out"]
    answer = json.loads(await _skill_view_handler({"name": "pdf-reading", "file_path": "references/x.md"}, api_client=api, session_config={}))
    assert answer["success"] is False
    api.view_skill.assert_not_awaited()  # nothing of the skill is read or staged, a linked file neither


async def test_a_turn_scopes_the_model_s_skill_tools(monkeypatch, tmp_path):
    """The wiring itself: a real _run_loop turn whose model calls skills_list, then skill_view of a hidden skill."""
    calls = iter([
        ({"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "skills_list", "arguments": "{}"}},
            {"id": "t2", "type": "function", "function": {"name": "skill_view", "arguments": '{"name": "pdf-reading"}'}},
        ]}, {"model": "test-model", "finish_reason": "tool_calls", "input_tokens": 1, "output_tokens": 1}),
        _final_response("Done."),
    ])

    async def llm(**_call):
        return next(calls)

    monkeypatch.setattr(loop_module, "call_llm_with_retry", llm)
    session = _session(config=dict(HOX))
    store = _StepStore(session, LocalBackend(str(tmp_path)))
    prompt = PromptBuilder(TENANT, skills=catalog(), session=session, available_tools=set(_registry().tool_names))
    harness = _turn_harness(store, media_gen=None, gate=None, prompt=prompt)
    store.write(EventType.USER_MESSAGE, {"content": "List your skills."})
    await harness._run_loop(
        store.row.model_copy(deep=True), [{"role": "user", "content": "List your skills."}], "system",
        SimpleNamespace(lease_token=uuid4()), all_events=list(store.log),
    )
    results = {e.data["tool_call_id"]: e.data["content"] for e in store.log if e.type == EventType.TOOL_RESULT.value}
    listed = {entry["name"] for entry in json.loads(results["t1"])["skills"]}
    assert "pdf-reading" not in listed and "schedule" in listed
    assert "needs tools this session does not have" in json.loads(results["t2"])["error"]


@pytest.mark.parametrize("text, tools, named", [
    ("Read a PDF when read_file fails.", {"read_file"}, {"read_file"}),
    ("Read the plan file with `read_file`.", {"read_file"}, {"read_file"}),
    ("pdf.read_file_bytes() and my_read_file and read_file.py", {"read_file"}, set()),
    ("The terminal state is invoking writing-plans.", {"terminal"}, set()),
    ("Run it in `terminal`, or call terminal(command=...)", {"terminal"}, {"terminal"}),
    ("tags: [process, planning]", {"process"}, set()),
    ("Poll it with `process`.", {"process"}, {"process"}),
    ("Ask `vision_analyze` about each page.", {"vision_analyze", "web_search"}, {"vision_analyze"}),
    ("call mcp__crm__get_record( with an id", {"mcp__crm__get_record", "mcp__crm__get"}, {"mcp__crm__get_record"}),
])
def test_where_a_text_names_a_tool(text, tools, named):
    assert tools_named([text], tools) == named


def test_what_does_not_count():
    expert = SimpleNamespace(type="expert", description="uses `read_file`", trigger=None, content="", requires_tools=None)
    assert not needs_unavailable_tool(expert, {"read_file"})
    skill = {"type": "skill", "description": "Edit documents live.", "requires_tools": ["office_edit"], "content": ""}
    assert needs_unavailable_tool(skill, {"office_edit"}) and not needs_unavailable_tool(skill, {"read_file"})
    optional = {"type": "skill", "description": "Ask with `ask_user_question` where there is one.", "optional_tools": ["ask_user_question"]}
    assert not needs_unavailable_tool(optional, {"ask_user_question"})
    assert not needs_unavailable_tool({"type": "skill", "content": "use `read_file`"}, set())
