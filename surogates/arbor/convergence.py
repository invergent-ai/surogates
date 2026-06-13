"""Convergence detection for the research coordinator loop.

Port of ``study/Arbor/src/coordinator/convergence.py`` operating over a
snapshot of ``idea_nodes`` rows (ordered by completion) instead of
Arbor's in-memory ``IdeaTree``. Pure functions: the caller supplies the
node list, the current dev trunk score, and the run meta; the detector
returns a signal (or ``None``) and formats the intervention text that is
injected into the harvest digest and the evaluator feedback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel


class ConvergenceConfig(BaseModel):
    """Plateau-detection thresholds (overridable via ``convergence_*`` meta)."""

    enabled: bool = True
    min_experiments: int = 4
    window_size: int = 5
    improvement_threshold: float = 0.001
    parent_exhaustion_count: int = 3
    warn_after: int = 3
    force_after: int = 5
    stop_after: int = 8

    @classmethod
    def from_meta(cls, meta: dict[str, Any]) -> "ConvergenceConfig":
        """Build from run meta, reading ``convergence_<field>`` keys."""
        known = set(cls.model_fields)
        src: dict[str, Any] = {}
        prefix = "convergence_"
        for key, value in (meta or {}).items():
            if key.startswith(prefix) and key[len(prefix):] in known:
                src[key[len(prefix):]] = value
        return cls(**src)


@dataclass
class ConvergenceSignal:
    level: Literal["warn", "paradigm_shift", "stop"]
    reason: str
    velocity: float
    consecutive_non_improving: int
    exhausted_parents: list[str]
    suggested_actions: list[str] = field(default_factory=list)


def _direction(meta: dict[str, Any]) -> str:
    return (meta or {}).get("metric_direction", "maximize")


def _is_meaningful_improvement(score, trunk_score, meta, config) -> bool:
    if score is None or trunk_score is None:
        return False
    direction = _direction(meta)
    delta = (trunk_score - score) if direction == "minimize" else (score - trunk_score)
    if delta <= 0:
        return False
    threshold = abs(trunk_score) * config.improvement_threshold or config.improvement_threshold
    return delta > threshold


def _done(nodes: list) -> list:
    """Completed scored experiments (exclude ROOT), in completion order.

    Ordered by ``completed_at`` when present, else by node_key — the same
    monotonic order the harvest writes them in.
    """
    done = [
        n for n in nodes
        if getattr(n, "status", None) in ("done", "merged")
        and getattr(n, "score", None) is not None
        and n.node_key != "ROOT"
    ]

    def order(n):
        return (str(getattr(n, "completed_at", "") or ""), n.node_key)

    return sorted(done, key=order)


def _consecutive_non_improving(nodes, trunk_score, meta, config) -> int:
    consecutive = 0
    for node in reversed(_done(nodes)):
        if _is_meaningful_improvement(node.score, trunk_score, meta, config):
            break
        consecutive += 1
    return consecutive


def _velocity(nodes, trunk_score, meta, config) -> float:
    done = _done(nodes)
    if len(done) < 2 or trunk_score is None:
        return 0.0
    direction = _direction(meta)
    window = done[-config.window_size:]
    improvements = [
        max(0.0, (trunk_score - n.score) if direction == "minimize" else (n.score - trunk_score))
        for n in window
    ]
    return (max(improvements) if improvements else 0.0) / max(1, len(window))


def _exhausted_parents(nodes, trunk_score, meta, config) -> list[str]:
    by_parent: dict[str, list] = {}
    for node in _done(nodes):
        if node.parent_key:
            by_parent.setdefault(node.parent_key, []).append(node)
    out: list[str] = []
    for parent, kids in by_parent.items():
        if parent == "ROOT" or len(kids) < config.parent_exhaustion_count:
            continue
        recent = kids[-config.parent_exhaustion_count:]
        if all(
            not _is_meaningful_improvement(c.score, trunk_score, meta, config)
            for c in recent
        ):
            out.append(parent)
    return sorted(out)


def _suggestions(level: str, exhausted: list[str]) -> list[str]:
    if level == "warn":
        return [
            "Switch to a fundamentally different approach family (Leap)",
            "Ensemble/blend existing diverse results (Combine)",
            "Review whether the current approach has hit its ceiling",
        ]
    if level == "paradigm_shift":
        return [
            "MANDATORY: the next idea must use a different approach family",
            f"Do NOT expand these exhausted parents: {exhausted}",
            "Try a different architecture, methodology, or an ensemble (Combine)",
            "If no promising new direction exists, finalize",
        ]
    return [
        "Ensemble the best diverse candidates if not already done (Combine)",
        "Merge the current best and finalize",
        "Override ONLY with a genuinely novel, unexplored direction",
    ]


def detect_convergence(
    nodes: list, *, trunk_score, meta: dict[str, Any], config: ConvergenceConfig,
) -> ConvergenceSignal | None:
    """Return a signal when the run has plateaued, else ``None``."""
    if not config.enabled or trunk_score is None:
        return None
    done = _done(nodes)
    if len(done) < config.min_experiments:
        return None
    n = _consecutive_non_improving(nodes, trunk_score, meta, config)
    if n >= config.stop_after:
        level: Literal["warn", "paradigm_shift", "stop"] = "stop"
    elif n >= config.force_after:
        level = "paradigm_shift"
    elif n >= config.warn_after:
        level = "warn"
    else:
        return None
    velocity = _velocity(nodes, trunk_score, meta, config)
    exhausted = _exhausted_parents(nodes, trunk_score, meta, config)
    direction = _direction(meta)
    dir_str = "higher is better" if direction == "maximize" else "lower is better"
    reason = (
        f"{n} consecutive experiments have not meaningfully improved the trunk "
        f"score ({trunk_score}, {dir_str}). Velocity: {velocity:.6f} per experiment."
    )
    return ConvergenceSignal(
        level=level, reason=reason, velocity=velocity,
        consecutive_non_improving=n, exhausted_parents=exhausted,
        suggested_actions=_suggestions(level, exhausted),
    )


def format_intervention(signal: ConvergenceSignal) -> str:
    """Render the intervention text injected into the coordinator context."""
    header = {
        "warn": "[Warning] CONVERGENCE WARNING",
        "paradigm_shift": "[Alert] CONVERGENCE: PARADIGM SHIFT REQUIRED",
        "stop": "[Critical] CONVERGENCE: STOP RECOMMENDED",
    }[signal.level]
    lines = [f"## {header}", "", signal.reason, ""]
    if signal.exhausted_parents:
        lines += [f"**Exhausted parents** (do NOT expand): {signal.exhausted_parents}", ""]
    lines.append("**Suggested actions:**")
    lines += [f"{i}. {a}" for i, a in enumerate(signal.suggested_actions, 1)]
    if signal.level == "stop":
        lines += [
            "",
            "Override this ONLY with a genuinely novel direction fundamentally "
            "different from everything explored; if overriding, state why it "
            "breaks the plateau.",
        ]
    return "\n".join(lines)
