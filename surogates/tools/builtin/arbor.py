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
import re
import uuid as _uuid
from datetime import datetime, timezone
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


def _naive_utcnow() -> datetime:
    """Naive UTC timestamp matching the schema's TIMESTAMP WITHOUT TIME ZONE."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _slug(text: str, length: int = 24) -> str:
    """Branch-safe slug from a hypothesis line."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:length] or "exp"


async def _sandbox_sh(kwargs, command: str, *, timeout: int = 120) -> str:
    """Run a shell command in the session's sandbox via the terminal tool."""
    from surogates.sandbox.base import default_sandbox_spec

    pool = kwargs["sandbox_pool"]
    owner = str(kwargs["session_id"])
    await pool.ensure(owner, default_sandbox_spec())
    return await pool.execute(owner, "terminal", json.dumps({
        "command": command, "timeout": timeout,
    }))


async def _ancestor_insights(store, run_id, node) -> list[tuple[str, str]]:
    """The (key, insight) chain from root down to the node's parent."""
    chain: list[tuple[str, str]] = []
    key = node.parent_key
    while key is not None:
        parent = await store.get_node(run_id, key)
        chain.append((parent.node_key, parent.insight or ""))
        key = parent.parent_key
    return list(reversed(chain))


# ---------------------------------------------------------------------------
# dispatch_experiments / merge_experiment — real schemas + handlers land in
# their own tasks; minimal stubs keep register() valid and the tool schemas
# present from the moment routing exists.
# ---------------------------------------------------------------------------

_DISPATCH_SCHEMA = ToolSchema(
    name="dispatch_experiments",
    description=(
        "Dispatch 1-4 pending leaf hypotheses to executor workers, each in "
        "an isolated git worktree. Validates cycle budget, depth, leaf-ness, "
        "and parallelism before spawning. Harvest folds results at your next "
        "wake — end your turn after dispatching."
    ),
    parameters={
        "type": "object",
        "properties": {
            "node_keys": {
                "type": "array", "items": {"type": "string"},
                "minItems": 1, "maxItems": 4,
                "description": "Pending leaf node keys to run, e.g. [\"1\", \"2\"].",
            },
            "extra_context": {
                "type": "string",
                "description": "Optional extra guidance appended to every brief.",
            },
            "action": {"type": "string", "enum": ["experiments", "baseline"]},
        },
        "required": ["node_keys"],
    },
)

_MERGE_SCHEMA = ToolSchema(
    name="merge_experiment",
    description="(implemented in the merge task)",
    parameters={"type": "object", "properties": {}},
)


async def _dispatch_experiments_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    from surogates.arbor.store import ResearchStoreError
    from surogates.tasks.service import TaskSpawnError, create_task_and_spawn

    try:
        store, run_id = await _require_run(
            kwargs.get("session_config") or {}, kwargs["session_factory"],
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    if arguments.get("action") == "baseline":
        return json.dumps({"error": (
            "baselines are captured at mission creation "
            "(/auto-research baseline=<dev> baseline_test=<test>), not via "
            "dispatch — dispatch is for hypotheses. If no baseline was set, "
            "clear and recreate the run with the baseline tokens."
        )})

    node_keys = list(arguments.get("node_keys") or [])
    if not node_keys:
        return json.dumps({"error": "node_keys is required (1-4 pending leaves)"})

    try:
        run = await store.get_run(run_id)
        meta = run.meta or {}

        if not meta.get("eval_cmd"):
            return json.dumps({"error": (
                "meta.eval_cmd is not set — set the dev eval command with "
                "idea_tree(set_meta) before dispatching"
            )})

        # ---- hard gates (budget does NOT ride the mission iteration cap) ----
        # Order: global budget stop, then per-node validity (the most
        # actionable message), then parallelism capacity.
        spent = await store.cycles_spent(run_id)
        max_cycles = int(meta.get("max_cycles", 20))
        if spent >= max_cycles:
            return json.dumps({"error": (
                f"cycle budget spent ({spent}/{max_cycles}) — merge the best, "
                "prune the rest, and finalize"
            )})

        all_nodes = await store.list_nodes(run_id)
        parents = {n.parent_key for n in all_nodes if n.parent_key}
        for key in node_keys:
            node = await store.get_node(run_id, key)
            if node.status != "pending":
                return json.dumps({
                    "error": f"node {key} is {node.status}, not pending"
                })
            if key in parents:
                return json.dumps({"error": f"node {key} is not a leaf"})

        in_flight = await store.in_flight_count(run_id)
        max_parallel = int(meta.get("max_parallel", 2))
        if in_flight + len(node_keys) > max_parallel:
            return json.dumps({"error": (
                f"max_parallel={max_parallel} exceeded "
                f"({in_flight} in flight, {len(node_keys)} requested)"
            )})

        parent_session_id = UUID(str(kwargs["session_id"]))
        dispatched: list[str] = []
        for key in node_keys:
            node = await store.get_node(run_id, key)
            sha8 = _uuid.uuid4().hex[:8]
            branch = f"{run.branch_prefix}/n{key}-{_slug(node.hypothesis)}-{sha8}"
            worktree = f"/workspace/.arbor/worktrees/{key}"
            # Trunk is created lazily from the repo HEAD on first dispatch;
            # nothing else creates it on a fresh run.
            out = await _sandbox_sh(kwargs, (
                f"cd {run.repo_path} && "
                f"(git rev-parse --verify {run.trunk_branch} >/dev/null 2>&1 "
                f"|| git branch {run.trunk_branch}) && "
                f"git worktree add -b {branch} {worktree} {run.trunk_branch} 2>&1"
            ))
            if "fatal" in (out or "").lower():
                return json.dumps({
                    "error": f"worktree creation failed for {key}: {out[:500]}",
                    "dispatched": dispatched,
                })

            from surogates.arbor.prompts import build_executor_brief

            brief = build_executor_brief(
                node=node, run=run, worktree_path=worktree, branch=branch,
                ancestor_insights=await _ancestor_insights(store, run_id, node),
                extra_context=arguments.get("extra_context") or "",
            )
            await _persist_workspace_file(
                kwargs, path=f".arbor/experiments/{key}/executor_prompt.md",
                content=brief,
            )

            try:
                result = await create_task_and_spawn(
                    goal=brief,
                    context=None,
                    agent_def_name="arbor-executor",
                    # A failed experiment is evidence, not a retryable crash —
                    # the tool default of 3 would silently re-run trainings.
                    max_attempts=1,
                    parent_ids=[],
                    parent_session_id=parent_session_id,
                    org_id=run.org_id,
                    mission_id=run.mission_id,
                    session_store=kwargs["session_store"],
                    session_factory=kwargs["session_factory"],
                    redis=kwargs.get("redis"),
                    tenant=kwargs.get("tenant"),
                )
            except TaskSpawnError as exc:
                return json.dumps({
                    "error": f"failed to spawn executor for {key}: {exc}",
                    "dispatched": dispatched,
                })

            await store.update_node(
                run_id, key, status="running",
                task_id=UUID(result["task_id"]), code_ref=branch,
                dispatched_at=_naive_utcnow(),
            )
            dispatched.append(key)

        return json.dumps({
            "dispatched": dispatched,
            "cycles_spent": spent, "max_cycles": max_cycles,
            "note": "end your turn — harvest folds these results at your next wake",
        })
    except ResearchStoreError as exc:
        return json.dumps({"error": str(exc)})


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
