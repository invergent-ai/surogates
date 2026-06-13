"""Unit tests for the convergence detector (operates over node snapshots)."""
from __future__ import annotations

from types import SimpleNamespace

from surogates.arbor.convergence import (
    ConvergenceConfig,
    ConvergenceSignal,
    detect_convergence,
    format_intervention,
)


def _node(key, status, score, parent="ROOT"):
    return SimpleNamespace(node_key=key, status=status, score=score, parent_key=parent)


def _nodes(scores):
    """ROOT + one done child per score (completion order = list order via key)."""
    out = [SimpleNamespace(node_key="ROOT", status="pending", score=None, parent_key=None)]
    out += [_node(str(i + 1), "done", s) for i, s in enumerate(scores)]
    return out


def test_no_signal_before_min_experiments():
    cfg = ConvergenceConfig(min_experiments=4)
    sig = detect_convergence(_nodes([0.5, 0.5]), trunk_score=0.5, meta={}, config=cfg)
    assert sig is None


def test_warn_after_three_non_improving():
    cfg = ConvergenceConfig(min_experiments=4, warn_after=3, force_after=5, stop_after=8)
    nodes = _nodes([0.50, 0.49, 0.48, 0.47])
    sig = detect_convergence(
        nodes, trunk_score=0.50, meta={"metric_direction": "maximize"}, config=cfg,
    )
    assert sig is not None and sig.level in ("warn", "paradigm_shift")


def test_paradigm_shift_then_stop_escalation():
    cfg = ConvergenceConfig(min_experiments=4, warn_after=3, force_after=5, stop_after=8)
    five = _nodes([0.5, 0.4, 0.4, 0.4, 0.4, 0.4])  # 5 consecutive non-improving
    assert detect_convergence(five, trunk_score=0.5, meta={}, config=cfg).level == "paradigm_shift"
    eight = _nodes([0.5] + [0.4] * 8)
    assert detect_convergence(eight, trunk_score=0.5, meta={}, config=cfg).level == "stop"


def test_improvement_resets_counter():
    cfg = ConvergenceConfig(min_experiments=4, warn_after=3)
    nodes = _nodes([0.4, 0.4, 0.4, 0.6])  # last improves -> resets
    assert detect_convergence(
        nodes, trunk_score=0.5, meta={"metric_direction": "maximize"}, config=cfg,
    ) is None


def test_exhausted_parents_detected():
    cfg = ConvergenceConfig(min_experiments=4, warn_after=3, parent_exhaustion_count=3)
    # ROOT child "1" with three non-improving done children -> exhausted parent "1".
    nodes = [
        SimpleNamespace(node_key="ROOT", status="pending", score=None, parent_key=None),
        _node("1", "done", 0.4, parent="ROOT"),
        _node("1.1", "done", 0.41, parent="1"),
        _node("1.2", "done", 0.40, parent="1"),
        _node("1.3", "done", 0.39, parent="1"),
    ]
    sig = detect_convergence(nodes, trunk_score=0.5, meta={}, config=cfg)
    assert sig is not None and "1" in sig.exhausted_parents


def test_format_intervention_levels():
    warn = ConvergenceSignal(level="warn", reason="r", velocity=0.0,
                             consecutive_non_improving=3, exhausted_parents=[],
                             suggested_actions=["Leap", "Combine"])
    text = format_intervention(warn)
    assert "CONVERGENCE WARNING" in text and "Leap" in text
    stop = ConvergenceSignal(level="stop", reason="r", velocity=0.0,
                             consecutive_non_improving=8, exhausted_parents=["1"],
                             suggested_actions=["finalize"])
    assert "STOP" in format_intervention(stop)


def test_config_from_meta_reads_convergence_keys():
    cfg = ConvergenceConfig.from_meta({"convergence_warn_after": 2, "max_cycles": 9})
    assert cfg.warn_after == 2
