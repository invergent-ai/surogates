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
        "Read and mutate this research run's Idea Tree. The root node's key "
        "is \"ROOT\". Actions: "
        "view (format=constraints|compact); "
        "add(parent_key, hypothesis) — parent_key defaults to \"ROOT\" for a "
        "top-level idea; "
        "update(node_key, fields) — mutable fields are status, insight, "
        "result, code_ref, related_work; score/test_score/branch are "
        "machine-set by dispatch/merge and ignored here; "
        "prune(node_key, reason); "
        "set_meta(values) — run-config keys only (eval_cmd, eval_cmd_test, "
        "max_cycles, max_parallel, …); baseline and repo are fixed at run "
        "creation and cannot be set; "
        "record_from_task(task_id); requeue(node_key, reason); report."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [
                "view", "add", "update", "prune", "set_meta",
                "record_from_task", "requeue", "propagate", "report",
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
    from surogates.arbor.store import (
        MACHINE_KEYS,
        MetaKeyError,
        ResearchStoreError,
    )

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
            # Top-level hypotheses hang off ROOT, and models routinely omit
            # parent_key or guess "0"/"root". Normalise all of those so a
            # first-cycle add doesn't bounce on "node '0' not found".
            parent_key = (arguments.get("parent_key") or "ROOT").strip() or "ROOT"
            if parent_key in ("0", "root", "Root"):
                parent_key = "ROOT"
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
            # Forgiving alias: models reach for "lesson"; the field is
            # "insight". Map it when the canonical field isn't already set.
            if "lesson" in fields:
                lesson = fields.pop("lesson")
                fields.setdefault("insight", lesson)
            # Machine-owned fields: scores and the experiment branch arrive
            # via dispatch / harvest / merge, never coordinator prose. Strip
            # them so the call still succeeds, and say they were ignored.
            ignored = [k for k in ("score", "test_score", "branch") if k in fields]
            for k in ignored:
                fields.pop(k, None)
            await store.update_node(run_id, node_key, **fields)
            out = {"ok": True}
            if ignored:
                out["ignored"] = ignored
                out["note"] = (
                    "score/test_score/branch are set by dispatch_experiments "
                    "and merge_experiment, not update; ignored those keys"
                )
            return json.dumps(out)

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

        if action == "set_meta":
            values = dict(arguments.get("values") or {})
            # Creation-time / machine-owned keys the model reaches for but
            # cannot set: baseline + repo are fixed at run creation; the
            # *_score keys are written by the eval / merge paths. Drop them
            # with a note instead of failing the whole call; genuinely
            # unknown keys still raise below so typos stay visible.
            readonly = {"baseline", "repo", "repo_path"} | MACHINE_KEYS
            ignored = sorted(k for k in values if k in readonly)
            settable = {k: v for k, v in values.items() if k not in ignored}
            try:
                if settable:
                    await store.set_meta(run_id, settable)
            except MetaKeyError as exc:
                return json.dumps({"error": str(exc)})
            out = {"ok": True}
            if ignored:
                out["ignored"] = ignored
                out["note"] = (
                    "baseline/repo are fixed at run creation and *_score keys "
                    "are machine-set; ignored those keys"
                )
            return json.dumps(out)

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

        if action == "report":
            # Lazy import: prompts.py (incl. build_report) ships with
            # dispatch_experiments; no earlier path exercises this action.
            from surogates.arbor.prompts import build_report

            report = build_report(run, await store.list_nodes(run_id))
            await _persist_workspace_file(
                kwargs, path=".arbor/REPORT.md", content=report,
            )
            # Mark the report produced — the evaluator gates `satisfied` on
            # this (machine-written, so the LLM cannot fake completion).
            await store.set_meta(
                run_id, {"report_rendered": True}, allow_machine_keys=True,
            )
            # Render the report as a markdown artifact on THIS (coordinator)
            # session so it surfaces in the chat. The strict coordinator has
            # no create_artifact tool, so without this it resorts to spawning
            # a child worker — whose artifact never propagates back to the
            # root, and which can't worker_complete unless task-bound.
            artifact_note = ""
            api_client = kwargs.get("api_client")
            if api_client is not None:
                try:
                    await api_client.create_artifact(
                        name="Research Report", kind="markdown",
                        spec={"content": report},
                    )
                    artifact_note = (
                        "\n\n[rendered as the 'Research Report' artifact in "
                        "this chat — do NOT spawn a worker/task to render it]"
                    )
                except Exception:  # noqa: BLE001 — artifact is best-effort
                    logger.warning(
                        "research: report artifact creation failed "
                        "(continuing)", exc_info=True,
                    )
            return report + artifact_note

        return json.dumps({"error": f"unknown action {action!r}"})
    except ResearchStoreError as exc:
        return json.dumps({"error": str(exc)})


async def _record_from_task(store, run_id, task_id: str, kwargs) -> str:
    """Coordinator's correction channel: fold a Task row by id — reads
    ``Task.result``/``result_metadata`` from the DB, never coordinator prose.

    The node is resolved from the ``idea_nodes.task_id`` link (the same
    authoritative source the harvest hook uses), not from task metadata.
    """
    from sqlalchemy import select

    from surogates.db.models import IdeaNode, Task
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
        node = await db.scalar(
            select(IdeaNode).where(
                IdeaNode.run_id == run_id, IdeaNode.task_id == tid,
            )
        )
    if node is None:
        return json.dumps({
            "error": f"no idea node in this run links task {task_id}"
        })
    folded = await fold_task_into_node(
        store, run_id, node.node_key, task, llm_client=None, model=None,
    )
    # LLM-synthesize the ancestor chain now that the node carries a fresh
    # insight (the wake harvest only concat-propagates; this is the v2
    # distillation, available because record runs in a coordinator turn).
    from surogates.arbor.propagate import propagate_insights_llm
    await propagate_insights_llm(
        store, run_id, node.node_key,
        llm_client=kwargs.get("llm_client"), model=kwargs.get("model"),
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


def _terminal_stdout(raw: str) -> str:
    """Extract bare stdout from the terminal tool's JSON envelope.

    ``_sandbox_sh`` returns the terminal tool's wrapper
    ``{"output", "exit_code", "error"}``. Callers that parse command
    output (the merge-eval ``result.json``, the protected-paths diff)
    need the stdout, not the envelope — parsing the envelope as the
    payload silently swallows the result (its ``error`` field is the
    command's error, NOT the eval's, so a clean run looks like a
    no-score failure). Defensive: a non-envelope string (e.g. a test
    stub returning raw stdout) passes through unchanged.
    """
    try:
        obj = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw
    if isinstance(obj, dict) and "output" in obj:
        return obj.get("output") or ""
    return raw


async def _bundle_branch_b64(
    kwargs, *, repo_path: str, trunk_branch: str, branch: str, key: str,
) -> str | None:
    """Create the experiment branch at trunk and return its git bundle,
    base64-encoded on a single line.

    The bundle is the durable payload the executor clones: terminal-created
    git state does NOT cross the per-session workspace boundary, but a
    ``write_file``'d bundle does. Returns ``None`` when bundling fails (the
    repo isn't a git repo, the branch can't be created, etc.).
    """
    tmp = f"/tmp/arbor-{key}.bundle"
    out = _terminal_stdout(await _sandbox_sh(kwargs, (
        f"cd {repo_path} && "
        f"(git rev-parse --verify {trunk_branch} >/dev/null 2>&1 "
        f"|| git branch {trunk_branch}) && "
        f"(git rev-parse --verify {branch} >/dev/null 2>&1 "
        f"|| git branch {branch} {trunk_branch}) && "
        f"git bundle create {tmp} {branch} >/dev/null 2>&1 && "
        # ``| tr -d '\\n'`` keeps the base64 one line on GNU *and* busybox
        # (no ``-w0`` flag dependency); the trailing rm always runs.
        f"base64 {tmp} | tr -d '\\n'; rm -f {tmp}"
    )))
    b64 = (out or "").strip()
    # A clean run yields one unbroken base64 line; reject empties and any
    # git error text that leaked instead.
    if not b64 or any(c.isspace() for c in b64) or "fatal" in b64.lower():
        return None
    return b64


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
    description=(
        "Merge a done experiment into trunk ONLY after this tool itself "
        "re-runs the held-out test eval in a detached worktree. "
        "start(node_key) launches the eval and returns immediately; "
        "status(node_key) reads the result and finalizes. There is NO way "
        "to pass a score — the held-out number is machine-measured here."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "status"]},
            "node_key": {"type": "string"},
        },
        "required": ["action", "node_key"],
    },
)

