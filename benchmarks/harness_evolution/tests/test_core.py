import json
import subprocess
from copy import deepcopy

import pytest

from harness_evolution.candidates import apply_proposal, snapshot, source_hash
from harness_evolution.config import load_config, partition, validate_tasks
from harness_evolution.proposer import search_packet
from harness_evolution.scoring import InvalidEvaluation, compare, normalize, validate_results


def row(tid, score, seconds=10):
    return {"benchmark": "claweval", "task_id": tid, "score": score,
            "passed": score == 1, "seconds": seconds, "safety_score": 1}


def repetitions(score, standard=0, guard=1, seconds=10):
    return [{"pro": [row("select", score, seconds), row("guard", guard, seconds)],
             "standard": [row("select", standard, seconds), row("guard", 1, seconds)]} for _ in range(3)]


def test_promotion_requires_both_models_and_repeated_pro_gain(config, tasks):
    selection = [t for t in tasks if t["split"] == "selection"]
    result = compare(repetitions(0), repetitions(1), selection, config["policy"])
    assert result["accepted"]
    assert result["models"]["pro"]["delta"] == 0.5
    one_lucky_run = repetitions(0)
    one_lucky_run[0] = repetitions(1)[0]
    assert not compare(repetitions(0), one_lucky_run, selection, config["policy"])["accepted"]


def test_standard_and_guard_regressions_cannot_hide_behind_pro_gain(config, tasks):
    selection = [t for t in tasks if t["split"] == "selection"]
    result = compare(repetitions(0, standard=1), repetitions(1), selection, config["policy"])
    assert not result["accepted"]
    result = compare(repetitions(0), repetitions(1, guard=0.8), selection, config["policy"])
    assert not result["accepted"]
    assert "pro: regression guard declined" in result["reasons"]


def test_safety_regression_on_an_already_failing_task_blocks_promotion(config, tasks):
    selection = [t for t in tasks if t["split"] == "selection"]
    candidate = repetitions(1)
    for repetition in candidate:
        repetition["standard"][0]["safety_score"] = 0
    result = compare(repetitions(0), candidate, selection, config["policy"])
    assert not result["accepted"]
    assert "standard: safety score declined" in result["reasons"]
    assert result["models"]["standard"]["delta"] == 0


def test_per_family_regression_and_latency_limits(config, tasks):
    selection = [t for t in tasks if t["split"] == "selection"]
    selection[1]["guard"] = False
    selection[1]["family"] = "other"
    assert not compare(repetitions(0), repetitions(1, guard=0.8), selection, config["policy"])["accepted"]
    assert not compare(repetitions(0), repetitions(1, seconds=20), selection, config["policy"])["accepted"]
    with pytest.raises(InvalidEvaluation, match="timing"):
        compare(repetitions(0), repetitions(1, seconds=None), selection, config["policy"])


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, 2, True, None])
def test_invalid_scores_are_never_promoted(bad):
    with pytest.raises(InvalidEvaluation):
        validate_results([row("x", bad)], [{"benchmark": "claweval", "task_id": "x"}])


def test_missing_and_duplicate_results_invalidate_comparison():
    tasks = [{"benchmark": "claweval", "task_id": "x"}]
    for results in ([], [row("x", 1), row("x", 1)], [row("y", 1)]):
        with pytest.raises(InvalidEvaluation):
            validate_results(results, tasks)


def test_benchmark_grading_and_infrastructure_are_distinct():
    claw = {"task_id": "a", "terminal_status": "completed", "scores": {"completion": 1, "safety": 0}}
    assert normalize("claweval", claw)["score"] == 0
    claw["scores"]["safety"] = 1
    assert normalize("claweval", claw)["score"] == 1
    for broken in ({**claw, "grader_error": "failed"}, {**claw, "scores": None}, {**claw, "error": "provider 429"}):
        with pytest.raises(InvalidEvaluation):
            normalize("claweval", broken)
    workspace = {"task_id": "a", "terminal_status": "completed", "passed_rubrics": 3, "total_rubrics": 4}
    assert normalize("workspace_bench", workspace)["score"] == 0.75
    assert normalize("workspace_bench", {**workspace, "terminal_status": "timeout"})["score"] == 0
    with pytest.raises(InvalidEvaluation):
        normalize("workspace_bench", {**workspace, "judge_error": "bad JSON"})


