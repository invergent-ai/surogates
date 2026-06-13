# Arbor Research Missions v2 — Method Fidelity & Steering — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the working-but-minimal v1 research engine and restore the parts of Arbor's *method* that v1 deliberately stubbed — LLM-synthesis insight backprop, a convergence detector with Exploit/Combine/Leap interventions, the hard-gated ideation skill, a real baseline (INIT) action, HITL steering, search-scout related-work, the board ticker, and final-report polish.

**Architecture:** Pure additive layer on v1 (branch `research-missions`). New code: `surogates/arbor/convergence.py` and synthesis helpers in `surogates/arbor/propagate.py`; new tool actions and wiring in the existing `surogates/tools/builtin/arbor.py`, `surogates/harness/loop_arbor.py`, and `surogates/harness/loop_mission_evaluator.py`; two new skill bundles. Determinism stays in tools; the LLM-synthesis and convergence *interventions* are fail-open (a failure degrades to v1 behavior, never breaks the loop).

**Tech Stack:** Python 3.12, SQLAlchemy 2 async, Pydantic v2, pytest + pytest-asyncio + testcontainers. Test runner: `.venv/bin/pytest` from `/work/surogates` (the ambient `python` is the surogate-ops venv — do not use it).

**Read first:** the completed v1 (`surogates/arbor/`, `surogates/tools/builtin/arbor.py`, `surogates/harness/loop_arbor.py`), spec §6 v2 in `docs/superpowers/specs/2026-06-12-arbor-research-missions-design.md`, and the Arbor sources being ported: `study/Arbor/src/coordinator/tools/tree_ops.py:518-597` (synthesis), `study/Arbor/src/coordinator/convergence.py:72-344` (detector), `study/Arbor/src/skills/idea_drafting.md` + `first_principles_probe.md`.

**Conventions (hard rules):**
- Run tests with `.venv/bin/pytest` from `/work/surogates`.
- Commit messages: conventional (`feat:`/`test:`/`refactor:`), NO task/step numbers, NO Co-Authored-By trailers.
- Fail-open discipline: every LLM call in a hot path (harvest, synthesis) must `try/except` and degrade to the deterministic v1 result — never raise into the loop.

## Progress Tracker

> Updated before every commit. `[ ]` pending · `[~]` in progress · `[x]` done.

- [x] Task 1 — LLM-synthesis insight backprop (`synthesize_insight` + `propagate_insights_llm` + `idea_tree(action=propagate)` + wire into prune/merge/record)
- [x] Task 2 — Convergence detector module (`surogates/arbor/convergence.py`)
- [x] Task 3 — Wire convergence into harvest digest + evaluator feedback
- [x] Task 4 — INIT fallback: real `dispatch_experiments(action="baseline")`
- [x] Task 5 — Final-report polish (test deltas, eval commands, convergence note)
- [x] Task 6 — Parallel dispatch hardening + multi-node harvest test
- [~] Task 7 — `arbor-ideate` skill bundle + coordinator hard-gate reference
- [ ] Task 8 — `arbor-merge-discipline` skill + HITL/search-scout/board prose + `hitl_mode` in constraints block

---

## File structure

```
New:
surogates/arbor/convergence.py          # ConvergenceConfig/Signal/Detector over idea_nodes rows
skills/research/arbor-ideate/SKILL.md    # hard-gated ideation (PI mindset, probe block, 4-line format)
skills/research/arbor-merge-discipline/SKILL.md  # DECIDE doctrine, combine recipe, report-uses-TEST

Modified:
surogates/arbor/propagate.py             # +synthesize_insight (LLM) +propagate_insights_llm
surogates/tools/builtin/arbor.py         # idea_tree(action=propagate); LLM synthesis in prune/merge/record; real baseline action; hitl_mode surfaced
surogates/arbor/store.py                 # constraints_block shows hitl_mode + convergence line
surogates/harness/loop_arbor.py          # harvest appends convergence intervention to the digest
surogates/harness/loop_mission_evaluator.py  # research feedback carries convergence stats
skills/research/arbor-coordinator/SKILL.md   # load arbor-ideate (hard gate); HITL modes; read_board; spawn search-scout
skills/research/arbor-executor/SKILL.md      # share_note FAIL/RESULT to the board

Tests:
tests/test_arbor_synthesis.py            # synthesize_insight + propagate_insights_llm (mocked llm)
tests/test_arbor_convergence.py          # detector levels, velocity, exhausted parents, format
tests/integration/test_arbor_v2_tools.py # propagate action, baseline action, related_work, multi-node
tests/integration/test_arbor_harvest_convergence.py  # harvest digest carries intervention
```

Dependency order: T1 and T2 are independent; T3 depends on T2; T4–T6 independent; T7–T8 (skills) last. Implement 1→8.

---

### Task 1: LLM-synthesis insight backprop

v1 only concat-propagates at harvest (deterministic, crash-safe). v2 adds Arbor's LLM synthesis (`tree_ops.py:518-597`) inside the tool-call paths, where an LLM client is available and a failure can degrade gracefully.

**Files:**
- Modify: `surogates/arbor/propagate.py`
- Modify: `surogates/tools/builtin/arbor.py` (prune, merge-finalize, record_from_task, new `propagate` action)
- Test: `tests/test_arbor_synthesis.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_arbor_synthesis.py
"""Unit tests for LLM-synthesis insight backprop (mocked LLM client)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from surogates.arbor.propagate import propagate_insights_llm, synthesize_insight


class _FakeLLM:
    """Minimal OpenAI-style client: returns a canned assistant message."""

    def __init__(self, text="SYNTH: retrieval is the bottleneck"):
        self._text = text
        self.calls = 0

        async def _create(**kwargs):
            self.calls += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=self._text)
                )]
            )

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))


class _RaisingLLM:
    def __init__(self):
        async def _create(**kwargs):
            raise RuntimeError("provider down")
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))


@pytest.mark.asyncio
async def test_synthesize_insight_returns_text():
    out = await synthesize_insight(
        _FakeLLM(), "test-model",
        node_label="ROOT (objective: maximize F1)",
        child_insights=["- [1, done] retrieval helps", "- [2, pruned] tuning lr did not"],
    )
    assert "SYNTH" in out


@pytest.mark.asyncio
async def test_synthesize_insight_fails_open_to_none():
    out = await synthesize_insight(
        _RaisingLLM(), "m", node_label="x", child_insights=["- [1] y"],
    )
    assert out is None


@pytest.mark.asyncio
async def test_synthesize_insight_none_without_children():
    out = await synthesize_insight(_FakeLLM(), "m", node_label="x", child_insights=[])
    assert out is None  # nothing to synthesize


class _StubStore:
    """Two-level tree: ROOT <- 1, 2 (children of ROOT)."""

    def __init__(self):
        self.nodes = {
            "ROOT": SimpleNamespace(node_key="ROOT", parent_key=None,
                                    hypothesis="obj", insight=None, status="pending", score=None),
            "1": SimpleNamespace(node_key="1", parent_key="ROOT",
                                 hypothesis="h1", insight="retrieval helps", status="done", score=0.5),
            "2": SimpleNamespace(node_key="2", parent_key="ROOT",
                                 hypothesis="h2", insight="lr tuning fails", status="pruned", score=0.2),
        }
        self.writes: list[tuple[str, str]] = []

    async def list_nodes(self, run_id):
        return list(self.nodes.values())

    async def get_node(self, run_id, key):
        return self.nodes[key]

    async def update_node(self, run_id, key, **fields):
        if "insight" in fields:
            self.writes.append((key, fields["insight"]))
            self.nodes[key].insight = fields["insight"]


@pytest.mark.asyncio
async def test_propagate_insights_llm_synthesizes_ancestors():
    store, llm = _StubStore(), _FakeLLM()
    n = await propagate_insights_llm(store, "run", "1", llm_client=llm, model="m")
    # ROOT is the only ancestor of "1"; it has children with insights -> synthesized.
    assert n == 1
    assert any(key == "ROOT" and "SYNTH" in ins for key, ins in store.writes)


@pytest.mark.asyncio
async def test_propagate_insights_llm_noop_without_llm():
    store = _StubStore()
    n = await propagate_insights_llm(store, "run", "1", llm_client=None, model=None)
    assert n == 0 and store.writes == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_arbor_synthesis.py -q`
