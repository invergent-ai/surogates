"""The partition is the whole experiment design, so it is what gets tested."""
import json

import pytest

from promptgepa.sets import NoSignalError, derive


def write_run(tmp_path, name, rows):
    d = tmp_path / name
    (d / "x").mkdir(parents=True, exist_ok=True)
    (d / "outcomes.json").write_text(json.dumps([
        {"task_id": t, "level": 2, "strict_pass": p, "lenient_pass": p,
         "flags": list(f)}
        for t, p, f in rows
    ]))
    return d


def test_partitions_by_agreement_across_runs(tmp_path):
    a = write_run(tmp_path, "a", [
        ("fail-flagged", False, ["no_tool_use"]),
        ("fail-quiet", False, []),
        ("passer", True, []),
        ("flipper", True, []),
    ])
    b = write_run(tmp_path, "b", [
        ("fail-flagged", False, ["no_final_answer"]),
        ("fail-quiet", False, []),
        ("passer", True, []),
        ("flipper", False, []),
    ])
    sets = derive([a, b], guard_size=5)

    assert sets.stable_fail == ("fail-flagged", "fail-quiet")
    assert sets.stable_pass == ("passer",)
    assert sets.flippers == ("flipper",)
    # Flagged failures train; the rest is held back to select on.
    assert sets.train == ("fail-flagged",)
    assert set(sets.val) == {"fail-quiet", "passer"}
    assert sets.guard == ("passer",)


def test_flippers_never_reach_a_set(tmp_path):
    a = write_run(tmp_path, "a", [("t", True, []), ("keep", False, ["x"])])
    b = write_run(tmp_path, "b", [("t", False, []), ("keep", False, ["x"])])
    sets = derive([a, b])
    assert "t" not in set(sets.train) | set(sets.val) | set(sets.guard)


def test_unsupported_capability_is_excluded(tmp_path):
    rows = [("audio", False, ["unsupported_capability"]), ("real", False, ["no_tool_use"])]
    a, b = write_run(tmp_path, "a", rows), write_run(tmp_path, "b", rows)
    sets = derive([a, b])
    assert sets.stable_fail == ("real",)


def test_train_flags_narrow_to_one_failure_class(tmp_path):
    rows = [
        ("quiet", False, ["no_tool_use"]),
        ("unanswered", False, ["no_final_answer"]),
        ("passer", True, []),
    ]
    a, b = write_run(tmp_path, "a", rows), write_run(tmp_path, "b", rows)
    sets = derive([a, b], train_flags=("no_tool_use",))
    assert sets.train == ("quiet",)
    assert "unanswered" in sets.val


def test_guard_sample_is_deterministic(tmp_path):
    rows = [(f"p{i}", True, []) for i in range(20)] + [("f", False, ["no_tool_use"])]
    a, b = write_run(tmp_path, "a", rows), write_run(tmp_path, "b", rows)
    assert derive([a, b], guard_size=5).guard == derive([a, b], guard_size=5).guard


def test_one_run_is_refused(tmp_path):
    a = write_run(tmp_path, "a", [("t", False, [])])
    with pytest.raises(ValueError, match="need >=2 runs"):
        derive([a])


def test_no_stable_failure_is_an_error_not_an_empty_run(tmp_path):
    rows = [("t", True, [])]
    a, b = write_run(tmp_path, "a", rows), write_run(tmp_path, "b", rows)
    with pytest.raises(NoSignalError):
        derive([a, b])


def test_unmatched_train_flags_names_what_was_available(tmp_path):
    rows = [("t", False, ["no_tool_use"])]
    a, b = write_run(tmp_path, "a", rows), write_run(tmp_path, "b", rows)
    with pytest.raises(NoSignalError, match="no_tool_use"):
        derive([a, b], train_flags=("tool_error",))
