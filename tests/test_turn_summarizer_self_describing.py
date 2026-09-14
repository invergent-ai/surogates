"""An iteration made only of skill_view or skill_step calls says everything
in its arguments; a caption would restate it for the price of a model call."""

from surogates.harness.turn_summarizer import _is_self_describing_iteration


def _call(name):
    return {"id": "c1", "function": {"name": name, "arguments": "{}"}}


def _ok():
    return {"tool_call_id": "c1", "name": "x", "content": '{"ok": true}'}


def test_step_markers_alone_are_self_describing():
    assert _is_self_describing_iteration([_call("skill_step")], [_ok()])
    assert _is_self_describing_iteration([_call("skill_step"), _call("skill_view")], [_ok(), _ok()])


def test_markers_mixed_with_real_work_still_get_a_caption():
    assert not _is_self_describing_iteration([_call("skill_step"), _call("terminal")], [_ok(), _ok()])


def test_empty_batches_are_not_self_describing():
    assert not _is_self_describing_iteration([], [])