Expected: `ImportError: cannot import name 'propagate_insights_llm'`

- [ ] **Step 3: Implement in `surogates/arbor/propagate.py`** (append after `concat_propagate`)

```python
import logging

logger = logging.getLogger(__name__)

_SYNTH_SYSTEM = (
    "You are a research insight synthesizer. Given insights from child "
    "experiments, produce a concise summary that captures the key learnings, "
    "patterns, and actionable conclusions. Be specific about what works and "
    "what doesn't. Keep it under 200 words."
)


async def synthesize_insight(
    llm_client, model: str | None, *, node_label: str, child_insights: list[str],
) -> str | None:
    """LLM-synthesize one node's insight from its children (port of
    tree_ops.py:533-571). Fails open to None: no client/model, no children,
    or any provider error returns None so the caller keeps the prior insight."""
    if llm_client is None or not model or not child_insights:
        return None
    joined = "\n".join(child_insights)
    try:
        resp = await llm_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYNTH_SYSTEM},
                {"role": "user", "content": (
                    f"{node_label}\n\nChildren insights:\n{joined}\n\n"
                    "Synthesize these into a concise research insight."
                )},
            ],
            max_tokens=600, temperature=0.0,
        )
        msg = resp.choices[0].message
        text = (getattr(msg, "content", None) or "").strip()
        return text or None
    except Exception:
        logger.warning("research: insight synthesis failed (continuing)", exc_info=True)
        return None


async def propagate_insights_llm(
    store, run_id, node_key: str, *, llm_client, model: str | None,
) -> int:
    """Walk node_key's ancestors (parent -> root); at each, synthesize an
    insight from its children's insights and persist it. Returns the number
    of ancestors updated. No-ops (returns 0) when synthesis is unavailable."""
    if llm_client is None or not model:
        return 0
    nodes = await store.list_nodes(run_id)
    by_key = {n.node_key: n for n in nodes}
    children: dict[str, list] = {}
    for n in nodes:
        if n.parent_key:
            children.setdefault(n.parent_key, []).append(n)
    if node_key not in by_key:
        return 0

    updated = 0
    cur = by_key[node_key].parent_key
    while cur is not None and cur in by_key:
        ancestor = by_key[cur]
        parts = []
        for c in children.get(cur, []):
            if c.insight:
                score = f" (score={c.score})" if c.score is not None else ""
                parts.append(f"- [{c.node_key}, {c.status}{score}]: {c.insight}")
        if parts:
            label = (
                "This is the ROOT node — produce a global research insight summary."
                if ancestor.parent_key is None
                else f"This is node {ancestor.node_key} (hypothesis: {ancestor.hypothesis})."
            )
            summary = await synthesize_insight(
                llm_client, model, node_label=label, child_insights=parts,
            )
            if summary:
                await store.update_node(run_id, cur, insight=summary)
                updated += 1
        cur = ancestor.parent_key
    return updated
```

- [ ] **Step 4: Run to verify the unit tests pass**

Run: `.venv/bin/pytest tests/test_arbor_synthesis.py -q`
Expected: 5 passed.

- [ ] **Step 5: Wire LLM synthesis into the tool paths** in `surogates/tools/builtin/arbor.py`.

(a) Add a `propagate` action to the `idea_tree` schema enum (find `"requeue", "report"` in `_IDEA_TREE_SCHEMA.parameters["properties"]["action"]["enum"]` and add `"propagate"`).

(b) In `_idea_tree_handler`, add the action branch (place it just before the `report` branch):

```python
        if action == "propagate":
            node_key = arguments.get("node_key")
            if not node_key:
                return json.dumps({"error": "propagate requires node_key"})
            from surogates.arbor.propagate import propagate_insights_llm
            n = await propagate_insights_llm(
                store, run_id, node_key,
                llm_client=kwargs.get("llm_client"), model=kwargs.get("model"),
            )
            return json.dumps({"ok": True, "ancestors_synthesized": n})
```

(c) In `_record_from_task`, after the fold, synthesize up the chain. Replace the final two lines:

```python
    folded = await fold_task_into_node(
        store, run_id, node.node_key, task, llm_client=None, model=None,
    )
    return json.dumps(folded)
```

with:

```python
    folded = await fold_task_into_node(
        store, run_id, node.node_key, task, llm_client=None, model=None,
    )
    from surogates.arbor.propagate import propagate_insights_llm
    await propagate_insights_llm(
        store, run_id, node.node_key,
        llm_client=kwargs.get("llm_client"), model=kwargs.get("model"),
    )
    return json.dumps(folded)
```

(d) In `_idea_tree_handler` `prune` branch, after `pruned = await store.prune(...)`, synthesize up from the pruned node:

```python
        if action == "prune":
            node_key = arguments.get("node_key")
            if not node_key:
                return json.dumps({"error": "prune requires node_key"})
            pruned = await store.prune(
                run_id, node_key, arguments.get("reason") or "no reason given",
            )
            from surogates.arbor.propagate import propagate_insights_llm
            await propagate_insights_llm(
                store, run_id, node_key,
                llm_client=kwargs.get("llm_client"), model=kwargs.get("model"),
            )
            return json.dumps({"pruned": pruned})
```

(e) In `_merge_experiment_handler`, after the successful merge writes `test_trunk_score` and sets node `merged` (just before building `result`), synthesize:

