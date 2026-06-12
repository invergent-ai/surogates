"""Arbor research tools: idea_tree, dispatch_experiments, merge_experiment.

The deterministic spine of research missions (spec §4.4). Judgment lives
in the ``arbor-*`` skills; these handlers enforce the guarantees: closed
meta keys, dispatch gates, and the no-score-argument merge gate.

Visibility is coordinator-only and gated on
``session.config['active_research_run_id']`` in
``surogates.orchestrator.worker._filter_effective_tools`` — executors
stay tree-blind (``mle_kaggle.yaml`` "no second shared-state protocol").
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# idea_tree
# ---------------------------------------------------------------------------

_IDEA_TREE_SCHEMA = ToolSchema(
    name="idea_tree",
    description=(
        "Read and mutate this research run's Idea Tree. Actions: "
        "view (format=constraints|compact), add(parent_key, hypothesis), "
        "update(node_key, fields), prune(node_key, reason), "
        "set_meta(values), record_from_task(task_id), "
        "requeue(node_key, reason), report."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [
                "view", "add", "update", "prune", "set_meta",
                "record_from_task", "requeue", "report",
            ]},
            "format": {"type": "string", "enum": ["constraints", "compact"]},
            "parent_key": {"type": "string"},
            "node_key": {"type": "string"},
            "hypothesis": {"type": "string"},
            "fields": {"type": "object"},
            "values": {"type": "object"},
            "task_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["action"],
    },
)

_FOUR_LINE_MARKERS = ("Mechanism:", "Hypothesis:", "Observable:", "Conflicts:")


def _hypothesis_warnings(hypothesis: str) -> list[str]:
    """Machine-warn on non-4-line hypotheses (port of arbor_state.py
    ``validate_hypothesis`` — warn, never block; idea quality is the
    skill's job, not the tool's)."""
    missing = [m for m in _FOUR_LINE_MARKERS if m not in (hypothesis or "")]
    if missing:
        return [f"hypothesis missing {', '.join(missing)} — use the 4-line format"]
    return []


async def _require_run(session_config: dict, session_factory: Any):
    """Resolve the active ResearchStore + run id, or raise ValueError."""
    from surogates.arbor.store import ResearchStore

    raw = (session_config or {}).get("active_research_run_id")
    if not raw:
        raise ValueError("no active research run on this session")
    return ResearchStore(session_factory), UUID(str(raw))


async def _idea_tree_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    from surogates.arbor.store import MetaKeyError, ResearchStoreError

    try:
        store, run_id = await _require_run(
            kwargs.get("session_config") or {}, kwargs["session_factory"],
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    action = arguments.get("action")
    try:
        run = await store.get_run(run_id)

        if action == "view":
            if arguments.get("format") == "compact":
                nodes = await store.list_nodes(run_id)
                rows = [
                    f"{n.node_key}\t{n.status}"
                    f"\t{n.score if n.score is not None else '-'}"
                    f"\t{(n.hypothesis or '').splitlines()[0][:80] if n.hypothesis else ''}"
                    for n in nodes
                ]
                return "key\tstatus\tscore\thypothesis\n" + "\n".join(rows)
            return await store.constraints_block(run_id)

        if action == "add":
            warnings = _hypothesis_warnings(arguments.get("hypothesis") or "")
            meta = run.meta or {}
            parent_key = arguments.get("parent_key")
            if not parent_key:
                return json.dumps({"error": "add requires parent_key"})
            if not (arguments.get("hypothesis") or "").strip():
                return json.dumps({"error": "add requires a non-empty hypothesis"})
            parent = await store.get_node(run_id, parent_key)
            depth_cap = int(meta.get("max_tree_depth", 3))
            if parent.node_key != "ROOT" and parent.depth >= depth_cap:
                return json.dumps({"error": (
                    f"depth cap {depth_cap} reached at {parent.node_key}; "
                    "refine an existing branch or prune"
                )})
            node = await store.add_node(
                run_id, org_id=run.org_id,
                parent_key=parent_key, hypothesis=arguments["hypothesis"],
            )
            out: dict[str, Any] = {"node_key": node.node_key, "depth": node.depth}
            if warnings:
                out["warnings"] = warnings
            return json.dumps(out)

        if action == "update":
            node_key = arguments.get("node_key")
            if not node_key:
                return json.dumps({"error": "update requires node_key"})
            fields = dict(arguments.get("fields") or {})
            # Scores arrive via harvest / merge, never coordinator prose.
            fields.pop("score", None)
            await store.update_node(run_id, node_key, **fields)
            return json.dumps({"ok": True})

        if action == "prune":
            node_key = arguments.get("node_key")
            if not node_key:
                return json.dumps({"error": "prune requires node_key"})
            pruned = await store.prune(
                run_id, node_key, arguments.get("reason") or "no reason given",
            )
            return json.dumps({"pruned": pruned})

        if action == "set_meta":
            try:
                await store.set_meta(run_id, dict(arguments.get("values") or {}))
            except MetaKeyError as exc:
                return json.dumps({"error": str(exc)})
            return json.dumps({"ok": True})

        if action == "record_from_task":
            task_id = arguments.get("task_id")
            if not task_id:
                return json.dumps({"error": "record_from_task requires task_id"})
            return await _record_from_task(store, run_id, task_id, kwargs)

        if action == "requeue":
            node_key = arguments.get("node_key")
            if not node_key:
                return json.dumps({"error": "requeue requires node_key"})
            node = await store.get_node(run_id, node_key)
            if node.status not in ("done", "failed"):
                return json.dumps({"error": f"cannot requeue a {node.status} node"})
            await store.update_node(run_id, node_key, status="pending", task_id=None)
            return json.dumps({
                "ok": True,
                "note": "requeued; the spent cycle is NOT refunded "
                        f"(reason: {arguments.get('reason') or 'unspecified'})",
            })

        if action == "report":
            # Lazy import: prompts.py (incl. build_report) ships with
            # dispatch_experiments; no earlier path exercises this action.
            from surogates.arbor.prompts import build_report

            report = build_report(run, await store.list_nodes(run_id))
            await _persist_workspace_file(
                kwargs, path=".arbor/REPORT.md", content=report,
            )
            return report

        return json.dumps({"error": f"unknown action {action!r}"})
    except ResearchStoreError as exc:
        return json.dumps({"error": str(exc)})


async def _record_from_task(store, run_id, task_id: str, kwargs) -> str:
    """Coordinator's correction channel: fold a Task row by id — reads
    ``Task.result``/``result_metadata`` from the DB, never coordinator prose."""
    from surogates.db.models import Task
    # Lazy import: loop_arbor.py ships with the harvest hook; record is
    # the same deterministic fold the wake hook uses.
    from surogates.harness.loop_arbor import fold_task_into_node

    try:
        tid = UUID(str(task_id))
    except ValueError:
        return json.dumps({"error": f"invalid task_id {task_id!r}"})
    async with kwargs["session_factory"]() as db:
        task = await db.get(Task, tid)
        if task is None:
            return json.dumps({"error": f"task {task_id} not found"})
        db.expunge(task)
    folded = await fold_task_into_node(
        store, run_id, task, llm_client=None, model=None,
    )
    return json.dumps(folded)


async def _persist_workspace_file(kwargs, *, path: str, content: str) -> None:
    """Write a file under ``/workspace`` via the session's sandbox.

    Best-effort: the DB is the source of truth; workspace files are
    audit/display artifacts, so a sandbox hiccup must not fail the tool.
    """
    pool = kwargs.get("sandbox_pool")
    if pool is None:
        return
    from surogates.sandbox.base import default_sandbox_spec

    owner = str(kwargs["session_id"])
    try:
        await pool.ensure(owner, default_sandbox_spec())
        await pool.execute(owner, "write_file", json.dumps({
            "path": f"/workspace/{path}", "content": content,
        }))
    except Exception:
        logger.warning(
            "research: failed to persist /workspace/%s (continuing)", path,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# dispatch_experiments / merge_experiment — real schemas + handlers land in
# their own tasks; minimal stubs keep register() valid and the tool schemas
# present from the moment routing exists.
# ---------------------------------------------------------------------------

_DISPATCH_SCHEMA = ToolSchema(
    name="dispatch_experiments",
    description="(implemented in the dispatch task)",
    parameters={"type": "object", "properties": {}},
)

_MERGE_SCHEMA = ToolSchema(
    name="merge_experiment",
    description="(implemented in the merge task)",
    parameters={"type": "object", "properties": {}},
)


async def _dispatch_experiments_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    return json.dumps({"error": "not implemented"})


async def _merge_experiment_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    return json.dumps({"error": "not implemented"})


def register(registry: ToolRegistry) -> None:
    """Register the three arbor tools. Called once per registry by
    ``tools/runtime.py``."""
    registry.register(
        name="idea_tree", schema=_IDEA_TREE_SCHEMA,
        handler=_idea_tree_handler, toolset="core",
    )
    registry.register(
        name="dispatch_experiments", schema=_DISPATCH_SCHEMA,
        handler=_dispatch_experiments_handler, toolset="core",
    )
    registry.register(
        name="merge_experiment", schema=_MERGE_SCHEMA,
        handler=_merge_experiment_handler, toolset="core",
    )
