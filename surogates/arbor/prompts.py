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
evaluate EXACTLY ONE hypothesis against the REAL benchmark.

## Set up your repo (handed to you as a git bundle — NOT pre-checked-out)
Run these EXACTLY:
    mkdir -p {work_dir} && cd {work_dir}
    base64 -d {bundle_path} > /tmp/repo-{node_key}.bundle
    git clone -q -b {branch} /tmp/repo-{node_key}.bundle . && rm /tmp/repo-{node_key}.bundle
You now have the full repo at {work_dir} on branch {branch}.

## Edit ONLY with the file tools (write_file / edit) — NOT shell redirects
Your code changes reach the coordinator ONLY through the file tools. A
change made with a shell redirect (`>`, `sed -i`, `tee`, `cat <<EOF`)
will NOT survive out of your sandbox and your experiment is lost. Edit
every file you change with write_file/edit under {work_dir}/. You do not
need to `git commit` — the coordinator imports your working tree.

## Hypothesis
{hypothesis}

## Ancestor insights (lessons from the tree, root -> parent)
{ancestor_insights}

## Evaluation (B_dev ONLY)
- command (run from {work_dir}): {eval_cmd}
- timeout: {eval_timeout}s. Long runs: use terminal(background=true,
  notify_on_complete=true) + process(wait).
- The held-out test split is OFF LIMITS. Do not look for it, do not run
  it. Merging is the coordinator's job through a verified gate.

## Report contract (MANDATORY)
Finish by calling worker_complete with:
- summary: what you changed, what you observed, eval output tail
- metadata: {{"node_key": "{node_key}", "score": <float B_dev score>,
  "insight": "<one transferable lesson>", "result": "<1-line outcome>",
  "branch": "{branch}"}}
A timeout or failure is still a result — report it with score=null
and the failure as the insight.{extra_context}"""


def build_executor_brief(
    *, node: Any, run: Any, bundle_path: str, work_dir: str, branch: str,
    ancestor_insights: list[tuple[str, str]], extra_context: str = "",
) -> str:
    """Render the executor brief for one dispatched hypothesis.

    The repo crosses to the executor's separate sandbox as a base64 git
    bundle at ``bundle_path`` (the only durable channel between sessions);
    the executor clones it into ``work_dir`` and edits there with the file
    tools so its changes survive back to the coordinator.
    """
    meta = run.meta or {}
    insights = "\n".join(
        f"- [{key}] {value}" for key, value in ancestor_insights if value
    ) or "(none yet)"
    extra = f"\n\n## Extra context\n{extra_context}" if extra_context else ""
    return BRIEF_TEMPLATE.format(
        node_key=node.node_key,
        bundle_path=bundle_path,
        work_dir=work_dir,
        branch=branch,
        trunk_branch=run.trunk_branch,
        hypothesis=node.hypothesis,
        ancestor_insights=insights,
        eval_cmd=meta.get("eval_cmd") or "(ask the coordinator — not configured)",
        eval_timeout=meta.get("eval_timeout", 1800),
        extra_context=extra,
    )


def _fmt_delta(baseline, trunk) -> str:
    """Signed held-out delta baseline -> trunk, or 'n/a' when unmeasured."""
    if baseline is None or trunk is None:
        return "n/a"
    return f"{trunk - baseline:+.4g}"


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
        f"- delta: {_fmt_delta(meta.get('test_baseline_score'), meta.get('test_trunk_score'))}",
        "",
        "## Eval commands",
        f"- dev:  {meta.get('eval_cmd', '(unset)')}",
        f"- test: {meta.get('eval_cmd_test', '(unset)')}",
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