```python
        await store.update_node(run_id, node_key, status="merged")
        from surogates.arbor.propagate import propagate_insights_llm
        await propagate_insights_llm(
            run_id and store, run_id, node_key,
            llm_client=kwargs.get("llm_client"), model=kwargs.get("model"),
        )
        result: dict[str, Any] = {"merged": True, "test_score": score}
```

Note: write `store` (not `run_id and store`) — the snippet above shows the call shape; the correct first arg is `store`. Use:
```python
        await propagate_insights_llm(
            store, run_id, node_key,
            llm_client=kwargs.get("llm_client"), model=kwargs.get("model"),
        )
```

- [ ] **Step 6: Run the existing tool tests to confirm no regression**

Run: `.venv/bin/pytest tests/integration/test_arbor_tools.py tests/test_arbor_routing.py -q`
Expected: all pass (the merge/prune tests use a fake sandbox and pass `llm_client` absent → synthesis no-ops via the `model`/`llm_client` guard).

- [ ] **Step 7: Commit**

```bash
git add surogates/arbor/propagate.py surogates/tools/builtin/arbor.py tests/test_arbor_synthesis.py docs/superpowers/plans/2026-06-13-arbor-research-missions-v2.md
git commit -m "feat: LLM-synthesis insight backprop in arbor tool paths"
```

---

### Task 2: Convergence detector module

Port `study/Arbor/src/coordinator/convergence.py:72-344` to operate over `idea_nodes` rows (ordered by `completed_at`) instead of Arbor's in-memory `IdeaTree`. Pure module, no I/O beyond a store read.

**Files:**
- Create: `surogates/arbor/convergence.py`
- Test: `tests/test_arbor_convergence.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_arbor_convergence.py
"""Unit tests for the convergence detector (operates over node snapshots)."""
from __future__ import annotations

from types import SimpleNamespace

from surogates.arbor.convergence import (
    ConvergenceConfig,
    detect_convergence,
    format_intervention,
)


def _node(key, status, score, parent="ROOT"):
    return SimpleNamespace(node_key=key, status=status, score=score, parent_key=parent)


def _nodes(scores):
    """ROOT + one done child per score (completion order = list order)."""
    out = [SimpleNamespace(node_key="ROOT", status="pending", score=None, parent_key=None)]
    out += [_node(str(i + 1), "done", s) for i, s in enumerate(scores)]
    return out


def test_no_signal_before_min_experiments():
    cfg = ConvergenceConfig(min_experiments=4)
    sig = detect_convergence(_nodes([0.5, 0.5]), trunk_score=0.5, meta={}, config=cfg)
    assert sig is None


def test_warn_after_three_non_improving():
    cfg = ConvergenceConfig(min_experiments=4, warn_after=3, force_after=5, stop_after=8)
    # trunk 0.50; four done all <= trunk -> 4 consecutive non-improving -> warn (or force at 5).
    nodes = _nodes([0.50, 0.49, 0.48, 0.47])
    sig = detect_convergence(nodes, trunk_score=0.50, meta={"metric_direction": "maximize"}, config=cfg)
    assert sig is not None and sig.level in ("warn", "paradigm_shift")


def test_paradigm_shift_then_stop_escalation():
    cfg = ConvergenceConfig(min_experiments=4, warn_after=3, force_after=5, stop_after=8)
    five = _nodes([0.5, 0.4, 0.4, 0.4, 0.4, 0.4])  # 5 consecutive non-improving
    assert detect_convergence(five, trunk_score=0.5, meta={}, config=cfg).level == "paradigm_shift"
    eight = _nodes([0.5] + [0.4] * 8)
    assert detect_convergence(eight, trunk_score=0.5, meta={}, config=cfg).level == "stop"


def test_improvement_resets_counter():
    cfg = ConvergenceConfig(min_experiments=4, warn_after=3)
    # last node improves on trunk -> consecutive resets -> no signal.
    nodes = _nodes([0.4, 0.4, 0.4, 0.6])
    assert detect_convergence(nodes, trunk_score=0.5, meta={"metric_direction": "maximize"}, config=cfg) is None


def test_format_intervention_levels():
    from surogates.arbor.convergence import ConvergenceSignal
    warn = ConvergenceSignal(level="warn", reason="r", velocity=0.0,
                             consecutive_non_improving=3, exhausted_parents=[],
                             suggested_actions=["Leap", "Combine"])
    text = format_intervention(warn)
    assert "CONVERGENCE WARNING" in text and "Leap" in text
    stop = ConvergenceSignal(level="stop", reason="r", velocity=0.0,
                             consecutive_non_improving=8, exhausted_parents=["1"],
                             suggested_actions=["finalize"])
    assert "STOP" in format_intervention(stop)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_arbor_convergence.py -q`
Expected: `ModuleNotFoundError: No module named 'surogates.arbor.convergence'`

- [ ] **Step 3: Implement `surogates/arbor/convergence.py`**

```python
"""Convergence detection for the research coordinator loop.

Port of study/Arbor/src/coordinator/convergence.py operating over a
snapshot of ``idea_nodes`` rows (ordered by completion) instead of
Arbor's in-memory IdeaTree. Pure functions: the caller supplies the
node list, the current dev trunk score, and the run meta; the detector
returns a signal (or None) and formats the intervention text injected
into the harvest digest and the evaluator feedback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel


class ConvergenceConfig(BaseModel):
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
        known = set(cls.model_fields)
        src = {}
        for k, v in (meta or {}).items():
            # accept convergence_<field> keys from run meta
            if k.startswith("convergence_") and k[len("convergence_"):] in known:
                src[k[len("convergence_"):]] = v
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
        return (getattr(n, "completed_at", None) or "", n.node_key)
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
    for n in _done(nodes):
        if n.parent_key:
            by_parent.setdefault(n.parent_key, []).append(n)
    out = []
    for parent, kids in by_parent.items():
        if parent == "ROOT" or len(kids) < config.parent_exhaustion_count:
            continue
        recent = kids[-config.parent_exhaustion_count:]
        if all(not _is_meaningful_improvement(c.score, trunk_score, meta, config) for c in recent):
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
    """Return a signal when the run has plateaued, else None."""
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
    exhausted = _exhausted_parents(nodes, trunk_score, meta, config)
    direction = _direction(meta)
    dir_str = "higher is better" if direction == "maximize" else "lower is better"
    reason = (
        f"{n} consecutive experiments have not meaningfully improved the trunk "
        f"score ({trunk_score}, {dir_str}). Velocity: "
        f"{_velocity(nodes, trunk_score, meta, config):.6f} per experiment."
    )
    return ConvergenceSignal(
        level=level, reason=reason,
        velocity=_velocity(nodes, trunk_score, meta, config),
        consecutive_non_improving=n, exhausted_parents=exhausted,
        suggested_actions=_suggestions(level, exhausted),
    )


def format_intervention(signal: ConvergenceSignal) -> str:
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
            "different from everything explored; if overriding, state why it breaks the plateau.",
        ]
    return "\n".join(lines)
```

