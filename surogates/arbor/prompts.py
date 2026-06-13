"""Prompt builders for research missions: executor briefs and the report.

Ported from Arbor's ``executor_run.py:248-365`` (brief) and
``report/generator.py`` (report). Divergence from native, deliberately
stricter: ``eval_cmd_test`` is never rendered into any executor-visible
text — there is no "DO NOT use" tag for the model to ignore, because the
held-out command simply never reaches it.
"""
from __future__ import annotations

from typing import Any

BRIEF_TEMPLATE = """\
[Research experiment {node_key}]

You are an executor for an autonomous research run. Implement and
evaluate EXACTLY ONE hypothesis in YOUR OWN git worktree.

## Worktree (already created for you — work ONLY here)
- path: {worktree_path}
- branch: {branch}
- Never `git merge`, never touch {trunk_branch} or main/master, never
  leave your worktree. Commit your changes on your branch when done.

## Hypothesis
{hypothesis}

## Ancestor insights (lessons from the tree, root -> parent)
{ancestor_insights}

## Evaluation (B_dev ONLY)
- command (run from your worktree): {eval_cmd}
- timeout: {eval_timeout}s. Long runs: use terminal(background=true,
  notify_on_complete=true) + process(wait); checkpoint to /workspace.
- The held-out test split is OFF LIMITS. Do not look for it, do not
  run it. Merging is the coordinator's job through a verified gate.

## Report contract (MANDATORY)
Finish by calling worker_complete with:
- summary: what you changed, what you observed, eval output tail
- metadata: {{"node_key": "{node_key}", "score": <float B_dev score>,
  "insight": "<one transferable lesson>", "result": "<1-line outcome>",
  "branch": "{branch}"}}
A timeout or failure is still a result — report it with score=null
and the failure as the insight.{extra_context}"""


def build_executor_brief(
    *, node: Any, run: Any, worktree_path: str, branch: str,
    ancestor_insights: list[tuple[str, str]], extra_context: str = "",
) -> str:
    """Render the executor brief for one dispatched hypothesis."""
    meta = run.meta or {}
    insights = "\n".join(
        f"- [{key}] {value}" for key, value in ancestor_insights if value
    ) or "(none yet)"
    extra = f"\n\n## Extra context\n{extra_context}" if extra_context else ""
    return BRIEF_TEMPLATE.format(
        node_key=node.node_key,
        worktree_path=worktree_path,
        branch=branch,
        trunk_branch=run.trunk_branch,
        hypothesis=node.hypothesis,
        ancestor_insights=insights,
        eval_cmd=meta.get("eval_cmd") or "(ask the coordinator — not configured)",
        eval_timeout=meta.get("eval_timeout", 1800),
        extra_context=extra,
    )


def build_report(run: Any, nodes: list[Any]) -> str:
    """Final REPORT.md: held-out test scores primary (the
    final-report-uses-TEST rule), top-10 dev-scored nodes, root insight,
    merged ideas, and the compact tree."""
    meta = run.meta or {}
    direction = meta.get("metric_direction", "maximize")
    scored = sorted(
        (n for n in nodes if n.score is not None and n.node_key != "ROOT"),
        key=lambda n: n.score, reverse=(direction == "maximize"),
    )
    merged = [n for n in nodes if n.status == "merged"]
    root = next((n for n in nodes if n.node_key == "ROOT"), None)

    lines = [
        f"# Research Report — {meta.get('objective', '(objective)')}",
        "",
        "## Held-out test (authoritative)",
        f"- baseline: {meta.get('test_baseline_score')}",
        f"- final trunk: {meta.get('test_trunk_score')} ({direction})",
        "",
        "## Root insight",
        (root.insight if root and root.insight else "(none recorded)"),
        "",
        "## Merged ideas",
    ]
    lines += [
        f"- {n.node_key} dev={n.score}: {(n.hypothesis or '').splitlines()[0]}"
        for n in merged
    ] or ["(none)"]
    lines += ["", "## Top ideas by dev score"]
    lines += [
        f"- {n.node_key} [{n.status}] dev={n.score}: "
        f"{(n.hypothesis or '').splitlines()[0][:120]}"
        for n in scored[:10]
    ] or ["(none)"]
    lines += ["", "## Tree"]
    for node in _ordered_for_report(nodes):
        indent = "  " * (0 if node.node_key == "ROOT" else node.depth)
        lines.append(f"{indent}- {node.node_key} [{node.status}]")
    return "\n".join(lines)


def _ordered_for_report(nodes: list[Any]) -> list[Any]:
    """ROOT first, then dotted-decimal keys ordered numerically."""
    def key(node: Any) -> tuple[int, list[int], str]:
        if node.node_key == "ROOT":
            return (0, [], "")
        try:
            return (1, [int(p) for p in node.node_key.split(".")], node.node_key)
        except ValueError:
            return (1, [], node.node_key)
    return sorted(nodes, key=key)
