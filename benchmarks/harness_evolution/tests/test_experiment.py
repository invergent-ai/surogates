import json
from copy import deepcopy
from pathlib import Path

import pytest

from harness_evolution.config import read_json, task_key
from harness_evolution.experiment import final_test, run
from harness_evolution.scoring import InvalidEvaluation


class FixtureRuntime:
    calls = []
    fail_label = None

    def __init__(self, config, run_dir, deadline):
        self.run_dir = run_dir

    def evaluate(self, source, tasks, label):
        self.calls.append((label, [task_key(t) for t in tasks]))
        if self.fail_label and self.fail_label in label:
            raise InvalidEvaluation("fixture infrastructure failure")
        changed = "Verify" in (source / "surogates/harness/prompts/guidance/test.md").read_text()
        results = {}
        for tier in ("pro", "standard"):
            results[tier] = []
            for task in tasks:
                score = int(task["task_id"] == "guard" or (changed and tier == "pro"))
                trace = self.run_dir / f"{label}-{tier}-{task['task_id']}.jsonl"
                trace.write_text(json.dumps({"type": "user.message", "data": {"content": "Inspect an invoice"}}) + "\n")
                results[tier].append({"benchmark": task["benchmark"], "task_id": task["task_id"],
                                      "score": score, "passed": bool(score), "seconds": 1, "safety_score": 1,
                                      "trace_path": str(trace)})
        return results


@pytest.fixture(autouse=True)
def reset_runtime():
    FixtureRuntime.calls = []
    FixtureRuntime.fail_label = None


def recorded(config, tmp_path):
    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps({"hypothesis": "Verify before concluding", "smoke_task": "claweval:search",
                                    "files": {config["allowed_files"][0]: "Verify the final output.\n"}}))
    config["proposer"] = {"recorded": [str(proposal)]}
    config["policy"]["max_proposals"] = 1


def test_complete_offline_search_produces_reviewable_patch_and_seals_holdout(config, tmp_path):
    recorded(config, tmp_path)
    root = tmp_path / "run"
    state = run(config, root, runtime_factory=FixtureRuntime)
    assert state["status"] == "completed"
    assert state["journal"][0]["accepted"]
    assert "Verify the final output" in (root / "best.patch").read_text()
    assert (root / "report.md").exists()
    assert all("claweval:holdout" not in tasks for _, tasks in FixtureRuntime.calls)
    labels = [label for label, _ in FixtureRuntime.calls]
    assert labels[2:8] == ["000-0-baseline", "000-0-candidate", "000-1-candidate", "000-1-baseline",
                           "000-2-baseline", "000-2-candidate"]
    packet = read_json(root / "attempts/000/proposer-input.json")
    assert "claweval:select" not in json.dumps(packet)
    results = final_test(config, root, runtime_factory=FixtureRuntime)
    assert results["candidate"]["pro"][0]["score"] == 1
    assert read_json(root / "final-summary.json")["pro"]["claweval"]["delta"] == 1
    assert "Final holdout" in (root / "report.md").read_text()
    with pytest.raises(ValueError, match="already"):
        final_test(config, root, runtime_factory=FixtureRuntime)
    with pytest.raises(ValueError, match="sealed"):
        run(config, root, resume=True, runtime_factory=FixtureRuntime)


def test_infrastructure_failure_keeps_frontier_and_resume_does_not_reuse_partial_scores(config, tmp_path):
    recorded(config, tmp_path)
    root = tmp_path / "run"
    FixtureRuntime.fail_label = "0-candidate"
    with pytest.raises(InvalidEvaluation):
        run(config, root, runtime_factory=FixtureRuntime)
    state = read_json(root / "state.json")
    assert state["frontier"] == "seed"
    assert state["journal"][0]["status"] == "invalid_evaluation"
    assert not (root / "best.patch").read_text()
    FixtureRuntime.fail_label = None
    count = len(FixtureRuntime.calls)
    state = run(config, root, resume=True, runtime_factory=FixtureRuntime)
    assert state["status"] == "completed"
    assert len(FixtureRuntime.calls) == count  # Interrupted proposal consumed its budget.


def test_resume_rejects_changed_inputs_or_tampered_evaluator(config, tmp_path):
    recorded(config, tmp_path)
    root = tmp_path / "run"
    run(config, root, runtime_factory=FixtureRuntime)
    changed = deepcopy(config)
    changed["models"]["pro"]["revision"] = "different"
    with pytest.raises(ValueError, match="inputs changed"):
        run(changed, root, resume=True, runtime_factory=FixtureRuntime)
    (root / "evaluator/benchmarks/claweval/README.md").write_text("Changed evaluator")
    with pytest.raises(ValueError, match="evaluator changed"):
        run(config, root, resume=True, runtime_factory=FixtureRuntime)


def test_smoke_gate_prevents_expensive_selection(config, tmp_path):
    recorded(config, tmp_path)
    proposal = Path(config["proposer"]["recorded"][0])
    body = read_json(proposal)
    body["files"][config["allowed_files"][0]] = "Keep working carefully.\n"
    proposal.write_text(json.dumps(body))
    root = tmp_path / "run"
    state = run(config, root, runtime_factory=FixtureRuntime)
    assert state["journal"][0]["status"] == "smoke_rejected"
    assert len(FixtureRuntime.calls) == 2


def test_failed_final_test_still_prevents_holdout_search(config, tmp_path):
    recorded(config, tmp_path)
    root = tmp_path / "run"
    run(config, root, runtime_factory=FixtureRuntime)
    FixtureRuntime.fail_label = "final"
    with pytest.raises(InvalidEvaluation):
        final_test(config, root, runtime_factory=FixtureRuntime)
    with pytest.raises(ValueError, match="already"):
        final_test(config, root, runtime_factory=FixtureRuntime)


def test_hard_kill_does_not_reset_elapsed_budget(config, tmp_path):
    import time
    from harness_evolution.config import write_json

    recorded(config, tmp_path)
    root = tmp_path / "run"
    run(config, root, runtime_factory=FixtureRuntime)
    state = read_json(root / "state.json")
    state["active_since"] = time.time() - config["policy"]["max_wall_seconds"] - 1
    write_json(root / "state.json", state)
    with pytest.raises(TimeoutError, match="budget exhausted"):
        run(config, root, resume=True, runtime_factory=FixtureRuntime)
    assert read_json(root / "state.json")["status"] == "budget_exhausted"
