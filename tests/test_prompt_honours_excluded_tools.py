"""The prompt's tool guidance follows the tools the session is offered.

The worker builds the prompt from the registry's tools less what it knows
to drop; a session's own ``excluded_tools`` and ``allowed_tools`` were
applied to the schemas only.  So a session with every web tool excluded
was still told how to do web research, and curled Wikipedia or called a
``web_search`` it did not have.  The guidance now follows the schemas.
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
MEMORY_MARK = "memory"


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


async def test_a_session_without_exclusions_gets_the_prompt_it_got_before():
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
