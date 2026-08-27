"""The whiteboard guidance fragment exists, is loadable, and only lands
on whiteboard sessions."""
import pytest

from surogates.harness.prompt_library import PromptLibrary


def _library():
    return PromptLibrary()


def test_the_fragment_loads():
    body = _library().get("guidance/whiteboard")
    assert body.strip()


def test_the_fragment_states_the_canvas_size():
    assert "20000" in _library().get("guidance/whiteboard")


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


def test_the_fragment_credits_penecho():
    body = _library().get("guidance/whiteboard")
    assert "PenEcho" in body


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


def _build(config):
    return PromptBuilder(
        _tenant(),
        session=_session(config),
        available_tools={"whiteboard_draw"},
    ).build()


def test_a_whiteboard_session_gets_the_fragment():
    assert "Whiteboard canvas" in _build({"surface": "whiteboard"})


def test_a_plain_session_does_not_get_the_fragment():
    assert "Whiteboard canvas" not in _build({})


def test_another_surface_does_not_get_the_fragment():
    assert "Whiteboard canvas" not in _build({"surface": "browser"})
