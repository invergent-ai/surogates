"""Unit tests for deterministic insight concat-propagation."""
from surogates.arbor.propagate import concat_propagate


def test_concat_propagate_appends_child_lesson_up_the_chain():
    insights = {"ROOT": "objective", "1": None, "1.1": "lr=3e-4 overfits"}
    parents = {"1": "ROOT", "1.1": "1"}
    updates = concat_propagate(
        node_key="1.1", insight="lr=3e-4 overfits",
        insights=insights, parents=parents, cap_chars=1200,
    )
    assert updates["1"] == "[from 1.1] lr=3e-4 overfits"
    assert "[from 1.1] lr=3e-4 overfits" in updates["ROOT"]


def test_concat_propagate_caps_and_keeps_tail():
    insights = {"ROOT": "x" * 1190, "1": "p"}
    parents = {"1": "ROOT"}
    updates = concat_propagate(
        node_key="1", insight="LESSON",
        insights=insights, parents=parents, cap_chars=1200,
    )
    assert len(updates["ROOT"]) <= 1200
    assert updates["ROOT"].endswith("[from 1] LESSON")


def test_concat_propagate_skips_empty_insight():
    assert concat_propagate(
        node_key="1", insight="", insights={"ROOT": None, "1": None},
        parents={"1": "ROOT"}, cap_chars=1200,
    ) == {}


def test_concat_propagate_root_has_no_parent():
    # ROOT itself has no entry in ``parents`` -> nothing to propagate.
    assert concat_propagate(
        node_key="ROOT", insight="anything",
        insights={"ROOT": None}, parents={}, cap_chars=1200,
    ) == {}
