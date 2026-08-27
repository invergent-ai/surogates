"""The two predicates every whiteboard code path asks."""
from types import SimpleNamespace

from surogates.whiteboard.session import (
    is_whiteboard_session,
    is_whiteboard_turn,
    turn_mode,
    whiteboard_metadata,
)


def _session(config=None):
    return SimpleNamespace(config=config or {}, channel="web")


def test_plain_session_is_not_a_whiteboard():
    assert is_whiteboard_session(_session()) is False


def test_surface_stamp_marks_a_whiteboard():
    assert is_whiteboard_session(_session({"surface": "whiteboard"})) is True


def test_another_surface_is_not_a_whiteboard():
    assert is_whiteboard_session(_session({"surface": "browser"})) is False


def test_missing_config_attribute_is_tolerated():
    # Several test harnesses build partial session objects.
    assert is_whiteboard_session(SimpleNamespace()) is False


def test_none_config_is_tolerated():
    assert is_whiteboard_session(SimpleNamespace(config=None)) is False


def test_a_turn_carrying_canvas_metadata_is_a_whiteboard_turn():
    assert is_whiteboard_turn({"whiteboard": {"mode": "sketch"}}) is True


def test_a_turn_with_an_empty_canvas_block_still_counts():
    # The block is what the client attaches alongside the render; an
    # empty one still means "this came from the board".
    assert is_whiteboard_turn({"whiteboard": {}}) is True


def test_a_typed_message_is_not_a_whiteboard_turn():
    # A board session also has a transcript view the user can type into
    # -- getting this wrong narrows that turn to a single drawing tool.
    assert is_whiteboard_turn({}) is False
    assert is_whiteboard_turn({"view_context": {}}) is False


def test_absent_metadata_is_not_a_whiteboard_turn():
    assert is_whiteboard_turn(None) is False


def test_a_non_dict_block_is_not_a_whiteboard_turn():
    # Client-supplied: a malformed block must not be read as consent.
    assert is_whiteboard_turn({"whiteboard": "nonsense"}) is False
    assert is_whiteboard_turn("nonsense") is False


def test_turn_mode_defaults_to_sketch():
    assert turn_mode(None) == "sketch"
    assert turn_mode({}) == "sketch"
    assert turn_mode({"whiteboard": {}}) == "sketch"


def test_turn_mode_reads_deep():
    assert turn_mode({"whiteboard": {"mode": "deep"}}) == "deep"


def test_turn_mode_rejects_an_unknown_value():
    # An unrecognised mode must fall back to the cheap path, never the
    # expensive one -- an attacker-controlled string must not be able to
    # promote a turn to the full tool catalogue.
    assert turn_mode({"whiteboard": {"mode": "unlimited"}}) == "sketch"


def test_turn_mode_tolerates_a_non_dict_metadata():
    assert turn_mode("nonsense") == "sketch"
    assert turn_mode({"whiteboard": "nonsense"}) == "sketch"


def test_whiteboard_metadata_extracts_the_payload():
    assert whiteboard_metadata({"whiteboard": {"imageScale": 0.5}}) == {
        "imageScale": 0.5,
    }


def test_whiteboard_metadata_returns_none_when_absent():
    assert whiteboard_metadata({"view_context": {}}) is None
    assert whiteboard_metadata(None) is None


def test_the_runtime_capability_defaults_to_off():
    """The consuming half of the ops runtime-config contract.

    A payload predating this field describes an agent that never had a
    board, so the default must be off. Asserted here rather than in ops:
    ops resolves surogates from its own pinned wheel, so the same check
    there would test whichever build happens to be installed.
    """
    from surogates.runtime.context import AgentRuntimeContext

    field = AgentRuntimeContext.__dataclass_fields__["whiteboard_enabled"]
    assert field.default is False


# ---------------------------------------------------------------------
# The surface is a capability, not a preference.
# ---------------------------------------------------------------------

from surogates.whiteboard.session import surface_rejection


def test_a_whiteboard_surface_is_allowed_when_the_agent_has_one():
    assert surface_rejection(
        {"surface": "whiteboard"}, whiteboard_enabled=True,
    ) is None


def test_a_whiteboard_surface_is_refused_when_the_agent_has_none():
    # The surface no longer confers the tool -- the agent capability
    # does -- but asking for a board on an agent that has none is still
    # a request the server cannot honour, and a 403 here beats a canvas
    # that silently never draws.
    err = surface_rejection({"surface": "whiteboard"}, whiteboard_enabled=False)
    assert err is not None
    assert "whiteboard" in err


def test_an_absent_surface_is_always_fine():
    assert surface_rejection({}, whiteboard_enabled=False) is None
    assert surface_rejection({"other": 1}, whiteboard_enabled=False) is None


def test_an_unknown_surface_is_refused_even_when_enabled():
    # An unrecognised value must not fall through as "not a whiteboard,
    # therefore harmless" -- it is a client asking for something the
    # server does not implement.
    err = surface_rejection({"surface": "canvas"}, whiteboard_enabled=True)
    assert err is not None
    assert "canvas" in err


def test_a_non_dict_config_is_tolerated():
    assert surface_rejection(None, whiteboard_enabled=False) is None
    assert surface_rejection("nope", whiteboard_enabled=False) is None


def _runtime_payload(**over):
    """Minimum viable runtime-config payload for the resolver."""
    base = {
        "agent_id": "a1",
        "org_id": "o1",
        "project_id": "p1",
        "enabled": True,
        "version": 1,
        "storage_key_prefix": "agents/a1",
    }
    base.update(over)
    return base


def test_the_resolver_parses_the_capability_from_the_payload():
    """The consuming half of the ops runtime-config contract.

    Declared-but-unparsed is the failure this guards: the dataclass had
    the field while ``build_agent_runtime_context`` never read it, so the
    operator's switch was inert no matter what ops sent.
    """
    from surogates.runtime.resolver import build_agent_runtime_context

    payload = _runtime_payload()
    assert build_agent_runtime_context(
        {**payload, "whiteboard_enabled": True},
    ).whiteboard_enabled is True
    assert build_agent_runtime_context(
        {**payload, "whiteboard_enabled": False},
    ).whiteboard_enabled is False


def test_an_older_ops_payload_leaves_the_board_off():
    # A payload predating the capability describes an agent that never
    # had a board; defaulting True would hand one to every agent.
    from surogates.runtime.resolver import build_agent_runtime_context

    assert build_agent_runtime_context(
        _runtime_payload(),
    ).whiteboard_enabled is False


def test_reuse_lookup_accepts_a_surface_scope():
    """Single-session reuse must not cross surfaces.

    The standalone board route asks for a session by surface so that
    reopening it resumes the board rather than adopting whichever chat
    session the reuse lookup happened to return.
    """
    import inspect

    from surogates.session.store import SessionStore

    sig = inspect.signature(SessionStore.get_reusable_channel_session)
    assert "surface" in sig.parameters
    assert sig.parameters["surface"].default is None