- [ ] **Step 4: Run the unit tests**

Run: `.venv/bin/pytest tests/test_arbor_convergence.py -q`
Expected: 5 passed. (If `test_paradigm_shift_then_stop_escalation` miscounts, verify `_done` ordering: the `_nodes` helper assigns keys "1".."N" in completion order, and `completed_at` is absent in the stub so it falls back to node_key — note "10" sorts after "9" lexically only for ≤9 nodes here, fine; if you extend beyond 9 nodes use zero-padded keys in the test.)

- [ ] **Step 5: Commit**

```bash
git add surogates/arbor/convergence.py tests/test_arbor_convergence.py docs/superpowers/plans/2026-06-13-arbor-research-missions-v2.md
git commit -m "feat: add arbor convergence detector (plateau levels + interventions)"
```

---

### Task 3: Wire convergence into the harvest digest + evaluator feedback

The detector is injected at the two points the coordinator reads: the pre-LLM harvest digest (every wake) and the research evaluator's `needs_revision` feedback.

**Files:**
- Modify: `surogates/harness/loop_arbor.py` (`_harvest_research_inner`)
- Modify: `surogates/arbor/evaluator_policy.py` (`research_prompt_block` gains a convergence line)
- Modify: `surogates/harness/loop_mission_evaluator.py` (pass the signal text into the block)
- Test: `tests/integration/test_arbor_harvest_convergence.py` (new)

- [ ] **Step 1: Write the failing integration test**

```python
# tests/integration/test_arbor_harvest_convergence.py
"""The harvest digest carries a convergence intervention once the run plateaus."""
from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio

from surogates.arbor.store import ResearchStore
from surogates.harness.loop_arbor import ArborHarvestMixin


class _Harness(ArborHarvestMixin):
    """Minimal host exposing the attributes the mixin reads."""

    def __init__(self, session_factory):
        self._session_factory = session_factory
        self._sandbox_pool = None
        self._llm = None
        self.events: list[str] = []

        class _Store:
            async def emit_event(_s, sid, etype, payload):
                self.events.append(getattr(etype, "value", str(etype)))
        self._store = _Store()


@pytest_asyncio.fixture(loop_scope="session")
async def plateaued_run(session_factory, seeded_org_and_session):
    org_id, mission_id, session_id = seeded_org_and_session
    store = ResearchStore(session_factory)
    run_id = await store.create_run(
        org_id=org_id, mission_id=mission_id, session_id=session_id,
        agent_id="agent-x", repo_path="/workspace/repo",
        trunk_branch="research/cv/trunk", branch_prefix="research/cv",
        objective="maximize F1",
        meta_overrides={"convergence_min_experiments": 4, "convergence_warn_after": 3},
    )
    # trunk_score is a machine key; set it + four non-improving done children.
    await store.set_meta(run_id, {"trunk_score": 0.50}, allow_machine_keys=True)
    for i, s in enumerate([0.50, 0.49, 0.48, 0.47], start=1):
        await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis=f"h{i}")
        await store.update_node(run_id, str(i), status="done", score=s, insight="no gain")
    return store, run_id, session_id


@pytest.mark.asyncio(loop_scope="session")
async def test_harvest_digest_includes_convergence(session_factory, plateaued_run):
    store, run_id, session_id = plateaued_run
    # A 5th experiment just finished (its task terminal); make node 5 running+linked.
    from surogates.db.models import Task
    org = (await store.get_run(run_id)).org_id
    await store.add_node(run_id, org_id=org, parent_key="ROOT", hypothesis="h5")
    async with session_factory() as db:
        t = Task(org_id=org, parent_session_id=session_id, agent_def_name="arbor-executor",
                 goal="g", status="done", max_attempts=1,
                 result_metadata={"score": 0.46, "insight": "still no gain"})
        db.add(t); await db.commit(); await db.refresh(t); tid = t.id
    await store.update_node(run_id, "5", status="running", task_id=tid)

    harness = _Harness(session_factory)
    session = type("S", (), {"id": session_id, "model": None,
                             "config": {"active_research_run_id": str(run_id)}})()
    messages: list[dict] = []
    await harness.maybe_harvest_research(session, messages)

    digest = next((m["content"] for m in messages if "[research harvest]" in m["content"]), "")
    assert digest
    assert "CONVERGENCE" in digest  # plateau intervention injected
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_arbor_harvest_convergence.py -q`
Expected: FAIL — `assert "CONVERGENCE" in digest` (harvest does not yet inject it).

- [ ] **Step 3: Inject convergence into the harvest digest.** In `surogates/harness/loop_arbor.py`, in `_harvest_research_inner`, after `constraints = await store.constraints_block(run_id)` and before building `content`, add:

```python
        # Convergence intervention (fail-open): plateaued runs get an
        # Exploit/Combine/Leap nudge appended to the digest.
        intervention = ""
        try:
            from surogates.arbor.convergence import (
                ConvergenceConfig, detect_convergence, format_intervention,
            )
            meta = run.meta or {}
            signal = detect_convergence(
                await store.list_nodes(run_id),
                trunk_score=meta.get("trunk_score"),
                meta=meta, config=ConvergenceConfig.from_meta(meta),
            )
            if signal is not None:
                intervention = "\n\n" + format_intervention(signal)
                await self._store.emit_event(
                    session.id, EventType.RESEARCH_CONVERGED,
                    {"run_id": str(run_id), "level": signal.level,
                     "consecutive_non_improving": signal.consecutive_non_improving},
                )
        except Exception:
            logger.warning("research: convergence check failed (continuing)", exc_info=True)
```

Then change the `content` assembly to append it:

```python
        content = (
            "[research harvest]\n"
            + json.dumps(digests, default=str)
            + "\n\n"
            + constraints
            + intervention
        )
```

- [ ] **Step 4: Add the convergence stat to the evaluator feedback.** In `surogates/arbor/evaluator_policy.py`, extend `research_prompt_block` with an optional `convergence: str | None = None` param appended when present:

```python
def research_prompt_block(
    *, constraints_block: str, cycles_spent: int, max_cycles: int,
    convergence: str | None = None,
) -> str:
    block = (
        "## Research run state (machine-written; the ONLY trusted scores)\n"
        f"cycles: {cycles_spent}/{max_cycles}\n\n"
        f"{constraints_block}\n\n"
        "Verdict guidance: needs_revision feedback must name the next "
        "structural step (expand X / prune Y / paradigm shift / merge / "
        "finalize). Never accept prose claims of improvement — only "
        "meta.test_trunk_score counts. Selecting on the held-out test split "
        "outside merge_experiment is a blocked outcome."
    )
    if convergence:
        block += "\n\n" + convergence
    return block
```

Then in `surogates/harness/loop_mission_evaluator.py`, where `research_prompt_block(...)` is called, compute the signal and pass it:

