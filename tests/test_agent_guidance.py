"""What a session is told about the ``agent`` family: how to start agents, and how to report as one."""

from __future__ import annotations

import re
from types import SimpleNamespace
from uuid import UUID

import pytest

from surogates.harness.prompt import PromptBuilder
from surogates.harness.prompt_library import default_library
from surogates.harness.subagents import DISPATCH_KEY
from surogates.tenant.context import TenantContext


def fragment(name: str) -> str:
    """The text the prompt library gives for *name*, as the prompt builder reads it."""
    return default_library().get(name)


def builder(available_tools: set[str], config: dict) -> PromptBuilder:
    tenant = TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    return PromptBuilder(tenant, session=SimpleNamespace(config=config), available_tools=available_tools)


def test_a_session_offered_agent_reads_how_to_use_it():
    prompt = builder(available_tools={"agent", "send_message"}, config={}).build()
    assert fragment("guidance/agents") in prompt and fragment("guidance/sub_agent") not in prompt


def test_a_sub_agent_reads_how_to_report():
    prompt = builder(available_tools={"read_file", "send_message"}, config={DISPATCH_KEY: {"mode": "background"}}).build()
    assert fragment("guidance/sub_agent") in prompt and fragment("guidance/agents") not in prompt


def test_a_session_without_the_family_reads_neither():
    prompt = builder(available_tools={"read_file", "delegate_task"}, config={}).build()
    assert fragment("guidance/agents") not in prompt and fragment("guidance/sub_agent") not in prompt


def test_a_sub_agent_that_may_start_agents_reads_both():
    prompt = builder(available_tools={"agent", "send_message"}, config={DISPATCH_KEY: {"mode": "background"}}).build()
    assert fragment("guidance/agents") in prompt and fragment("guidance/sub_agent") in prompt


@pytest.mark.parametrize("name", ["guidance/agents", "guidance/sub_agent"])
def test_the_guidance_names_no_product_and_no_file(name):
    text = fragment(name)
    assert not re.search(r"\b[\w/-]+\.(py|md|ts|json|yaml)\b", text)
    assert "Claude" not in text and "Surogate" not in text


def test_no_text_offers_a_sub_agent_the_background():
    # A sub-agent's call for the background is refused ("a sub-agent cannot start agents in the background"):
    # nothing a model reads may offer it.
    from surogates.tools.builtin import agents as agents_module

    for text in (fragment("guidance/agents"), agents_module._AGENT_DESCRIPTION):
        assert "ask for the background" not in text and "unless you pass true" not in text


def test_the_tools_say_an_agent_can_be_named_where_an_id_is_taken():
    from surogates.tools.builtin import agents as agents_module

    to = agents_module._SEND_MESSAGE_SCHEMA.parameters["properties"]["to"]["description"]
    stop = agents_module._STOP_AGENT_SCHEMA
    assert "name" in to
    assert "name" in stop.description and "name" in stop.parameters["properties"]["agent_id"]["description"]
    assert "name" in agents_module._LIST_AGENTS_SCHEMA.description