# Stdlib-only extractor written into the workspace and run by the detached
# eval. Reads the eval log, takes the LAST flat ``{... "score": <num> ...}``
# object, and always writes a result.json (score or error) so ``status`` can
# distinguish "still running" (no file) from "finished without a score".
_SCORE_EXTRACTOR = r'''import json, re, sys
try:
    text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
except Exception as exc:  # pragma: no cover - defensive
    print(json.dumps({"error": "could not read eval log: %s" % exc})); raise SystemExit(0)
best = None
for m in re.finditer(r'\{[^{}]*"score"[^{}]*\}', text):
    try:
        obj = json.loads(m.group(0))
    except Exception:
        continue
    if isinstance(obj.get("score"), (int, float)) and not isinstance(obj.get("score"), bool):
        best = obj
if best is not None:
    print(json.dumps({"score": float(best["score"])}))
else:
    print(json.dumps({"error": 'no {"score": <number>} found in eval output'}))
'''


def _merge_eval_dir(node_key: str) -> str:
    return f"/workspace/.arbor/merge-eval/{node_key}"


async def _launch_merge_eval(kwargs, *, run, node_key: str, branch: str) -> str | None:
    """Write the extractor and launch the held-out eval detached.

    The eval runs under ``nohup ... &`` writing ``eval.log`` then
    ``result.json`` — so no sandbox exec is held open for the eval's
    duration (per-exec timeouts, pod churn) and ``status`` polls the file.

    Returns ``None`` when the eval was launched, else a one-line error
    describing why the setup (worktree add) failed — so ``start`` can
    surface it immediately instead of leaving ``status`` to poll
    ``running`` until the stale-grace timeout (~30 min).
    """
    meta = run.meta or {}
    evald = _merge_eval_dir(node_key)
    extractor = "/workspace/.arbor/extract_score.py"
    # Must match ``extractor`` above — the detached eval runs it by that
    # absolute path. ``write_file`` creates the ``.arbor`` parent dir.
    await _persist_workspace_file(
        kwargs, path=".arbor/extract_score.py", content=_SCORE_EXTRACTOR,
    )
    eval_cmd_test = meta["eval_cmd_test"]
    out = _terminal_stdout(await _sandbox_sh(kwargs, (
        f"rm -rf {evald} && mkdir -p {evald} && "
        f"cd {run.repo_path} && "
        # ``git worktree prune`` clears the admin entry left behind when a
        # previous merge-eval dir was rm-ed (a re-start after a stale eval,
        # or a re-merge); without it ``git worktree add`` to the same path
        # fails as already-registered. Keeps the staleness-recovery path
        # working and stops registered worktrees from accumulating.
        f"git worktree prune && "
        f"git worktree add --detach {evald}/wt {branch} 2>&1 && "
        f"cd {evald}/wt && "
        f"(nohup sh -c '{eval_cmd_test} > {evald}/eval.log 2>&1; "
        f"python3 {extractor} {evald}/eval.log > {evald}/result.json.tmp "
        f"2>>{evald}/eval.log; mv {evald}/result.json.tmp {evald}/result.json' "
        # The setup runs synchronously; on success ``echo`` confirms the
        # eval was backgrounded. On a worktree-add failure the ``&&`` chain
        # stops before this and the marker is absent.
        f">/dev/null 2>&1 &) && echo MERGE_EVAL_LAUNCHED"
    ), timeout=60))
    if "MERGE_EVAL_LAUNCHED" in (out or ""):
        return None
    detail = " ".join((out or "").split())[:300] or "no output"
    return (
        f"held-out eval setup failed for {node_key}: {detail} — the branch "
        "may be checked out in another worktree or the repo is mid-merge; "
        "retry shortly"
    )


