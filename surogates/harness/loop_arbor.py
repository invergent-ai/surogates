"""Pre-LLM harvest hook for research missions.

At each research-coordinator wake, BEFORE the LLM call, this folds every
``running`` idea_node whose backing Task is terminal into the tree:
metadata-first, fail-open, deterministic. It then concat-propagates the
insight up the ancestor chain, removes the experiment worktree (the
branch survives — Arbor's invariant), and appends a ``[research harvest]``
digest plus a fresh constraints block at the END of history (the
BoardMixin idiom — append-only so the provider prefix cache and event
replay stay stable).

The fold runs no matter what the coordinator LLM does, so a dead executor
plus a lazy/compacted coordinator can never strand a ``running`` node or
let the evaluator grade a stale leaderboard.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# A Task is terminal (its attempt has ended) at one of these statuses;
# "done" is the success terminal set by worker_complete (tasks/tools.py).
_TERMINAL_TASK: tuple[str, ...] = ("done", "failed", "cancelled")

# Head+tail cap for LLM report extraction (Arbor's 12k rule).
_REPORT_CAP = 12_000


async def _extract_report(task: Any, llm_client: Any, model: str) -> dict[str, Any]:
    """Best-effort structured extraction of a free-text executor report.

    Only used when the worker completed but left no structured metadata.
    Fails open to done-with-null-score so a missing report never strands
    the node.
    """
    from surogates.arbor.models import ExperimentReport
    from surogates.harness.structured_output import generate_structured

    text = task.result or ""
    if len(text) > _REPORT_CAP:
        half = _REPORT_CAP // 2
        text = text[:half] + "\n[... middle truncated ...]\n" + text[-half:]
    try:
        report = await generate_structured(
            llm_client=llm_client, model=model,
            messages=[{
                "role": "user",
                "content": (
                    "Extract the experiment report fields from this executor "
                    "transcript. If a numeric dev score is not clearly stated, "
                    "leave score null.\n\n" + text
                ),
            }],
            output_model=ExperimentReport, max_tokens=600, temperature=0,
        )
    except Exception:
        logger.warning("research: report extraction failed (continuing)", exc_info=True)
        report = None
    if report is None:
        return {
            "status": "done", "score": None,
            "insight": "(report extraction produced nothing usable)",
            "result": (task.result or "")[:500],
        }
    return {
        "status": "done", "score": report.score,
        "insight": (report.insight or "")[:2000],
        "result": (report.result or "")[:500],
    }


async def fold_task_into_node(
    store: Any, run_id: Any, node_key: str, task: Any,
    *, llm_client: Any, model: str | None = None,
) -> dict[str, Any]:
    """Fold one terminal Task into ``node_key`` and propagate its lesson.

    ``node_key`` is authoritative (the harvest hook gets it from the
    ``idea_nodes.task_id`` -> ``tasks.id`` join; ``record_from_task``
    looks it up). Metadata supplies only score/insight/result.
    """
    if not node_key:
        return {"skipped": "no node_key"}

    md = dict(getattr(task, "result_metadata", None) or {})
    status = getattr(task, "status", None)
    has_report = any(k in md for k in ("score", "insight", "result"))

    if status == "done" and has_report:
        fields = {
            "status": "done",
            "score": md.get("score"),
            "insight": str(md.get("insight") or "")[:2000],
            "result": str(md.get("result") or "")[:500],
        }
    elif status == "done" and getattr(task, "result", None) and llm_client is not None and model:
        fields = await _extract_report(task, llm_client, model)
    elif status == "done":
        fields = {
            "status": "done", "score": None,
            "insight": "(executor completed without a structured report)",
            "result": (getattr(task, "result", None) or "")[:500],
        }
    else:
        # crashed / timed out / cancelled — budget consumed, timeout is evidence.
        fields = {
            "status": "failed", "score": None,
            "insight": f"Timed out/crashed: task ended {status}",
            "result": (getattr(task, "result", None) or "")[:500],
        }

    await store.update_node(run_id, node_key, **fields)

    # Deterministic concat-propagate up the ancestor chain (no LLM in the
    # wake hot path; LLM-synthesis backprop lands in v2 inside tool calls).
    from surogates.arbor.propagate import concat_propagate

    nodes = await store.list_nodes(run_id)
    insights = {n.node_key: n.insight for n in nodes}
    parents = {n.node_key: n.parent_key for n in nodes if n.parent_key}
    for ancestor, merged in concat_propagate(
        node_key=node_key, insight=fields.get("insight") or "",
        insights=insights, parents=parents,
    ).items():
        await store.update_node(run_id, ancestor, insight=merged)

    return {
        "folded": node_key,
        "status": fields["status"],
        "score": fields["score"],
    }


class ArborHarvestMixin:
    """Mixin on ``AgentHarness`` providing the pre-LLM research harvest hook.

    Mirrors ``BoardMixin``: never raises (a harvest failure must not break
    the agent loop) and appends at the end of history.
    """

    async def maybe_harvest_research(
        self, session: Any, messages: list[dict],
    ) -> None:
        """Top-of-iteration research hook. Never raises."""
        try:
            await self._harvest_research_inner(session, messages)
        except Exception:
            logger.exception(
                "research: harvest hook failed for session %s (continuing)",
                session.id,
            )

    async def _harvest_research_inner(
        self, session: Any, messages: list[dict],
    ) -> None:
        config = session.config or {}
        raw = config.get("active_research_run_id")
        if not raw:
            return

        from sqlalchemy import select

        from surogates.arbor.store import ResearchStore
        from surogates.db.models import IdeaNode, Task
        from surogates.session.events import EventType

        run_id = UUID(str(raw))
        store = ResearchStore(self._session_factory)

        async with self._session_factory() as db:
            rows = (await db.execute(
                select(IdeaNode, Task)
                .join(Task, IdeaNode.task_id == Task.id)
                .where(
                    IdeaNode.run_id == run_id,
                    IdeaNode.status == "running",
                    Task.status.in_(_TERMINAL_TASK),
                )
            )).all()

        if not rows:
            return

        run = await store.get_run(run_id)
        model = getattr(session, "model", None)
        digests: list[dict[str, Any]] = []
        for node, task in rows:
            folded = await fold_task_into_node(
                store, run_id, node.node_key, task,
                llm_client=getattr(self, "_llm", None), model=model,
            )
            digests.append(folded)
            # Worktree cleanup — the branch survives (Arbor's invariant).
            await self._research_worktree_cleanup(session, run.repo_path, node.node_key)

        constraints = await store.constraints_block(run_id)

        # Convergence intervention (fail-open): a plateaued run gets an
        # Exploit/Combine/Leap nudge appended to the digest so the coordinator
        # sees it before its next IDEATE.
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
            logger.warning(
                "research: convergence check failed for session %s (continuing)",
                session.id, exc_info=True,
            )

        content = (
            "[research harvest]\n"
            + json.dumps(digests, default=str)
            + "\n\n"
            + constraints
            + intervention
        )
        await self._store.emit_event(
            session.id, EventType.RESEARCH_HARVESTED,
            {"run_id": str(run_id), "folded": [d.get("folded") for d in digests]},
        )
        messages.append({"role": "user", "content": content})

    async def _research_worktree_cleanup(
        self, session: Any, repo_path: str, node_key: str,
    ) -> None:
        """Remove the experiment worktree dir; keep its branch. Best-effort."""
        pool = getattr(self, "_sandbox_pool", None)
        if pool is None:
            return
        from surogates.sandbox.base import default_sandbox_spec

        owner = str(session.id)
        try:
            await pool.ensure(owner, default_sandbox_spec())
            await pool.execute(owner, "terminal", json.dumps({
                "command": (
                    f"cd {repo_path} && git worktree remove --force "
                    f"/workspace/.arbor/worktrees/{node_key} 2>/dev/null; true"
                ),
                "timeout": 60,
            }))
        except Exception:
            logger.warning(
                "research: worktree cleanup failed for node %s (continuing)",
                node_key, exc_info=True,
            )