def test_partition_is_reproducible_keeps_groups_and_sealed_holdout():
    catalog = [{"benchmark": "claweval", "task_id": str(i), "family": "workflow",
                "group": f"family-{i // 2}", "failure_mode": "incomplete", "sealed_holdout": i == 0}
               for i in range(20)]
    result = partition(catalog, seed=12)
    assert result == partition(catalog, seed=12)
    assert {t["split"] for t in result[:2]} == {"holdout"}
    by_group = {}
    for task in result:
        by_group.setdefault(task["group"], set()).add(task["split"])
    assert all(len(roles) == 1 for roles in by_group.values())
    assert {t["split"] for t in result} == {"search", "selection", "holdout"}


def test_manifest_rejects_leakage_and_duplicates(tasks):
    for broken in (tasks + [tasks[0]], [{**t, "group": "same"} for t in tasks],
                   [{**t, "sealed_holdout": True} for t in tasks]):
        with pytest.raises(ValueError):
            validate_tasks(broken)


def test_workspace_existing_holdout_cannot_be_relabelled(config, tmp_path):
    tasks = [{"benchmark": "workspace_bench", "task_id": tid, "family": "files", "split": split}
             for tid, split in (("1", "search"), ("2", "selection"), ("4", "search"))]
    manifest = tmp_path / "ws.json"
    manifest.write_text(json.dumps(tasks))
    config["tasks"] = str(manifest)
    path = tmp_path / "ws-config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="holdout"):
        load_config(path)


def test_snapshot_ignores_dirty_work_and_exports_applicable_patch(repository, tmp_path, config):
    name = config["allowed_files"][0]
    (repository / name).write_text("Unrelated local edit\n")
    seed = tmp_path / "seed"
    snapshot(repository, "HEAD", seed)
    assert (seed / name).read_text() == "Keep working.\n"
    assert not (seed / "benchmarks").exists()
    assert not (seed / "answer-key.json").exists()
    before = source_hash(seed)
    proposal = {"hypothesis": "verify output", "files": {name: "Verify the output.\n\n"}}
    candidate = tmp_path / "candidate"
    patch = apply_proposal(seed, candidate, proposal, config["allowed_files"])
    assert source_hash(seed) == before
    patch_file = tmp_path / "change.patch"
    patch_file.write_text(patch)
    subprocess.run(["git", "apply", "--check", str(patch_file)], cwd=seed, check=True)
    subprocess.run(["git", "apply", str(patch_file)], cwd=seed, check=True)
    assert (seed / name).read_bytes() == (candidate / name).read_bytes()
    assert (repository / name).read_text() == "Unrelated local edit\n"


@pytest.mark.parametrize("name,content", [("../escape.py", "x=1"), ("answer-key.json", "{}"),
                                         ("surogates/harness/test.py", "def syntax error")])
def test_candidate_scope_and_syntax(repository, tmp_path, config, name, content):
    with pytest.raises((ValueError, SyntaxError)):
        apply_proposal(repository, tmp_path / "candidate", {"hypothesis": "x", "files": {name: content}}, config["allowed_files"])
    assert not (tmp_path / "candidate").exists()


def test_proposer_never_receives_selection_details_or_grader_fields(repository, tmp_path, config, tasks):
    trace = tmp_path / "events.jsonl"
    trace.write_text(json.dumps({"type": "user.message", "data": {"content": "Find the invoice", "gold_answer": "HIDDEN-ANSWER"}}) + "\n")
    result = {"pro": [{**row("search", 0), "trace_path": str(trace)}]}
    journal = [{"hypothesis": "verify files", "accepted": False, "status": "rejected",
                "comparison": {"task_id": "HIDDEN-SELECTION-TASK", "answer": "HIDDEN-ANSWER"}}]
    packet = search_packet(repository, config["allowed_files"], [tasks[0]], result, journal)
    assert "HIDDEN" not in json.dumps(packet)
    assert "Find the invoice" in json.dumps(packet)
    with pytest.raises(ValueError):
        search_packet(repository, config["allowed_files"], tasks, result, journal)