async def _cleanup_merge_worktree(kwargs, *, run, node_key: str) -> None:
    """Remove the detached merge-eval worktree once its result is consumed.

    The merge operates on the main checkout, not this worktree, so it is
    safe to drop as soon as ``status`` has read result.json — keeping
    distinct merged nodes from leaving a worktree each. Best-effort; the
    ``git worktree prune`` in ``_launch_merge_eval`` is the backstop.
    """
    evald = _merge_eval_dir(node_key)
    try:
        await _sandbox_sh(kwargs, (
            f"cd {run.repo_path} && "
            f"git worktree remove --force {evald}/wt 2>/dev/null; "
            f"rm -rf {evald}/wt; git worktree prune; true"
        ))
    except Exception:
        logger.warning(
            "research: merge-eval worktree cleanup failed for %s (continuing)",
            node_key, exc_info=True,
        )


async def _merge_experiment_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    from surogates.arbor.store import ResearchStoreError, is_improvement

    try:
        store, run_id = await _require_run(
            kwargs.get("session_config") or {}, kwargs["session_factory"],
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    action = arguments.get("action")
    node_key = arguments.get("node_key")
    if not node_key:
        return json.dumps({"error": "node_key is required"})

    try:
        run = await store.get_run(run_id)
        meta = run.meta or {}
        node = await store.get_node(run_id, node_key)
        evald = _merge_eval_dir(node_key)

        if action == "start":
            if node.status != "done":
                return json.dumps({"error": f"node {node_key} is {node.status}, not done"})
            if not node.code_ref:
                return json.dumps({"error": f"node {node_key} has no branch recorded"})
            if not meta.get("eval_cmd_test"):
                return json.dumps({"error": (
                    "meta.eval_cmd_test is not set — a research run without a "
                    "held-out eval cannot merge (there is no LLM-reported fallback)"
                )})
            if run.trunk_branch in ("main", "master"):
                return json.dumps({"error": "refusing to operate on main/master as trunk"})
            # One merge eval at a time: the merge_eval stamp is a single slot,
            # so starting a second while another node's eval is in flight would
            # clobber it and orphan the first eval. Refuse — finish that one
            # first (its status() clears the stamp on a terminal outcome).
            in_flight = (meta.get("merge_eval") or {}).get("node_key")
            if in_flight and in_flight != node_key:
                return json.dumps({"error": (
                    f"a merge eval for node {in_flight} is already in flight — "
                    f"poll merge_experiment(status, {in_flight!r}) to finish it "
                    "before starting another"
                )})
            launch_err = await _launch_merge_eval(
                kwargs, run=run, node_key=node_key, branch=node.code_ref,
            )
            if launch_err is not None:
                return json.dumps({"merged": False, "error": launch_err})
            await store.set_meta(run_id, {"merge_eval": {
                "node_key": node_key,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "retries_left": int(meta.get("eval_retries", 1)),
            }}, allow_machine_keys=True)
            return json.dumps({
                "started": node_key,
                "note": f"poll with merge_experiment(status, {node_key!r})",
            })

        if action != "status":
            return json.dumps({"error": f"unknown action {action!r}"})

        # ---- status ----
        stamp = meta.get("merge_eval") or {}
        if stamp.get("node_key") != node_key:
            return json.dumps({"error": f"no merge eval started for {node_key}"})

        raw = _terminal_stdout(
            await _sandbox_sh(kwargs, f"cat {evald}/result.json 2>/dev/null")
        )
        if not (raw or "").strip():
            started = _parse_iso(stamp.get("started_at"))
            grace = int(meta.get("eval_timeout", 1800)) + 300
            age = (datetime.now(timezone.utc) - started).total_seconds() if started else 0
            if started is not None and age > grace:
                return json.dumps({
                    "stale": True, "age_seconds": int(age),
                    "note": "eval orphaned (pod recycle?) — call start again to re-run",
                })
            return json.dumps({"running": True, "age_seconds": int(age)})

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"error": "result.json was not valid JSON"}

        if "score" not in parsed:
            # Eval finished but produced no score. Retry within budget,
            # else report failure (the eval contract requires a JSON score).
            retries_left = int(stamp.get("retries_left", 0))
            err = parsed.get("error", "eval produced no score")
            if retries_left <= 0:
                # Terminal: no relaunch will reuse the worktree — drop it.
                await _cleanup_merge_worktree(kwargs, run=run, node_key=node_key)
            if retries_left > 0:
                relaunch_err = await _launch_merge_eval(
                    kwargs, run=run, node_key=node_key, branch=node.code_ref,
                )
                if relaunch_err is not None:
                    await store.set_meta(
                        run_id, {"merge_eval": {}}, allow_machine_keys=True,
                    )
                    return json.dumps({"merged": False, "error": relaunch_err})
                await store.set_meta(run_id, {"merge_eval": {
                    "node_key": node_key,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "retries_left": retries_left - 1,
                }}, allow_machine_keys=True)
                return json.dumps({
                    "running": True, "retrying": True,
                    "retries_left": retries_left - 1, "previous_error": err,
                })
            await store.set_meta(run_id, {"merge_eval": {}}, allow_machine_keys=True)
            return json.dumps({
                "merged": False,
                "error": f"held-out eval produced no score after retries: {err}; "
                         f"see {evald}/eval.log",
            })

        # The eval is finished and read; the merge runs on the main checkout,
        # so the detached eval worktree is no longer needed on any path below.
        await _cleanup_merge_worktree(kwargs, run=run, node_key=node_key)

        score = float(parsed["score"])
        reference = meta.get("test_trunk_score", meta.get("test_baseline_score"))
        direction = meta.get("metric_direction", "maximize")
        if not is_improvement(score, reference, direction):
            await store.set_meta(run_id, {"merge_eval": {}}, allow_machine_keys=True)
            return json.dumps({
                "merged": False, "test_score": score, "reference": reference,
                "note": "held-out eval shows no improvement — treat as tree evidence",
            })

        threshold = float(meta.get("merge_threshold", 0.0))
        warning = None
        if reference is not None and abs(score - reference) < threshold:
            warning = (f"below merge_threshold={threshold} but improving — "
                       "merging per Arbor's soft-threshold semantics")

        # Protected-paths guard before touching trunk.
        protected = meta.get("protected_paths") or []
        if protected:
            diff = _terminal_stdout(await _sandbox_sh(kwargs, (
                f"cd {run.repo_path} && "
                f"git diff --name-only {run.trunk_branch}...{node.code_ref}"
            )))
            hit = sorted({
                p for p in protected
                for f in (diff or "").splitlines()
                if f.strip().startswith(p)
            })
            if hit:
                await store.set_meta(run_id, {"merge_eval": {}}, allow_machine_keys=True)
                return json.dumps({
                    "merged": False,
                    "error": f"branch touches protected paths: {hit}",
                })

        # Merge inside a fresh worktree of trunk, NOT the shared main
        # checkout. ``git checkout {trunk}`` in the main working dir is
        # flaky on the shared workspace — a stray dirty file or a worktree
        # contending for the branch makes it fail, which the old ``||``
        # mislabelled as a "merge conflict". Merging in a worktree on the
        # trunk branch advances the trunk ref the same way; on ANY failure
        # we drop the worktree, which discards the in-progress merge and
        # leaves trunk exactly as it was.
        mwt = f"/workspace/.arbor/merge/{node_key}"
        out = _terminal_stdout(await _sandbox_sh(kwargs, (
            f"cd {run.repo_path} && git worktree prune && rm -rf {mwt} && "
            f"git worktree add --force {mwt} {run.trunk_branch} 2>&1 && "
            f"git -C {mwt} -c user.email=arbor@local -c user.name=arbor "
            f"merge --no-ff {node.code_ref} "
            f"-m 'research: merge {node_key} (test={score})' 2>&1; rc=$?; "
            f"cd {run.repo_path} && "
            f"git worktree remove --force {mwt} 2>/dev/null; rm -rf {mwt}; "
            f"[ $rc -eq 0 ] && echo MERGE_OK || echo MERGE_FAILED"
        ), timeout=120))
        if "MERGE_OK" not in (out or ""):
            await store.set_meta(run_id, {"merge_eval": {}}, allow_machine_keys=True)
            detail = " ".join(
                (out or "").replace("MERGE_FAILED", "").split()
            )[:400] or "no output"
            return json.dumps({
                "merged": False,
                "error": (
                    f"could not merge {node_key} into trunk (trunk left "
                    f"unchanged): {detail}"
                ),
            })

        # The tool is the SOLE writer of test_trunk_score (machine key).
        await store.set_meta(run_id, {
            "test_trunk_score": score, "trunk_score": node.score, "merge_eval": {},
        }, allow_machine_keys=True)
        await store.update_node(run_id, node_key, status="merged")
        # Distill the validated win up the ancestor chain (fail-open).
        from surogates.arbor.propagate import propagate_insights_llm
        await propagate_insights_llm(
            store, run_id, node_key,
            llm_client=kwargs.get("llm_client"), model=kwargs.get("model"),
        )
        result: dict[str, Any] = {"merged": True, "test_score": score}
        if warning:
            result["warning"] = warning
        return json.dumps(result)
    except ResearchStoreError as exc:
        return json.dumps({"error": str(exc)})


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


