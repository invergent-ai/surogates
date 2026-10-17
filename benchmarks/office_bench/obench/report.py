"""Render a run: a row per rollout, the failed checks, and the totals by kind and mode.

The failed checks are the product: each names what the document lacks or what the edit destroyed, which is where
the model or the tools fail.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def _pct(a: int, b: int) -> str:
    return f"{100.0 * a / b:.0f}%" if b else "-"


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])  # passed checks, scored checks, clean rollouts, rollouts
    for r in results:
        scored = r["passed"] + r["failed"]
        for key in (r["kind"], r["mode"], "all"):
            row = by[key]
            row[0] += r["passed"]
            row[1] += scored
            row[2] += int(r["failed"] == 0)
            row[3] += 1
    return by


def render(results: list[dict[str, Any]], run_id: str = "", invalid: list[tuple[str, str]] | None = None) -> str:
    results = sorted(results, key=lambda r: (r["kind"], r["mode"], r["task_id"], r.get("index") or 0))
    out = [f"# office_bench run {run_id}".rstrip(), ""]
    by = summarize(results)
    out += ["## Score", "", "| Slice | Checks passed | Rollouts with every check passed |", "| --- | --- | --- |"]
    for key in ("all", "create", "edit", "docx", "xlsx", "pptx", "pdf"):
        if key in by:
            p, s, clean, n = by[key]
            out.append(f"| {key} | {p}/{s} ({_pct(p, s)}) | {clean}/{n} |")
    out.append("")

    off_channel = [r for r in results if r.get("channel") != "studio"]
    served = sorted({m for r in results for m in r["metrics"]["served_models"]})
    out += [
        "## Run health",
        "",
        f"- served models: {', '.join(served) or '(none)'}",
        f"- sessions not on the studio channel: {len(off_channel)}",
        f"- sessions not completed: {sum(1 for r in results if r['terminal_status'] != 'completed')}",
        f"- rollouts with a harness/runner error: {sum(1 for r in results if r.get('error'))}",
        f"- rollouts set aside as infrastructure-invalid (not scored): {len(invalid or [])}",
        *[f"  - `{name}`: {why}" for name, why in invalid or []],
        f"- sandbox_unavailable tool results: {sum(r['metrics'].get('sandbox_unavailable', 0) for r in results)}"
        " (each waits out the sandbox's readiness timeout; office tools do not need the sandbox)",
        f"- 'office service full' results (runtime-office page pool): {sum(r['metrics'].get('office_busy', 0) for r in results)}",
        "",
        "## Rollouts",
        "",
        "| Task | Checks | Status | Wall s | Model calls | Tool calls | office_edit refused / errors | File/terminal calls | Office skill read | Claim vs checks |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        m = r["metrics"]
        tools = ", ".join(f"{k} {v}" for k, v in sorted(m["tool_calls"].items(), key=lambda kv: -kv[1]))
        claim = "contradicted" if r["claims"]["contradicted"] else (
            "claims success despite failed checks" if r["claims"]["claims_success_despite_failures"] else "ok")
        out.append(
            f"| `{r['task_id']}` r{r.get('index')} | {r['passed']}/{r['passed'] + r['failed']}"
            + (f" (+{r['unknown']}?)" if r["unknown"] else "")
            + f" | {r['terminal_status']} | {r['wall_clock_s']} | {m['model_calls']} | {tools or '-'} | "
            f"{len(m['refusals'])} / {len(m['office_errors'])} | {m.get('file_or_terminal_calls', 0)} | "
            f"{'yes' if m['read_office_skill'] else 'no'} | {claim} |"
        )
    out.append("")

    out += ["## Failed checks", ""]
    any_failed = False
    for r in results:
        failed = [c for c in r["checks"] if c["passed"] is False]
        unknown = [c for c in r["checks"] if c["passed"] is None]
        m = r["metrics"]
        if not (failed or unknown or m["refusals"] or m["office_errors"] or r["claims"]["contradicted"] or r.get("error")):
            continue
        any_failed = True
        out.append(f"### `{r['task_id']}` r{r.get('index')}  (session `{r['session_id']}`)")
        out.append("")
        if r.get("error"):
            out.append(f"- error: {r['error']}")
        for c in failed:
            out.append(f"- FAIL {c['name']}: {c['detail']}")
        for c in unknown:
            out.append(f"- ? {c['name']}: {c['detail']}")
        for x in m["refusals"]:
            out.append(f"- refused: {x}")
        for x in m["office_errors"]:
            out.append(f"- office tool error: {x}")
        for x in m["file_tools_on_documents"]:
            out.append(f"- file/terminal tool on a document: {x}")
        for x in r["claims"]["contradicted"]:
            out.append(f"- claim vs checks: {x}")
        out.append("")
    if not any_failed:
        out += ["None.", ""]
    return "\n".join(out)