```python
        from surogates.arbor.evaluator_policy import research_prompt_block
        conv_text = None
        try:
            from surogates.arbor.convergence import (
                ConvergenceConfig, detect_convergence, format_intervention,
            )
            meta = research_run.meta or {}
            sig = detect_convergence(
                await research_store.list_nodes(research_run.id),
                trunk_score=meta.get("trunk_score"),
                meta=meta, config=ConvergenceConfig.from_meta(meta),
            )
            conv_text = format_intervention(sig) if sig else None
        except Exception:
            conv_text = None
        user_prompt = user_prompt + "\n\n" + research_prompt_block(
            constraints_block=await research_store.constraints_block(research_run.id),
            cycles_spent=research_cycles, max_cycles=research_max_cycles,
            convergence=conv_text,
        )
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/integration/test_arbor_harvest_convergence.py tests/test_arbor_evaluator_policy.py tests/integration/test_arbor_evaluator_wiring.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add surogates/harness/loop_arbor.py surogates/arbor/evaluator_policy.py surogates/harness/loop_mission_evaluator.py tests/integration/test_arbor_harvest_convergence.py docs/superpowers/plans/2026-06-13-arbor-research-missions-v2.md
git commit -m "feat: inject convergence interventions into harvest digest and evaluator feedback"
```

---

### Task 4: INIT fallback — real `dispatch_experiments(action="baseline")`

v1 returns an error for `action="baseline"`. v2 implements it: when intake did not supply a baseline, the coordinator dispatches a baseline experiment that measures the unmodified repo on the dev split; harvest writes `baseline_score`. (`test_baseline_score` still comes only from intake or a merge — the baseline action measures dev only.)

**Files:**
- Modify: `surogates/tools/builtin/arbor.py` (`_dispatch_experiments_handler` baseline branch; harvest of a baseline node)
- Modify: `surogates/harness/loop_arbor.py` (fold a baseline node into `baseline_score`)
- Test: `tests/integration/test_arbor_v2_tools.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_arbor_v2_tools.py
"""v2 tool behaviours: baseline action, propagate action, related_work, multi-node."""
from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from surogates.arbor.store import ResearchStore
from surogates.tools.builtin.arbor import (
    _dispatch_experiments_handler, _idea_tree_handler,
)
from .test_arbor_tools import FakeSandboxPool, _StubSessionStore, _fake_spawn


@pytest_asyncio.fixture(loop_scope="session")
async def base_run(session_factory, seeded_org_and_session):
    org_id, mission_id, session_id = seeded_org_and_session
    store = ResearchStore(session_factory)
    run_id = await store.create_run(
        org_id=org_id, mission_id=mission_id, session_id=session_id,
        agent_id="agent-x", repo_path="/workspace/repo",
        trunk_branch="research/v2/trunk", branch_prefix="research/v2",
        objective="o",
    )
    await store.set_meta(run_id, {"eval_cmd": "python eval.py --split dev"})
    kwargs = {
        "session_factory": session_factory,
        "session_config": {"active_research_run_id": str(run_id)},
        "session_id": str(session_id), "sandbox_pool": FakeSandboxPool(),
        "session_store": _StubSessionStore(), "tenant": object(), "redis": object(),
    }
    return store, run_id, org_id, kwargs


@pytest.mark.asyncio(loop_scope="session")
async def test_baseline_action_spawns_a_baseline_experiment(base_run):
    store, run_id, org_id, kwargs = base_run
    spawn = AsyncMock(side_effect=_fake_spawn(
        kwargs["session_factory"], org_id, uuid.UUID(kwargs["session_id"]),
    ))
    with patch("surogates.tasks.service.create_task_and_spawn", new=spawn):
        out = json.loads(await _dispatch_experiments_handler(
            {"node_keys": [], "action": "baseline"}, **kwargs,
        ))
    assert out.get("baseline_dispatched") is True
    # A BASELINE node exists, running, on the trunk branch.
    node = await store.get_node(run_id, "BASELINE")
    assert node.status == "running" and node.task_id is not None
    brief = spawn.call_args.kwargs["goal"]
    assert "baseline" in brief.lower() and "do not modify" in brief.lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_related_work_is_writable_via_update(base_run):
    store, run_id, org_id, kwargs = base_run
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="h")
    out = json.loads(await _idea_tree_handler(
        {"action": "update", "node_key": "1",
         "fields": {"related_work": "see Smith et al. 2024"}}, **kwargs,
    ))
    assert out["ok"]
    assert (await store.get_node(run_id, "1")).related_work == "see Smith et al. 2024"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_arbor_v2_tools.py -q`
Expected: FAIL — baseline action still returns the v1 "captured at mission creation" error.

- [ ] **Step 3: Implement the baseline action.** In `surogates/tools/builtin/arbor.py`, replace the v1 baseline guard:

```python
    if arguments.get("action") == "baseline":
        return json.dumps({"error": (
            "baselines are captured at mission creation ..."
        )})
```

with a real dispatch:

```python
    if arguments.get("action") == "baseline":
        return await _dispatch_baseline(store, run_id, kwargs)
```

and add the helper (near `_dispatch_experiments_handler`):

```python
async def _dispatch_baseline(store, run_id, kwargs) -> str:
    """Measure the unmodified repo on the dev split. Creates the trunk and a
    BASELINE node, spawns an executor whose brief forbids source edits; harvest
    writes meta.baseline_score from its reported score."""
    from surogates.tasks.service import TaskSpawnError, create_task_and_spawn

    run = await store.get_run(run_id)
    meta = run.meta or {}
    if not meta.get("eval_cmd"):
        return json.dumps({"error": "meta.eval_cmd is not set — set it before baseline"})
    if meta.get("baseline_score") is not None:
        return json.dumps({"error": "baseline_score already set"})
    # Reuse a fixed node key so the run has exactly one baseline.
    existing = {n.node_key for n in await store.list_nodes(run_id)}
    if "BASELINE" in existing:
        return json.dumps({"error": "a baseline experiment already exists"})

    worktree = "/workspace/.arbor/worktrees/BASELINE"
    out = await _sandbox_sh(kwargs, (
        f"cd {run.repo_path} && "
        f"(git rev-parse --verify {run.trunk_branch} >/dev/null 2>&1 "
        f"|| git branch {run.trunk_branch}) && "
        f"git worktree add --detach {worktree} {run.trunk_branch} 2>&1"
    ))
    if "fatal" in (out or "").lower():
        return json.dumps({"error": f"baseline worktree failed: {out[:300]}"})

    brief = (
        "[Baseline experiment]\n\n"
        f"Measure the UNMODIFIED repo on the dev split. Worktree: {worktree}.\n"
        "DO NOT MODIFY any source — run the eval as-is and report the number.\n"
        f"Eval (dev): {meta['eval_cmd']}\n\n"
        "Finish with worker_complete(metadata={\"node_key\": \"BASELINE\", "
        "\"score\": <float dev score>, \"insight\": \"baseline\", "
        "\"result\": \"baseline measured\"})."
    )
    # Insert the BASELINE node directly (fixed key, not the auto-incrementing add).
    from surogates.db.models import IdeaNode
    async with kwargs["session_factory"]() as db:
        db.add(IdeaNode(
            org_id=run.org_id, run_id=run_id, node_key="BASELINE",
            parent_key="ROOT", depth=1, hypothesis="baseline (unmodified repo)",
            status="pending",
        ))
        await db.commit()
    try:
        result = await create_task_and_spawn(
            goal=brief, context=None, agent_def_name="arbor-executor",
            max_attempts=1, parent_ids=[],
            parent_session_id=UUID(str(kwargs["session_id"])),
            org_id=run.org_id, mission_id=run.mission_id,
            session_store=kwargs["session_store"], session_factory=kwargs["session_factory"],
            redis=kwargs.get("redis"), tenant=kwargs.get("tenant"),
        )
    except TaskSpawnError as exc:
        return json.dumps({"error": f"failed to spawn baseline: {exc}"})
    await store.update_node(
        run_id, "BASELINE", status="running",
        task_id=UUID(result["task_id"]), code_ref=run.trunk_branch,
        dispatched_at=_naive_utcnow(),
    )
    return json.dumps({"baseline_dispatched": True})
```