async def _dispatch_baseline(store, run_id, kwargs) -> str:
    """Measure the UNMODIFIED repo on the dev split (the INIT fallback when
    intake supplied no baseline). Creates the trunk and a fixed-key BASELINE
    node, spawns an executor whose brief forbids source edits; the harvest
    writes ``meta.baseline_score`` from its reported dev score."""
    from surogates.db.models import IdeaNode
    from surogates.tasks.service import TaskSpawnError, create_task_and_spawn

    run = await store.get_run(run_id)
    meta = run.meta or {}
    if not meta.get("eval_cmd"):
        return json.dumps({"error": "meta.eval_cmd is not set — set it before baseline"})
    if meta.get("baseline_score") is not None:
        return json.dumps({"error": "baseline_score already set"})
    existing = {n.node_key for n in await store.list_nodes(run_id)}
    if "BASELINE" in existing:
        return json.dumps({"error": "a baseline experiment already exists"})

    work_dir = "/workspace/.arbor/experiments/BASELINE/wt"
    bundle_rel = ".arbor/experiments/BASELINE/repo.bundle.b64"
    b64 = await _bundle_branch_b64(
        kwargs, repo_path=run.repo_path,
        trunk_branch=run.trunk_branch, branch=run.trunk_branch, key="BASELINE",
    )
    if b64 is None:
        return json.dumps({"error": (
            f"could not bundle repo for baseline — is {run.repo_path} a "
            "git repo with commits?"
        )})
    await _persist_workspace_file(kwargs, path=bundle_rel, content=b64)

    brief = (
        "[Baseline experiment]\n\n"
        "Measure the UNMODIFIED repo on the dev split.\n"
        "Set up the repo (handed to you as a git bundle):\n"
        f"    mkdir -p {work_dir} && cd {work_dir}\n"
        f"    base64 -d /workspace/{bundle_rel} > /tmp/repo-baseline.bundle\n"
        f"    git clone -q /tmp/repo-baseline.bundle . && rm /tmp/repo-baseline.bundle\n"
        "DO NOT MODIFY any source — run the eval as-is and report the number.\n"
        f"Eval (dev), run from {work_dir}: {meta['eval_cmd']}\n\n"
        "Finish with worker_complete(metadata={\"node_key\": \"BASELINE\", "
        "\"score\": <float dev score>, \"insight\": \"baseline\", "
        "\"result\": \"baseline measured\"})."
    )
    # Insert the fixed-key BASELINE node directly (not the auto-incrementing add).
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
            # The coordinator's own wake bundle carries the arbor-executor
            # AgentDef; without it the spawn resolver is bundle-blind.
            bundle=kwargs.get("bundle"),
        )
    except TaskSpawnError as exc:
        return json.dumps({"error": f"failed to spawn baseline: {exc}"})
    await store.update_node(
        run_id, "BASELINE", status="running",
        task_id=UUID(result["task_id"]), code_ref=run.trunk_branch,
        dispatched_at=_naive_utcnow(),
    )
    return json.dumps({"baseline_dispatched": True})


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
        try:
            return await _dispatch_baseline(store, run_id, kwargs)
        except ResearchStoreError as exc:
            return json.dumps({"error": str(exc)})

    node_keys = list(arguments.get("node_keys") or [])
    if not node_keys:
        return json.dumps({"error": "node_keys is required (1-4 pending leaves)"})
    if len(set(node_keys)) != len(node_keys):
        return json.dumps({"error": "duplicate node_keys in one dispatch batch"})

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
            bundle_rel = f".arbor/experiments/{key}/repo.bundle.b64"
            work_dir = f"/workspace/.arbor/experiments/{key}/wt"
            # The executor runs in a separate sandbox that cannot see this
            # session's git state. Hand it the repo as a write_file'd bundle
            # (the durable cross-session channel); it clones into work_dir.
            b64 = await _bundle_branch_b64(
                kwargs, repo_path=run.repo_path,
                trunk_branch=run.trunk_branch, branch=branch, key=key,
            )
            if b64 is None:
                return json.dumps({
                    "error": (
                        f"could not bundle repo for {key} — is "
                        f"{run.repo_path} a git repo with commits?"
                    ),
                    "dispatched": dispatched,
                })
            await _persist_workspace_file(kwargs, path=bundle_rel, content=b64)

            from surogates.arbor.prompts import build_executor_brief

            brief = build_executor_brief(
                node=node, run=run,
                bundle_path=f"/workspace/{bundle_rel}", work_dir=work_dir,
                branch=branch,
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
                    # Coordinator's wake bundle carries arbor-executor.
                    bundle=kwargs.get("bundle"),
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
