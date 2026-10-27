"""A routine's run and a helper have nobody to answer a question.

A scheduled run that called ``ask_user_question`` stayed active forever:
nobody was present, and answering it from the inbox was refused because
the run is read-only.  Both surfaces (prompt and schemas) must drop the
tool, and the platform hint must say nobody is there.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from surogates.harness.prompt_library import PromptLibrary
from surogates.orchestrator.worker import _filter_effective_tools
from surogates.tenant.context import TenantContext
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime
from tests.test_steer_loop import _make_loop_harness, _make_session

UNATTENDED = ["scheduled", "delegation", "worker", "task"]


def _prompt_surface(channel: str) -> set[str]:
    tenant = TenantContext(
        org_id=uuid.uuid4(), user_id=uuid.uuid4(), org_config={},
        user_preferences={}, permissions=frozenset(), asset_root="/tmp",
    )
    return _filter_effective_tools(
        tools={"ask_user_question", "web_search"},
        tenant=tenant,
        session=SimpleNamespace(config={}, channel=channel),
        use_api_for_harness_tools=False,
    )


def _schema_surface(channel: str) -> set[str]:
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    harness = _make_loop_harness(session_store=None)
    harness._tools = registry
    session = _make_session()
    session.channel = channel
    offered = harness._tool_filter_for_session(session)
    return set(registry.tool_names) if offered is None else offered


@pytest.mark.parametrize("channel", UNATTENDED)
def test_an_unattended_session_is_never_offered_a_question(channel):
    assert "ask_user_question" not in _prompt_surface(channel)
    assert "ask_user_question" not in _schema_surface(channel)
    assert "web_search" in _prompt_surface(channel)


@pytest.mark.parametrize("channel", ["studio", "web", "slack"])
def test_a_conversation_keeps_its_questions(channel):
    assert "ask_user_question" in _prompt_surface(channel)


@pytest.mark.parametrize("channel", UNATTENDED)
def test_an_unattended_session_is_told_nobody_is_there(channel):
    hint = PromptLibrary().platform_hint(channel)
    assert hint and "cannot ask questions" in hint
