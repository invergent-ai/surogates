"""build_report renders test scores, eval commands, deltas, and the tree."""
from types import SimpleNamespace as N

from surogates.arbor.prompts import build_report


def _run(meta):
    return N(meta=meta, trunk_branch="research/trunk")


def test_report_includes_eval_commands_and_delta():
    run = _run({
        "objective": "maximize F1", "metric_direction": "maximize",
        "test_baseline_score": 0.50, "test_trunk_score": 0.61,
        "eval_cmd": "python eval.py --split dev",
        "eval_cmd_test": "python eval.py --split test",
    })
    nodes = [
        N(node_key="ROOT", status="pending", depth=0, score=None,
          hypothesis="o", insight="root", parent_key=None),
        N(node_key="1", status="merged", depth=1, score=0.6,
          hypothesis="idea", insight="good", parent_key="ROOT"),
    ]
    out = build_report(run, nodes)
    assert "python eval.py --split test" in out
    assert "+0.11" in out or "0.11" in out
    assert "## Held-out test" in out and "## Tree" in out
    assert "## Eval commands" in out


def test_report_delta_na_without_scores():
    run = _run({"objective": "o", "metric_direction": "maximize"})
    nodes = [N(node_key="ROOT", status="pending", depth=0, score=None,
               hypothesis="o", insight=None, parent_key=None)]
    out = build_report(run, nodes)
    assert "delta: n/a" in out