- [ ] **Step 4: Fold the baseline node into `baseline_score`.** In `surogates/harness/loop_arbor.py` `fold_task_into_node`, after computing `fields` and before `update_node`, special-case the baseline node so its score lands in meta (the baseline is dev-split, written to `baseline_score`, which is NOT a machine key so the deterministic harvest may write it through a dedicated store call):

Add, right after `await store.update_node(run_id, node_key, **fields)`:

```python
    # The baseline experiment's dev score seeds meta.baseline_score so the
    # coordinator and convergence detector have a reference before any merge.
    if node_key == "BASELINE" and fields.get("score") is not None:
        try:
            await store.set_meta(run_id, {"baseline_score": fields["score"]})
        except Exception:
            logger.warning("research: baseline_score write failed (continuing)", exc_info=True)
```

(`baseline_score` is in `META_KEYS` and NOT in `MACHINE_KEYS`, so `set_meta` accepts it without `allow_machine_keys`.)

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/integration/test_arbor_v2_tools.py -q`
Expected: 2 passed. (`_fake_spawn` inserts a real Task so the BASELINE node's `task_id` FK holds.)

- [ ] **Step 6: Commit**

```bash
git add surogates/tools/builtin/arbor.py surogates/harness/loop_arbor.py tests/integration/test_arbor_v2_tools.py docs/superpowers/plans/2026-06-13-arbor-research-missions-v2.md
git commit -m "feat: real baseline (INIT fallback) dispatch action for research runs"
```

---

### Task 5: Final-report polish

Enrich `build_report` with per-node held-out deltas, the run's eval commands, and a convergence summary line, so `REPORT.md` is a complete research artifact.

**Files:**
- Modify: `surogates/arbor/prompts.py` (`build_report`)
- Test: `tests/test_arbor_report.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_arbor_report.py
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
        N(node_key="ROOT", status="pending", depth=0, score=None, hypothesis="o", insight="root", parent_key=None),
        N(node_key="1", status="merged", depth=1, score=0.6, hypothesis="idea", insight="good", parent_key="ROOT"),
    ]
    out = build_report(run, nodes)
    assert "python eval.py --split test" in out
    assert "+0.11" in out or "0.11" in out          # test delta baseline->trunk
    assert "## Held-out test" in out and "## Tree" in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_arbor_report.py -q`
Expected: FAIL on the eval-command / delta assertions (v1 report omits them).

- [ ] **Step 3: Enhance `build_report`** in `surogates/arbor/prompts.py`. After the "Held-out test (authoritative)" lines, insert a delta + eval-command block. Replace:

```python
        "## Held-out test (authoritative)",
        f"- baseline: {meta.get('test_baseline_score')}",
        f"- final trunk: {meta.get('test_trunk_score')} ({direction})",
        "",
```

with:

```python
        "## Held-out test (authoritative)",
        f"- baseline: {meta.get('test_baseline_score')}",
        f"- final trunk: {meta.get('test_trunk_score')} ({direction})",
        f"- delta: {_fmt_delta(meta.get('test_baseline_score'), meta.get('test_trunk_score'))}",
        "",
        "## Eval commands",
        f"- dev:  {meta.get('eval_cmd', '(unset)')}",
        f"- test: {meta.get('eval_cmd_test', '(unset)')}",
        "",
```

and add the helper at module level:

```python
def _fmt_delta(baseline, trunk) -> str:
    if baseline is None or trunk is None:
        return "n/a"
    d = trunk - baseline
    return f"{d:+.4g}"
```

- [ ] **Step 4: Run the test + the existing report smoke**

Run: `.venv/bin/pytest tests/test_arbor_report.py -q`
Expected: 1 passed. Then `.venv/bin/pytest tests/integration/test_arbor_smoke.py -q` → still passes.

- [ ] **Step 5: Commit**

```bash
git add surogates/arbor/prompts.py tests/test_arbor_report.py docs/superpowers/plans/2026-06-13-arbor-research-missions-v2.md
git commit -m "feat: enrich research REPORT.md with deltas and eval commands"
```

---

### Task 6: Parallel dispatch hardening + multi-node harvest

v1 dispatches a batch in a loop and harvests one node at a time. Harden: a mid-batch worktree failure must not strand the already-spawned siblings (v1 returns them in `dispatched`), and harvest must fold several terminal siblings in one wake. This task adds the regression coverage and a guard that already-running nodes are never re-dispatched in a batch with duplicates.

**Files:**
- Modify: `surogates/tools/builtin/arbor.py` (dedupe node_keys in dispatch)
- Test: extend `tests/integration/test_arbor_v2_tools.py`

- [ ] **Step 1: Write the failing test** (append to `tests/integration/test_arbor_v2_tools.py`)

```python
@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_rejects_duplicate_node_keys(base_run):
    store, run_id, org_id, kwargs = base_run
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="h")
    out = json.loads(await _dispatch_experiments_handler(
        {"node_keys": ["1", "1"]}, **kwargs,
    ))
    assert "duplicate" in out["error"]


@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_two_nodes_spawns_two_worktrees(base_run):
    store, run_id, org_id, kwargs = base_run
    await store.set_meta(run_id, {"max_parallel": 2})
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="a")
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="b")
    spawn = AsyncMock(side_effect=_fake_spawn(
        kwargs["session_factory"], org_id, uuid.UUID(kwargs["session_id"]),
    ))
    with patch("surogates.tasks.service.create_task_and_spawn", new=spawn):
        out = json.loads(await _dispatch_experiments_handler(
            {"node_keys": ["1", "2"]}, **kwargs,
        ))
    assert out["dispatched"] == ["1", "2"]
    pool = kwargs["sandbox_pool"]
    adds = [i for (_, _, i) in pool.calls if "git worktree add" in i]
    assert len(adds) == 2
    assert spawn.await_count == 2
