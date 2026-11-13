"""A skill that needs or names a tool the session is not sent is hidden: from the skill index, skills_list and skill_view.

HO-X run (surogates 15af7f4e0, template eog-lean-govoff-fixed, 14 tools in ``excluded_tools``): the model called
``read_file`` and ``terminal`` 26 times in 10 of 763 sessions, and each call was refused "Unknown tool".  It read the
names in skill texts: ``skills_list`` gave pdf-reading's "Read a PDF when read_file fails ...", ``skill_view`` gave
office's, xlsx's, pdf's, executing-plans' bodies.  The prompt's index hid a skill only by its ``requires_tools``,
and the skill tools did not even do that.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.prompt import PromptBuilder
from surogates.sandbox.pool import SandboxPool
from surogates.tools.builtin.skills import _skill_view_handler, _skills_list_handler
from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.tools.skill_visibility import SkillScope, needs_unavailable_tool, skill_scope, tools_named
from tests.test_case_tool_gate import _chat, _tenant
from tests.test_fallback_skills_hidden import TENANT, catalog, the_worker_reads_the_repositorys_skills  # noqa: F401

# The HO-X template's excluded_tools (eog-lean's 14 denied tools).
HOX_EXCLUDED = [
    "terminal", "process", "read_file", "write_file", "patch", "search_files", "list_files", "web_search",
    "web_extract", "vision_analyze", "generate_image", "generate_video", "github", "run_coding_agent",
]
OTHERS = ["skills_list", "skill_view", "agent", "consult_expert", "load_tools", "memory", "office_edit", "office_read"]


def names_a(text: str, tool: str) -> bool:
    """Read independently of the code: a tool named as the skill texts name one, in backticks or called; a name
    with an underscore anywhere."""
    if f"`{tool}`" in text or re.search(rf"(?<![\w.-]){re.escape(tool)}\(", text):
        return True
    return "_" in tool and re.search(rf"(?<![\w.-]){re.escape(tool)}(?![\w-])", text) is not None


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name in sorted({*HOX_EXCLUDED, *OTHERS}):
        registry.register(
            name=name, schema=ToolSchema(name=name, description=name, parameters={"type": "object", "properties": {}}),
            handler=lambda args, **kwargs: "", toolset="core",
        )
    return registry


def _harness(config: dict) -> tuple[AgentHarness, object]:
    session = _chat(config)
    builder = PromptBuilder(_tenant(session), skills=catalog(), session=session, available_tools={*HOX_EXCLUDED, *OTHERS})
    harness = AgentHarness(
        session_store=AsyncMock(), tool_registry=_registry(), llm_client=AsyncMock(), tenant=_tenant(session),
        worker_id="w", budget=IterationBudget(max_total=10), context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=builder, sandbox_pool=MagicMock(spec=SandboxPool), vision_client=None, vision_model="",
        mcp_tool_names=frozenset(),
    )
    return harness, session


async def _what_the_skill_tools_give(config: dict) -> tuple[list[dict], dict[str, dict], str]:
    """The skills_list a session with *config* gets this turn, skill_view of every catalog skill, and its prompt."""
    harness, session = _harness(config)
    prompt = await harness._build_system_prompt(session)
    harness._scope_skills(harness._tool_filter_for_session(session))  # as the turn does, beside the experts' scope
    listed = json.loads(await _skills_list_handler({}, tenant=TENANT, session_config=config, channel="api"))["skills"]
    views = {
        skill.name: json.loads(await _skill_view_handler({"name": skill.name}, tenant=TENANT, session_config=config, channel="api"))
        for skill in catalog()
    }
    return listed, views, prompt


async def test_a_session_with_excluded_tools_gets_no_skill_text_naming_one():
    listed, views, prompt = await _what_the_skill_tools_give({"excluded_tools": HOX_EXCLUDED})
    texts = [json.dumps(entry) for entry in listed]
    texts += [view.get("content", "") + view.get("description", "") for view in views.values() if view.get("success")]
    named = {tool for text in texts for tool in HOX_EXCLUDED if names_a(text, tool)}
    # Master: read_file (pdf-reading's description; xlsx, pdf, docx, executing-plans), vision_analyze, web_search, ...
    assert named == set()
    index = prompt[prompt.find("Skills"):]
    assert not {tool for tool in HOX_EXCLUDED if names_a(index, tool)}
    # The skills that name none of them are still there; brainstorming has the design doc saved with `write_file`.
    shown = {entry["name"] for entry in listed}
    assert {"writing-plans", "schedule", "doc-coauthoring"} <= shown
    assert not {"pdf-reading", "brainstorming"} & shown


async def test_a_hidden_skill_viewed_by_name_is_refused_without_naming_a_tool():
    _, views, _ = await _what_the_skill_tools_give({"excluded_tools": HOX_EXCLUDED})
    refused = views["pdf-reading"]
    assert refused["success"] is False
    assert refused["error"] == "The pdf-reading skill needs tools this session does not have. Do the work with the tools you have."


async def test_a_session_sent_every_tool_keeps_every_skill():
    listed, views, _ = await _what_the_skill_tools_give({})
    shown = {entry["name"] for entry in listed}
    assert {"pdf-reading", "pdf", "xlsx", "executing-plans", "maps"} <= shown
    assert views["pdf-reading"].get("success") is not False


async def test_an_allowed_tools_list_hides_the_skills_naming_what_it_leaves_out():
    listed, _, _ = await _what_the_skill_tools_give({"allowed_tools": ["skills_list", "skill_view", "web_search"]})
    shown = {entry["name"] for entry in listed}
    assert "pdf-reading" not in shown and "maps" in shown  # maps names web_search, which this session is sent


async def test_through_the_api_the_list_leaves_them_out_too():
    harness, session = _harness({"excluded_tools": HOX_EXCLUDED})
    harness._scope_skills(harness._tool_filter_for_session(session))
    api = SimpleNamespace(list_skills=AsyncMock(return_value="{}"), view_skill=AsyncMock(return_value="{}"))
    await _skills_list_handler({}, api_client=api, session_config={"excluded_tools": HOX_EXCLUDED})
    left_out = api.list_skills.await_args.kwargs["leave_out"]
    assert {"pdf-reading", "pdf", "xlsx", "executing-plans"} <= left_out
    answer = json.loads(await _skill_view_handler({"name": "pdf-reading"}, api_client=api, session_config={}))
    assert answer["success"] is False
    api.view_skill.assert_not_awaited()  # nothing of the skill is read or staged


def test_outside_a_turn_nothing_is_hidden():
    assert skill_scope.get() == SkillScope()


@pytest.mark.parametrize("text, tools, named", [
    ("Read a PDF when read_file fails.", {"read_file"}, {"read_file"}),
    ("Read the plan file with `read_file`.", {"read_file"}, {"read_file"}),
    ("pdf.read_file_bytes() and my_read_file", {"read_file"}, set()),
    ("The terminal state is invoking writing-plans.", {"terminal"}, set()),
    ("Run it in `terminal`, or call terminal(command=...)", {"terminal"}, {"terminal"}),
    ("tags: [process, planning]", {"process"}, set()),
    ("Poll it with `process`.", {"process"}, {"process"}),
    ("Ask `vision_analyze` about each page.", {"vision_analyze", "web_search"}, {"vision_analyze"}),
])
def test_where_a_text_names_a_tool(text, tools, named):
    assert tools_named([text], tools) == named


def test_an_expert_is_never_hidden_by_its_text_and_requires_tools_hides_a_skill():
    expert = SimpleNamespace(type="expert", description="uses `read_file`", trigger=None, content="", requires_tools=None)
    assert not needs_unavailable_tool(expert, {"read_file"})
    skill = {"type": "skill", "description": "Edit documents live.", "requires_tools": ["office_edit"], "content": ""}
    assert needs_unavailable_tool(skill, {"office_edit"}) and not needs_unavailable_tool(skill, {"read_file"})
