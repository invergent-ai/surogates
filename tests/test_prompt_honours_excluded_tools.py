"""The prompt's tool guidance follows the tools the session is offered.

The worker builds the prompt from the registry's tools less what it knows
to drop; the schemas also drop what the session names (``excluded_tools``,
``allowed_tools``) and what its kind never gets (the coordinator's tools
outside a coordinator, a strict coordinator's implementation tools).  The
guidance and the skill index now follow the schemas in every session.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from surogates.harness.budget import IterationBudget
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.prompt import PromptBuilder
from surogates.sandbox.pool import SandboxPool
from surogates.tools.registry import ToolRegistry, ToolSchema
from tests.test_case_tool_gate import _chat, _tenant

TOOLS = {"web_search", "web_extract", "create_artifact", "memory", "read_file", "ask_user_question"}
WEB = "# Web research"
ARTIFACT = "**Artifacts.**"


def _registry(names: set[str]) -> ToolRegistry:
    registry = ToolRegistry()
    for name in sorted(names):
        registry.register(
            name=name,
            schema=ToolSchema(name=name, description=name, parameters={"type": "object", "properties": {}}),
            handler=lambda args, **kwargs: "",
            toolset="core",
        )
    return registry


def _harness(session, builder: PromptBuilder) -> AgentHarness:
    return AgentHarness(
        session_store=AsyncMock(), tool_registry=_registry(TOOLS), llm_client=AsyncMock(),
        tenant=_tenant(session), worker_id="w", budget=IterationBudget(max_total=10),
        context_compressor=MagicMock(spec=ContextCompressor), prompt_builder=builder,
        sandbox_pool=MagicMock(spec=SandboxPool), vision_client=None, vision_model="",
        mcp_tool_names=frozenset(),
    )


async def _prompt(config: dict) -> tuple[str, set[str]]:
    """The system prompt of a session with *config*, and the tools its schemas offer."""
    session = _chat(config)
    builder = PromptBuilder(_tenant(session), session=session, available_tools=set(TOOLS))
    harness = _harness(session, builder)
    return await harness._build_system_prompt(session), harness._tool_filter_for_session(session)


async def test_an_excluded_tools_guidance_is_left_out_and_an_offered_ones_kept():
    prompt, offered = await _prompt({"excluded_tools": ["web_search", "web_extract"]})
    assert not {"web_search", "web_extract"} & offered
    assert WEB not in prompt
    assert "create_artifact" in offered and ARTIFACT in prompt


async def test_one_web_tool_left_keeps_the_web_guidance():
    prompt, _ = await _prompt({"excluded_tools": ["web_search"]})
    assert WEB in prompt  # web_extract alone still benefits from it, as before


async def test_an_allow_list_gives_the_guidance_of_its_tools_only():
    prompt, offered = await _prompt({"allowed_tools": ["read_file", "web_search"]})
    assert "create_artifact" not in offered
    assert WEB in prompt and ARTIFACT not in prompt


async def test_a_session_whose_schemas_offer_every_tool_keeps_its_prompt():
    config: dict = {}
    session = _chat(config)
    before = PromptBuilder(_tenant(session), session=session, available_tools=set(TOOLS)).build()
    prompt, _ = await _prompt(config)
    assert prompt == before
    assert WEB in prompt and ARTIFACT in prompt


def test_restricting_only_narrows():
    session = _chat({})
    builder = PromptBuilder(_tenant(session), session=session, available_tools={"web_search", "whiteboard_draw"})
    builder.restrict_tools({"web_search", "create_artifact"})
    assert builder._available_tools == {"web_search"}
    assert builder.has_whiteboard  # read for the schemas themselves: the worker's answer stands


def test_restricting_renders_the_agents_section_again():
    """A build before the narrowing (the compaction measure) must not leave the wider spawn tools behind."""
    from surogates.tools.loader import AgentDef

    session = _chat({"coordinator": True, "excluded_tools": ["spawn_task", "spawn_worker"]})
    builder = PromptBuilder(
        _tenant(session), session=session, available_tools={"spawn_task", "spawn_worker", "delegate_task"},
        available_agents=[AgentDef(name="analyst", description="Reads the numbers", system_prompt="",
                                   source="platform")],
    )
    assert "spawn_task" in builder._available_agents_section()
    builder.restrict_tools({"delegate_task"})
    section = builder._available_agents_section()
    assert "delegate_task" in section and "spawn_task" not in section


async def test_the_compaction_measure_is_narrowed_too():
    session = _chat({"excluded_tools": ["web_search", "web_extract"]})
    builder = PromptBuilder(_tenant(session), session=session, available_tools=set(TOOLS))
    harness = _harness(session, builder)
    harness._compressor.should_compress.return_value = False
    await harness._engineer_context(session, [], [])
    measured = harness._compressor.should_compress.call_args.args[1]
    assert WEB not in measured
    assert await harness._build_system_prompt(session) == measured


def test_sections_that_name_a_restricted_tool_stay_out():
    session = _chat({})
    kbs = [{"id": "kb-1", "name": "handbook", "display_name": "Handbook", "mode": "grounding"}]
    builder = PromptBuilder(_tenant(session), session=session, available_kbs=kbs,
                            available_tools={"kb_list_pages", "kb_read_page", "kb_search_pages", "read_file"})
    assert builder._kb_section()
    builder.restrict_tools({"read_file"})
    assert builder._kb_section() == ""
    unrestricted = PromptBuilder(_tenant(session), session=session, available_kbs=kbs, available_tools={"read_file"})
    assert unrestricted._kb_section()  # a builder never narrowed: as before


def _real_harness(session, registry, skills, tools):
    return AgentHarness(
        session_store=AsyncMock(), tool_registry=registry, llm_client=AsyncMock(),
        tenant=_tenant(session), worker_id="w", budget=IterationBudget(max_total=10),
        context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=PromptBuilder(_tenant(session), session=session, available_tools=set(tools), skills=skills),
        sandbox_pool=MagicMock(spec=SandboxPool), vision_client=None, vision_model="",
        mcp_tool_names=frozenset(),
    )


async def test_the_skill_index_follows_the_schemas_in_every_session():
    """The built-in registry and the in-tree skills: the orchestrator skill needs spawn_task, which only a
    coordinator's schemas offer."""
    from surogates.tools.loader import ResourceLoader
    from surogates.tools.runtime import ToolRuntime

    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    skills = ResourceLoader()._load_skills_from_dir("skills", "platform")
    assert any(getattr(s, "name", "") == "subagent-task-orchestrator" for s in skills)
    tools = set(registry.tool_names)

    ordinary = _chat({})
    harness = _real_harness(ordinary, registry, skills, tools)
    assert "spawn_task" not in harness._tool_filter_for_session(ordinary)
    assert "subagent-task-orchestrator" not in await harness._build_system_prompt(ordinary)

    coordinator = _chat({"coordinator": True})
    harness = _real_harness(coordinator, registry, skills, tools)
    assert "spawn_task" in harness._tool_filter_for_session(coordinator)
    assert "subagent-task-orchestrator" in await harness._build_system_prompt(coordinator)


async def test_a_strict_coordinator_is_not_told_about_its_implementation_tools():
    from surogates.tools.runtime import ToolRuntime

    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    tools = set(registry.tool_names)
    strict = _chat({"coordinator": True, "strict_coordinator": True})
    harness = _real_harness(strict, registry, [], tools)
    offered = harness._tool_filter_for_session(strict)
    assert not {"web_search", "web_extract"} & offered
    assert WEB not in await harness._build_system_prompt(strict)