```

- [ ] **Step 2: Run to verify the duplicate test fails**

Run: `.venv/bin/pytest tests/integration/test_arbor_v2_tools.py -k duplicate -q`
Expected: FAIL — v1 does not reject duplicates (it would try to dispatch node 1 twice).

- [ ] **Step 3: Add the dedupe guard.** In `_dispatch_experiments_handler`, right after `node_keys = list(arguments.get("node_keys") or [])` and the empty check, add:

```python
        if len(set(node_keys)) != len(node_keys):
            return json.dumps({"error": "duplicate node_keys in one dispatch batch"})
```

- [ ] **Step 4: Run both tests**

Run: `.venv/bin/pytest tests/integration/test_arbor_v2_tools.py -q`
Expected: all pass (multi-node spawns two worktrees + two tasks).

- [ ] **Step 5: Commit**

```bash
git add surogates/tools/builtin/arbor.py tests/integration/test_arbor_v2_tools.py docs/superpowers/plans/2026-06-13-arbor-research-missions-v2.md
git commit -m "feat: harden parallel dispatch (reject duplicate node keys) + multi-node coverage"
```

---

### Task 7: `arbor-ideate` skill + coordinator hard-gate reference

Split the hard-gated ideation playbook out of the coordinator skill into its own bundle, ported from `study/Arbor/src/skills/idea_drafting.md` + `first_principles_probe.md`.

**Files:**
- Create: `skills/research/arbor-ideate/SKILL.md`
- Modify: `skills/research/arbor-coordinator/SKILL.md` (IDEATE step loads it as a hard gate)

- [ ] **Step 1: Write `skills/research/arbor-ideate/SKILL.md`**

Frontmatter (match the bundle convention — `name`, `description`, `version: 1.0.0`, `license: MIT`, `tags`):

```markdown
---
name: arbor-ideate
description: "Hard-gated ideation for an Arbor research run. Load at the START of every IDEATE round, before drafting any hypothesis. Enforces the PI mindset (mechanism over knob), the four-question first-principles probe, the kill-filter, and the four-line hypothesis format. Ported from Arbor's idea_drafting + first_principles_probe."
version: 1.0.0
license: MIT
tags: [research, ideate, arbor]
---

# Arbor Ideate — Hard-Gated Idea Drafting

<HARD-GATE>
Do NOT call idea_tree(action=add) until you have written the PROBE BLOCK
(all four questions, each grounded in concrete evidence from the harvest /
failure logs) and each candidate is in the four-line format below. Skipping
the probe is the default LLM failure mode — it is forbidden here.
</HARD-GATE>

## 1. Mindset: PI, not engineer
- HOW, not HOW MUCH — change the algorithm/representation/objective, not a knob.
- 10×, not 10% — if it worked completely, would it move a CLASS of failures by ≥1σ?
- Mechanism is a noun — name a new component/stage/strategy. "Be more robust" is a goal, not a mechanism. If you write "improve / better / handle X better", stop — you have not named a mechanism.

## 2. First-Principles Probe (MANDATORY, before any candidate)
Answer all four in your reasoning trace, each citing concrete evidence (log lines, failure case ids, code refs):
1. **First principles** — the bottleneck CLASS (wrong retrieval / wrong reasoning / wrong stopping / wrong representation / wrong objective / wrong action space / wrong credit assignment). Cite ≥2 concrete cases; if you can't, OBSERVE more.
2. **Hidden assumption** — a load-bearing assumption the trunk silently relies on; what opens up if dropped.
3. **Elephant** — the ugly problem everyone quietly works around.
4. **Hamming** — if the bottleneck were solved, would the benchmark meaningfully change? If "not really", redo (1).

Paste a PROBE BLOCK into your reasoning trace before listing ideas.

## 3. Kill-filter
Drop any candidate that is a knob/prompt tweak, restates the trunk, or fails the 2-page-paper test.

## 4. Four-line hypothesis (the idea_tree(add) format)
```
Mechanism: <the new component/stage/strategy — a noun>
Hypothesis: <causal story: doing X changes Y because Z>
Observable: <the dev-split signal that confirms/refutes it>
Conflicts: <what trunk assumption or prior node it challenges, or "none">
```
idea_tree(add) machine-warns when these four markers are missing — treat the warning as a rejection and rewrite.
```

- [ ] **Step 2: Update the coordinator skill.** In `skills/research/arbor-coordinator/SKILL.md`, change the IDEATE step to a hard gate:

Replace:
```
2. **IDEATE** — load the `arbor-ideate` skill (hard gate), then add 1-3
```
with:
```
2. **IDEATE** — you MUST `skill_view("arbor-ideate")` and complete its
   PROBE BLOCK before adding any node. Then add 1-3
```

- [ ] **Step 3: Validate frontmatter parses**

Run:
```bash
.venv/bin/python -c "
from surogates.tools.loader import _parse_skill_frontmatter
p='skills/research/arbor-ideate/SKILL.md'
d=_parse_skill_frontmatter(open(p).read(),'arbor-ideate')
assert d['name']=='arbor-ideate' and d['description']
print('arbor-ideate frontmatter ok')
"
```
Expected: `arbor-ideate frontmatter ok`

- [ ] **Step 4: Commit**

```bash
git add skills/research/arbor-ideate/SKILL.md skills/research/arbor-coordinator/SKILL.md docs/superpowers/plans/2026-06-13-arbor-research-missions-v2.md
git commit -m "feat: add arbor-ideate hard-gated ideation skill"
```

---

### Task 8: `arbor-merge-discipline` skill + HITL / search-scout / board prose + `hitl_mode` in constraints block

The remaining v2 method items are coordinator/executor *steering*, which the spec scopes as prompt-level for v2. Ship the merge-discipline skill, weave HITL + search-scout + board-ticker guidance into the existing skills, and surface `hitl_mode` in the constraints block so the coordinator always sees the active mode.

**Files:**
- Create: `skills/research/arbor-merge-discipline/SKILL.md`
- Modify: `skills/research/arbor-coordinator/SKILL.md` (HITL modes; read_board; spawn search-scout)
- Modify: `skills/research/arbor-executor/SKILL.md` (share_note FAIL/RESULT)
- Modify: `surogates/arbor/store.py` (`constraints_block` shows `hitl_mode`)
- Test: extend `tests/integration/test_arbor_store.py`

- [ ] **Step 1: Write the failing test** (append to `tests/integration/test_arbor_store.py`)

```python
@pytest.mark.asyncio(loop_scope="session")
async def test_constraints_block_shows_hitl_mode(research_run):
    store, run_id, _ = research_run
    await store.set_meta(run_id, {"hitl_mode": "review"})
    block = await store.constraints_block(run_id)
    assert "HITL mode: review" in block
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_arbor_store.py -k hitl -q`
Expected: FAIL — the block doesn't render `hitl_mode`.

- [ ] **Step 3: Surface `hitl_mode`** in `surogates/arbor/store.py` `constraints_block`. In the `### BUDGET & DISCIPLINE` lines, add the mode to the budget line:

