"""DB CRUD for research runs and idea nodes.

Mirrors :class:`~surogates.missions.store.MissionStore`'s shape. All
``meta`` writes are per-key ``jsonb_set`` UPDATEs — never a
read-modify-write of the whole blob — so concurrent writers (the
coordinator's ``set_meta`` and the merge gate's score write) cannot
clobber each other.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.arbor.models import BUDGET_SPENDING_STATUSES, TERMINAL_NODE_STATUSES
from surogates.db.models import IdeaNode, ResearchRun

# Closed meta key set, ported from Arbor's ``tree.meta``
# (study/Arbor/src/coordinator/idea_tree.py:124-141) plus the run-config
# keys the spec adds (§4.3). Unknown keys are rejected at write time.
META_KEYS: frozenset[str] = frozenset({
    "objective",
    "baseline_score", "trunk_score",
    "test_baseline_score", "test_trunk_score",
    "eval_cmd", "eval_cmd_test", "eval_timeout",
    "eval_retries", "eval_retry_base_delay", "eval_retry_max_delay",
    "metric_direction", "dataset_info",
    "protected_paths", "required_outputs",
    "max_cycles", "max_tree_depth", "max_parallel",
    "merge_threshold", "hitl_mode",
    "convergence_window", "convergence_min_delta",
    "merge_eval",
})

# Writable ONLY by merge_experiment / the baseline-record path
# (``allow_machine_keys=True``). ``idea_tree(set_meta)`` from the LLM
# cannot fake research progress by writing these.
MACHINE_KEYS: frozenset[str] = frozenset({
    "test_baseline_score", "test_trunk_score", "trunk_score", "merge_eval",
})

DEFAULT_META: dict[str, Any] = {
    "metric_direction": "maximize",
    "max_cycles": 20,
    "max_tree_depth": 3,
    "max_parallel": 2,
    "merge_threshold": 0.0,
    "eval_timeout": 1800,
    "eval_retries": 1,
    "eval_retry_base_delay": 10,
    "eval_retry_max_delay": 60,
    "hitl_mode": "auto",
}

_MUTABLE_NODE_FIELDS: frozenset[str] = frozenset({
    "status", "score", "insight", "result", "code_ref",
    "related_work", "task_id", "dispatched_at", "completed_at",
})
_TERMINAL: frozenset[str] = frozenset(TERMINAL_NODE_STATUSES)


def _naive_utcnow() -> datetime:
    """Naive UTC timestamp matching the schema's ``TIMESTAMP WITHOUT TIME
    ZONE`` columns (the convention across the platform's ORM models)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _node_sort_key(node: "IdeaNode") -> tuple[int, list[int], str]:
    """Order ROOT first, then by dotted-decimal key numerically
    ("2" before "10", "1.2" before "1.10")."""
    if node.node_key == "ROOT":
        return (0, [], "")
    try:
        return (1, [int(p) for p in node.node_key.split(".")], node.node_key)
    except ValueError:
        return (1, [], node.node_key)


def _ordered(nodes: list["IdeaNode"]) -> list["IdeaNode"]:
    return sorted(nodes, key=_node_sort_key)


class ResearchStoreError(Exception):
    """Base for research store errors."""


class MetaKeyError(ResearchStoreError):
    """Unknown meta key, or a machine key written from the LLM path."""


class NodeStateError(ResearchStoreError):
    """Illegal node state transition (e.g. mutating a pruned node)."""


def is_improvement(
    candidate: float | None, reference: float | None, direction: str,
) -> bool:
    """Direction-aware comparison (port of idea_tree.py:193-198).

    No candidate is never an improvement; a candidate with no reference
    (no baseline yet) always is.
    """
    if candidate is None:
        return False
    if reference is None:
        return True
    if direction == "minimize":
        return candidate < reference
    return candidate > reference


class ResearchStore:
    """Async CRUD for ``research_runs`` and ``idea_nodes``."""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    # -- runs ---------------------------------------------------------------

    async def create_run(
        self, *, org_id: UUID, mission_id: UUID, session_id: UUID,
        agent_id: str, repo_path: str, trunk_branch: str,
        branch_prefix: str, objective: str,
        meta_overrides: dict[str, Any] | None = None,
    ) -> UUID:
        """Insert a run + its ROOT node; return the run id."""
        meta = dict(DEFAULT_META)
        meta["objective"] = objective
        for key, value in (meta_overrides or {}).items():
            if key not in META_KEYS:
                raise MetaKeyError(f"unknown meta key: {key}")
            meta[key] = value
        async with self._sf() as db:
            run = ResearchRun(
                org_id=org_id, mission_id=mission_id, session_id=session_id,
                agent_id=agent_id, repo_path=repo_path,
                trunk_branch=trunk_branch, branch_prefix=branch_prefix,
                meta=meta,
            )
            db.add(run)
            await db.flush()
            run_id = run.id
            db.add(IdeaNode(
                org_id=org_id, run_id=run_id, node_key="ROOT",
                parent_key=None, depth=0, hypothesis=objective,
            ))
            await db.commit()
            return run_id

    async def get_run(self, run_id: UUID) -> ResearchRun:
        async with self._sf() as db:
            run = await db.get(ResearchRun, run_id)
            if run is None:
                raise ResearchStoreError(f"research run {run_id} not found")
            db.expunge(run)
            return run

    async def get_run_for_mission(self, mission_id: UUID) -> ResearchRun | None:
        async with self._sf() as db:
            run = await db.scalar(
                select(ResearchRun).where(ResearchRun.mission_id == mission_id)
            )
            if run is not None:
                db.expunge(run)
            return run

    async def get_run_for_session(self, session_id: UUID) -> ResearchRun | None:
        async with self._sf() as db:
            run = await db.scalar(
                select(ResearchRun)
                .where(ResearchRun.session_id == session_id)
                .order_by(ResearchRun.created_at.desc())
                .limit(1)
            )
            if run is not None:
                db.expunge(run)
            return run

    async def set_run_status(self, run_id: UUID, status: str) -> None:
        async with self._sf() as db:
            await db.execute(
                update(ResearchRun)
                .where(ResearchRun.id == run_id)
                .values(status=status)
            )
            await db.commit()

    async def set_meta(
        self, run_id: UUID, values: dict[str, Any],
        *, allow_machine_keys: bool = False,
    ) -> None:
        """Per-key ``jsonb_set`` writes — no whole-blob read-modify-write.

        Each value is serialized to JSON text and cast back to ``jsonb``
        in SQL, so numbers, strings, lists, and dicts all round-trip
        with their native JSON type.
        """
        for key in values:
            if key not in META_KEYS:
                raise MetaKeyError(f"unknown meta key: {key}")
            if key in MACHINE_KEYS and not allow_machine_keys:
                raise MetaKeyError(
                    f"meta key {key!r} is machine-written only "
                    "(merge_experiment / baseline path)"
                )
        if not values:
            return
        stmt = text(
            "UPDATE research_runs "
            "SET meta = jsonb_set(meta, CAST(:path AS text[]), "
            "CAST(:val AS jsonb), true) "
            "WHERE id = :run_id"
        )
        async with self._sf() as db:
            for key, value in values.items():
                await db.execute(stmt, {
                    # asyncpg maps a Python list to the SQL ``text[]`` the
                    # CAST declares; a single-element path sets a top-level key.
                    "path": [key],
                    "val": json.dumps(value),
                    "run_id": run_id,
                })
            await db.commit()

    # -- nodes --------------------------------------------------------------

    async def add_node(
        self, run_id: UUID, *, org_id: UUID, parent_key: str, hypothesis: str,
    ) -> IdeaNode:
        """Allocate the next dotted-decimal child key under ``parent_key``."""
        async with self._sf() as db:
            parent = await db.scalar(
                select(IdeaNode).where(
                    IdeaNode.run_id == run_id, IdeaNode.node_key == parent_key,
                )
            )
            if parent is None:
                raise ResearchStoreError(f"parent node {parent_key!r} not found")
            if parent.status in _TERMINAL:
                raise NodeStateError(
                    f"parent {parent_key!r} is {parent.status}"
                )
            prefix = "" if parent_key == "ROOT" else f"{parent_key}."
            siblings = (await db.execute(
                select(IdeaNode.node_key).where(
                    IdeaNode.run_id == run_id,
                    IdeaNode.parent_key == parent_key,
                )
            )).scalars().all()
            next_ordinal = 1 + max(
                (int(k.rsplit(".", 1)[-1]) for k in siblings), default=0,
            )
            depth = 1 if parent_key == "ROOT" else parent.depth + 1
            node = IdeaNode(
                org_id=org_id, run_id=run_id,
                node_key=f"{prefix}{next_ordinal}",
                parent_key=parent_key, depth=depth, hypothesis=hypothesis,
            )
            db.add(node)
            await db.commit()
            await db.refresh(node)
            db.expunge(node)
            return node

    async def get_node(self, run_id: UUID, node_key: str) -> IdeaNode:
        async with self._sf() as db:
            node = await db.scalar(
                select(IdeaNode).where(
                    IdeaNode.run_id == run_id, IdeaNode.node_key == node_key,
                )
            )
            if node is None:
                raise ResearchStoreError(f"node {node_key!r} not found")
            db.expunge(node)
            return node

    async def list_nodes(self, run_id: UUID) -> list[IdeaNode]:
        async with self._sf() as db:
            nodes = (await db.execute(
                select(IdeaNode)
                .where(IdeaNode.run_id == run_id)
                .order_by(IdeaNode.node_key)
            )).scalars().all()
            for node in nodes:
                db.expunge(node)
            return list(nodes)

    async def update_node(
        self, run_id: UUID, node_key: str, **fields: Any,
    ) -> None:
        unknown = set(fields) - _MUTABLE_NODE_FIELDS
        if unknown:
            raise ResearchStoreError(f"immutable/unknown node fields: {unknown}")
        async with self._sf() as db:
            node = await db.scalar(
                select(IdeaNode).where(
                    IdeaNode.run_id == run_id, IdeaNode.node_key == node_key,
                )
            )
            if node is None:
                raise ResearchStoreError(f"node {node_key!r} not found")
            if node.status in _TERMINAL and fields.get("status") != node.status:
                raise NodeStateError(
                    f"node {node_key!r} is terminal ({node.status})"
                )
            for key, value in fields.items():
                setattr(node, key, value)
            if fields.get("status") in ("done", "failed", "merged"):
                node.completed_at = node.completed_at or _naive_utcnow()
            await db.commit()

    async def prune(self, run_id: UUID, node_key: str, reason: str) -> list[str]:
        """Recursively prune ``node_key`` and its subtree. Returns the keys."""
        async with self._sf() as db:
            nodes = (await db.execute(
                select(IdeaNode).where(IdeaNode.run_id == run_id)
            )).scalars().all()
            by_key = {n.node_key: n for n in nodes}
            if node_key not in by_key:
                raise ResearchStoreError(f"node {node_key!r} not found")
            doomed = [
                k for k in by_key
                if k == node_key or k.startswith(node_key + ".")
            ]
            for key in doomed:
                node = by_key[key]
                if node.status in _TERMINAL:
                    continue
                node.status = "pruned"
                tag = f"[Pruned: {reason}]"
                node.insight = f"{node.insight}\n{tag}" if node.insight else tag
            await db.commit()
            return doomed

    # -- accounting ---------------------------------------------------------

    async def cycles_spent(self, run_id: UUID) -> int:
        async with self._sf() as db:
            count = await db.scalar(
                select(func.count(IdeaNode.id)).where(
                    IdeaNode.run_id == run_id,
                    IdeaNode.status.in_(BUDGET_SPENDING_STATUSES),
                    IdeaNode.node_key != "ROOT",
                )
            )
            return int(count or 0)

    async def in_flight_count(self, run_id: UUID) -> int:
        async with self._sf() as db:
            count = await db.scalar(
                select(func.count(IdeaNode.id)).where(
                    IdeaNode.run_id == run_id,
                    IdeaNode.status == "running",
                )
            )
            return int(count or 0)

    # -- rendering ----------------------------------------------------------

    async def constraints_block(self, run_id: UUID) -> str:
        """The anti-amnesia artifact, re-read every IDEATE.

        Port of ``idea_tree.py:358-435`` — TREE SHAPE / ROOT INSIGHT /
        PRUNED LESSONS / VALIDATED FINDINGS / BUDGET, rendered from the
        DB rows. Immune to context compression: it is regenerated from
        the system of record on every wake.
        """
        run = await self.get_run(run_id)
        nodes = await self.list_nodes(run_id)
        by_key = {n.node_key: n for n in nodes}
        meta = run.meta or {}

        def _first_line(node: IdeaNode) -> str:
            return (node.hypothesis or "").splitlines()[0][:100] if node.hypothesis else ""

        lines: list[str] = ["## RESEARCH CONSTRAINTS", "", "### TREE SHAPE"]
        for node in _ordered(nodes):
            indent = "  " * (0 if node.node_key == "ROOT" else node.depth)
            score = f" score={node.score}" if node.score is not None else ""
            lines.append(
                f"{indent}- {node.node_key} [{node.status}]{score} {_first_line(node)}"
            )

        root = by_key.get("ROOT")
        lines += ["", "### ROOT INSIGHT",
                  (root.insight if root and root.insight else "(none yet)")]

        pruned = [n for n in nodes if n.status == "pruned" and n.insight]
        lines += ["", "### PRUNED LESSONS"]
        lines += [f"- {n.node_key}: {n.insight.splitlines()[-1][:200]}"
                  for n in pruned] or ["(none)"]

        merged = [n for n in nodes if n.status == "merged"]
        lines += ["", "### VALIDATED FINDINGS"]
        lines += [f"- {n.node_key} score={n.score}: {(n.insight or '')[:200]}"
                  for n in merged] or ["(none)"]

        cycles = await self.cycles_spent(run_id)
        lines += [
            "", "### BUDGET & DISCIPLINE",
            f"- cycles: {cycles}/{meta.get('max_cycles')}"
            f" | depth cap: {meta.get('max_tree_depth')}"
            f" | max parallel: {meta.get('max_parallel')}",
            f"- scores: baseline={meta.get('baseline_score')}"
            f" trunk(dev)={meta.get('trunk_score')}"
            f" test_baseline={meta.get('test_baseline_score')}"
            f" test_trunk={meta.get('test_trunk_score')}"
            f" ({meta.get('metric_direction')})",
            "- B_dev for iteration; B_test ONLY through merge_experiment.",
        ]
        return "\n".join(lines)
