"""The whiteboard guidance fragment exists, is loadable, and lands only
on agents that actually have the drawing tool."""
import pytest

from surogates.harness.prompt_library import PromptLibrary


def _library():
    return PromptLibrary()


def test_the_fragment_loads():
    body = _library().get("guidance/whiteboard")
    assert body.strip()


def test_the_fragment_says_the_canvas_is_infinite():
    body = _library().get("guidance/whiteboard")
    assert "infinite" in body
    # An edge or a fixed size would be a lie the model plans against.
    assert "20000" not in body


def test_the_fragment_names_the_tool():
    assert "whiteboard_draw" in _library().get("guidance/whiteboard")


@pytest.mark.parametrize("rule", [
    "latestInput",          # the authoritative attention region
    "sourceRect",           # image <-> global coordinate mapping
    "imageScale",
    "extend",               # extend the document, never reproduce it
    "arrow",                # spatial gesture reading
])
def test_the_fragment_carries_the_ported_rules(rule):
    assert rule in _library().get("guidance/whiteboard")


def test_the_fragment_credits_penecho_without_spending_tokens():
    # Attribution belongs in the frontmatter, which PromptLibrary strips
    # from the body: the model gains nothing from reading it, and every
    # token in this fragment is paid on every whiteboard turn.
    assert "PenEcho" in _library().metadata("guidance/whiteboard")["source"]
    assert "PenEcho" not in _library().get("guidance/whiteboard")


def test_the_fragment_has_frontmatter():
    # Every sibling fragment carries name/description frontmatter; the
    # metadata reader is what tooling introspects.
    meta = _library().metadata("guidance/whiteboard")
    assert meta.get("name") == "whiteboard"
    assert meta.get("description")


# ---------------------------------------------------------------------
# Injection: the fragment reaching the built prompt is the half that can
# silently break, so assert it against a real PromptBuilder.
# ---------------------------------------------------------------------

from types import SimpleNamespace
from uuid import uuid4

from surogates.harness.prompt import PromptBuilder
from surogates.tenant.context import TenantContext


def _tenant():
    return TenantContext(
        org_id=uuid4(),
        user_id=uuid4(),
        org_config={"default_model": "gpt-4o"},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/test_assets",
    )


def _session(config):
    return SimpleNamespace(
        config=config, channel="web", model=None,
        service_account_id=None, task_id=None,
    )


def _build(tools):
    return PromptBuilder(
        _tenant(), session=_session({}), available_tools=tools,
    ).build()


def test_an_agent_with_the_tool_gets_the_fragment():
    assert "Whiteboard canvas" in _build({"whiteboard_draw"})


def test_an_agent_without_the_tool_does_not():
    assert "Whiteboard canvas" not in _build({"web_search"})
    assert "Whiteboard canvas" not in _build(set())


def test_the_fragment_does_not_depend_on_the_session_config():
    """Keyed on the tool, not the session.

    The board is a view mode: a board-enabled agent can be handed a
    canvas at any point in an ordinary chat session, and the system
    prompt is built once per wake. Keying on ``config.surface`` meant
    the contract was missing exactly when the user first drew.
    """
    prompt = PromptBuilder(
        _tenant(),
        session=_session({"surface": "browser"}),
        available_tools={"whiteboard_draw"},
    ).build()
    assert "Whiteboard canvas" in prompt