Replace:
```python
            f"- cycles: {cycles}/{meta.get('max_cycles')}"
            f" | depth cap: {meta.get('max_tree_depth')}"
            f" | max parallel: {meta.get('max_parallel')}",
```
with:
```python
            f"- cycles: {cycles}/{meta.get('max_cycles')}"
            f" | depth cap: {meta.get('max_tree_depth')}"
            f" | max parallel: {meta.get('max_parallel')}"
            f" | HITL mode: {meta.get('hitl_mode', 'auto')}",
```

- [ ] **Step 4: Run the test**

Run: `.venv/bin/pytest tests/integration/test_arbor_store.py -k hitl -q`
Expected: 1 passed.

- [ ] **Step 5: Write `skills/research/arbor-merge-discipline/SKILL.md`**

```markdown
---
name: arbor-merge-discipline
description: "DECIDE-phase doctrine for an Arbor research run: when to merge, prune, combine, or finalize; how the held-out merge gate works; and the rule that the final report uses TEST scores. Load before deciding what to do with completed experiments."
version: 1.0.0
license: MIT
tags: [research, decide, merge, arbor]
---

# Arbor Merge Discipline — DECIDE Doctrine

## When to merge
Call `merge_experiment(start, node_key)` for a `done` node whose dev score beats
trunk. The tool re-runs the held-out test eval ITSELF (you cannot pass a score);
poll `merge_experiment(status, node_key)` on a later turn. A merge writes
`test_trunk_score` and advances trunk — later experiments branch from the new HEAD.

## When to prune
`idea_tree(prune, node_key, reason=<the lesson>)` for dead ends. The reason is
backpropagated, so write the transferable lesson, not "didn't work".

## Combine (ensemble)
When several diverse nodes each help a different failure class, propose a child
hypothesis that ensembles/blends them — often the highest-leverage move once
single ideas plateau (see the convergence intervention's "Combine" suggestion).

## Search-scout (related work)
Before merging a validated winner, optionally `delegate_task` a short web search
("related work for <mechanism>"), then record it with
`idea_tree(update, node_key, fields={"related_work": "<refs>"})`. Run it async —
do not block the cycle on it.

## Finalize — the report uses TEST, not dev
On budget/convergence-stop/target: merge the best, `idea_tree(report)` (test
scores are authoritative there), then spawn ONE report task that creates the
artifact and completes with metadata `{"report": true}`. The mission is only
satisfied once a machine-written test improvement AND that report task exist.
```

- [ ] **Step 6: Weave HITL + board prose into the coordinator skill.** In `skills/research/arbor-coordinator/SKILL.md`, add a section after "The laws":

```markdown
## Steering (HITL) and the board

- The constraints block shows the active **HITL mode**:
  - `auto` — proceed without asking.
  - `direction` — at the START of each IDEATE round, `ask_user_question` for the
    direction to explore before adding nodes.
  - `review` — `ask_user_question` for approval before `dispatch_experiments`
    and before finalizing a `merge_experiment`.
- Read your executors' board notes with `read_board` during OBSERVE — they post
  `FAIL` (dead ends) and `RESULT` (candidate outcomes) notes you can reuse across
  the tree. Load `arbor-merge-discipline` before the DECIDE phase.
```

- [ ] **Step 7: Add board guidance to the executor skill.** In `skills/research/arbor-executor/SKILL.md`, add to the REPORT step:

```markdown
- If your coordination board is available (`share_note`), post a `FAIL` note for
  a dead end (with why) or a `RESULT` note for a candidate outcome
  (`outcome=… | evidence=<the check you ran> | risk=…`) so sibling experiments
  and the coordinator can reuse it. This is in addition to worker_complete, not
  a replacement.
```

- [ ] **Step 8: Validate both new skills parse + run the store suite**

Run:
```bash
.venv/bin/python -c "
from surogates.tools.loader import _parse_skill_frontmatter
for n in ('arbor-merge-discipline',):
    d=_parse_skill_frontmatter(open(f'skills/research/{n}/SKILL.md').read(), n)
    assert d['name']==n and d['description']; print(n,'ok')
"
.venv/bin/pytest tests/integration/test_arbor_store.py -q
```
Expected: `arbor-merge-discipline ok`; store suite passes.

- [ ] **Step 9: Commit**

```bash
git add skills/research/ surogates/arbor/store.py tests/integration/test_arbor_store.py docs/superpowers/plans/2026-06-13-arbor-research-missions-v2.md
git commit -m "feat: add arbor-merge-discipline skill, HITL/board steering, hitl_mode in constraints"
```

---

## Final verification (after all tasks)

```bash
.venv/bin/pytest tests/ -q -k "arbor or task_service" --no-header 2>&1 | tail -3
.venv/bin/pytest tests/harness/ tests/missions/ tests/integration/missions/ -q 2>&1 | tail -3
```
Expected: all arbor v1+v2 tests green; harness/missions suites green (the 4 pre-existing `worker_notify` + 6 `agent_resolver` stale-signature failures from v1 remain, unrelated).

## Self-review checklist (run after implementation)

1. **Spec §6 v2 coverage:** LLM-synthesis backprop (T1) ✓ convergence + Exploit/Combine/Leap (T2,T3) ✓ arbor-ideate hard-gate + 4-line check (T7; the machine check shipped in v1) ✓ HITL direction/review (T8) ✓ record/requeue polish (T1 record synthesis) ✓ parallel dispatch hardening (T6) ✓ search-scout related_work (T4 update field + T8 prose) ✓ board FAIL/RESULT ticker (T8) ✓ arbor-merge-discipline skill (T8) ✓ final-report polish (T5) ✓ INIT fallback baseline (T4) ✓. Real-run prompt tuning is an out-of-band activity, not a code task — excluded by design.
2. **Fail-open:** every LLM/convergence call in harvest and the evaluator is wrapped try/except and degrades to v1 behavior. Confirm no bare `await` on synthesis/convergence in a hot path.
3. **Machine-key safety:** `baseline_score` (T4) and `trunk_score` writes go through the right `set_meta` path (`baseline_score` is not a machine key; `trunk_score`/`test_trunk_score` are and stay merge-only).

## Execution

Plan complete and saved. Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task with review between tasks (superpowers:subagent-driven-development).
2. **Inline Execution** — task-by-task in this session with checkpoints (superpowers:executing-plans).
