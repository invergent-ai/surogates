"""The two predicates every whiteboard code path asks."""
from types import SimpleNamespace

from surogates.whiteboard.session import (
    is_whiteboard_session,
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
