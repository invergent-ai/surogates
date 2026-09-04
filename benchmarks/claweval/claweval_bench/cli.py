"""Command-line entry point for the Claw-Eval-through-the-harness benchmark.

    claweval-bench run --split general --limit 5
    claweval-bench report <run_id>
    claweval-bench cleanup            # remove stray MCP rows after a crash

Environment (all in .env, see README):
    SUROGATES_SA_TOKEN      harness /v1/api/* auth (service account)
    CLAWEVAL_BASE_URL       harness API base (default http://localhost:8000)
    CLAWEVAL_AGENT_ID       agent under test
    CLAWEVAL_PROJECT_ID     ops project (= harness org) owning the MCP rows
    CLAWEVAL_OPS_BASE_URL   ops control plane (defaults: localhost:8888 for
                            a local harness, https://ops.surogate.ai else)
    CLAWEVAL_OPS_TOKEN      ops JWT -- or CLAWEVAL_OPS_USER +
                            CLAWEVAL_OPS_PASSWORD to log in (self-renewing)
    CLAWEVAL_ADAPTER_PUBLIC_URL  optional base URL fronting the adapter;
                            unset + remote harness = cloudflared quick tunnel
    CLAWEVAL_JUDGE_BASE_URL OpenAI-compatible judge endpoint (optional)
    CLAWEVAL_JUDGE_KEY / CLAWEVAL_JUDGE_MODEL
    CLAWEVAL_HOME           claw-eval checkout (default vendor/claw-eval)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
from typing import Any

from claweval_bench import bridge, grade, report, runner, tunnel, vendor
from claweval_bench.client import DEFAULT_EXCLUDED_TOOLS, HarnessClient
from claweval_bench.registrar import Registrar
from claweval_bench.runner import build_prompt, run_task
from claweval_bench.tasks import load_tasks, select_ids

RUNS_DIR = pathlib.Path(__file__).parent.parent / "runs"


def _next_run_id(prefix: str) -> str:
    """Next ``<prefix>-NNN`` id, one past the highest existing in that family.

    Uses max+1 rather than count+1 so a gap (a removed diagnostic run)
    never resurrects an id a report already refers to.
    """
    highest = 0
    for path in RUNS_DIR.glob(f"{prefix}-*"):
        if not path.is_dir():
            continue
        suffix = path.name.rsplit("-", 1)[-1]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"{prefix}-{highest + 1:03d}"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set -- see claweval_bench/cli.py docstring")
    return value


def _base_url() -> str:
    return os.environ.get("CLAWEVAL_BASE_URL", "http://localhost:8000")


def _resume_outcome(out_dir: pathlib.Path, task: Any) -> dict | None:
    """Reconstruct a task's outcome from disk if it already ran cleanly.

    A task counts as done when it has a graded ``scores.json`` and its
    ``meta.json`` is not an environment/rollout failure -- those (and
    un-run tasks, which have no dir) are re-executed on ``--resume`` so an
    interrupted run continues without losing or repeating good results.
    """
    task_dir = out_dir / "tasks" / task.task_id
    meta_path = task_dir / "meta.json"
    scores_path = task_dir / "scores.json"
    if not (meta_path.exists() and scores_path.exists()):
        return None
    meta = json.loads(meta_path.read_text())
    if meta.get("terminal_status") in ("env_error", "error"):
        return None
    graded = json.loads(scores_path.read_text())
    outcome = {
        "task_id": task.task_id,
        "category": task.category,
        "terminal_status": meta.get("terminal_status"),
        "wall_clock_s": meta.get("wall_clock_s"),
        "error": meta.get("error"),
        "scores": None,
        "grader_error": None,
    }
    outcome.update(graded)
    return outcome


def _excluded_tools() -> list[str]:
    """Native tools to strip from every claw session.

    ``CLAWEVAL_EXCLUDED_TOOLS`` overrides the default set: a
    comma-separated list, or the literal ``none`` to exclude nothing
    (measure the agent's own toolset as-is).
    """
    raw = os.environ.get("CLAWEVAL_EXCLUDED_TOOLS")
    if raw is None:
        return list(DEFAULT_EXCLUDED_TOOLS)
    if raw.strip().lower() == "none":
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _ops_base_url() -> str:
    explicit = os.environ.get("CLAWEVAL_OPS_BASE_URL")
    if explicit:
        return explicit
    if tunnel.is_local_base(_base_url()):
        return "http://localhost:8888"
    return "https://ops.surogate.ai"


def _build_registrar() -> Registrar:
    # CLAWEVAL_ORG_ID is accepted as a fallback: the ops project id and
    # the harness org id are the same value on this platform.
    project_id = (
        os.environ.get("CLAWEVAL_PROJECT_ID")
        or _require_env("CLAWEVAL_ORG_ID")
    )
    return Registrar(
        base_url=_ops_base_url(),
        project_id=project_id,
        agent_id=_require_env("CLAWEVAL_AGENT_ID"),
        token=os.environ.get("CLAWEVAL_OPS_TOKEN"),
        username=os.environ.get("CLAWEVAL_OPS_USER"),
        password=os.environ.get("CLAWEVAL_OPS_PASSWORD"),
        **(
            {"firebase_api_key": key}
            if (key := os.environ.get("CLAWEVAL_FIREBASE_API_KEY"))
            else {}
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claweval-bench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run tasks against the agent")
    run.add_argument("--split", default="general")
    run.add_argument("--language", default="en")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--tasks", default=None, metavar="IDS",
                     help="Comma-separated task ids (prefixes accepted)")
    run.add_argument("--run-id", default=None)
    run.add_argument("--adapter-port", type=int, default=8321)
    run.add_argument("--wall-clock-cap", type=float, default=900.0)
    run.add_argument("--task-cooldown", type=float, default=20.0,
                     metavar="SECONDS",
                     help="Idle pause between tasks so back-to-back sessions "
                          "do not throttle the model tier (default 20s)")
    run.add_argument("--rate-limit-backoff", type=float, default=300.0,
                     metavar="SECONDS",
                     help="Longer pause after a task that died to a provider "
                          "rate-limit, to let the tier's window clear before "
                          "the next task starts (default 300s)")
    run.add_argument("--no-grade", action="store_true",
                     help="Collect traces only; grade later via report")
    run.add_argument("--resume", action="store_true",
                     help="Skip tasks that already have a graded result in the "
                          "run dir (re-runs env_error/un-run tasks only). Use "
                          "with --run-id to continue an interrupted run.")

    rep = sub.add_parser("report", help="Render a run report")
    rep.add_argument("run_id")

    sub.add_parser("cleanup", help="Remove leftover claweval MCP rows")

    return parser


async def _cmd_run(args: argparse.Namespace) -> int:
    vendor.verify_pin()
    tasks_root = vendor.tasks_dir()

    selection = load_tasks(tasks_root, split=args.split, language=args.language)
    tasks = select_ids(selection.eligible, args.tasks)
    if args.limit:
        tasks = tasks[: args.limit]

    # A partial run (an explicit --limit or --tasks selection) is a smoke
    # test; a full split is a real run. They auto-increment in separate
    # families -- smoke-00N vs dev-00N -- so a run's id says what it was.
    is_smoke = bool(args.limit or args.tasks)
    run_id = args.run_id or _next_run_id("smoke" if is_smoke else "dev")
    out_dir = RUNS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"run {run_id}: {len(tasks)} eligible task(s), sequential "
          f"({len(selection.skipped)} skipped as out of scope)")

    registrar = _build_registrar()
    judge = None if args.no_grade else grade.build_judge()
    if not args.no_grade and judge is None:
        print("note: CLAWEVAL_JUDGE_BASE_URL unset -- rubric items will be "
              "skipped by graders that tolerate a missing judge, and crash "
              "the ones that do not (recorded per task).")

    exposure = tunnel.expose_adapter(
        _base_url(), args.adapter_port,
        public_url=os.environ.get("CLAWEVAL_ADAPTER_PUBLIC_URL"),
    )
    if exposure.public_base:
        print(f"adapter exposed at {exposure.public_base}")

    excluded_tools = _excluded_tools()
    if excluded_tools:
        print(f"excluding native tools per session: {', '.join(excluded_tools)}")

    outcomes: list[dict] = []
    try:
        async with HarnessClient(
            base_url=_base_url(),
            token=_require_env("SUROGATES_SA_TOKEN"),
            agent_id=_require_env("CLAWEVAL_AGENT_ID"),
            excluded_tools=excluded_tools,
        ) as client:
            for i, task in enumerate(tasks, 1):
                cached = _resume_outcome(out_dir, task) if args.resume else None
                if cached is not None:
                    outcomes.append(cached)
                    print(f"[{i}/{len(tasks)}] {task.task_id} -> cached "
                          f"({'pass' if report.passed(cached) else 'FAIL'}), "
                          f"skipped", flush=True)
                    continue
                print(f"[{i}/{len(tasks)}] {task.task_id} ...", flush=True)
                result = await run_task(
                    client, task,
                    vendor_root=vendor.home(),
                    registrar=registrar,
                    out_dir=out_dir,
                    exposure=exposure,
                    adapter_port=args.adapter_port,
                    wall_clock_cap_s=args.wall_clock_cap,
                )
                outcome: dict = {
                    "task_id": task.task_id,
                    "category": task.category,
                    "terminal_status": result.terminal_status,
                    "wall_clock_s": result.wall_clock_s,
                    "error": result.error,
                    "scores": None,
                    "grader_error": None,
                }
                if not args.no_grade and result.error is None:
                    messages = bridge.to_messages(
                        result.events, build_prompt(task),
                    )
                    dispatches = bridge.load_dispatches(
                        out_dir / "tasks" / task.task_id / "dispatches.jsonl",
                    )
                    graded = grade.grade_task(
                        task, messages, dispatches, result.audit_data, judge,
                    )
                    outcome.update(graded)
                    (out_dir / "tasks" / task.task_id / "scores.json").write_text(
                        json.dumps(graded, ensure_ascii=False, indent=2),
                    )
                outcomes.append(outcome)
                ok = report.passed(outcome)
                print(f"    -> {result.terminal_status}, "
                      f"{'pass' if ok else 'FAIL'} "
                      f"({result.wall_clock_s:.0f}s)", flush=True)
                if i < len(tasks):
                    if runner.was_rate_limited(result.events):
                        cooldown = args.rate_limit_backoff
                        print(f"    rate-limit detected — backing off "
                              f"{cooldown:.0f}s to let the tier recover",
                              flush=True)
                    else:
                        cooldown = args.task_cooldown
                    if cooldown > 0:
                        await asyncio.sleep(cooldown)
    finally:
        registrar.cleanup_all()
        registrar.close()
        exposure.close()

    (out_dir / "outcomes.json").write_text(
        json.dumps(outcomes, ensure_ascii=False, indent=2),
    )
    (out_dir / "skipped.json").write_text(
        json.dumps(selection.skipped, ensure_ascii=False, indent=2),
    )
    n_pass = sum(1 for o in outcomes if report.passed(o))
    print(f"run {run_id}: {n_pass}/{len(outcomes)} passed")
    print(f"next: claweval-bench report {run_id}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    out_dir = RUNS_DIR / args.run_id
    outcomes_path = out_dir / "outcomes.json"
    if not outcomes_path.exists():
        raise SystemExit(f"no outcomes for run {args.run_id}")
    outcomes = json.loads(outcomes_path.read_text())
    skipped_path = out_dir / "skipped.json"
    skipped = json.loads(skipped_path.read_text()) if skipped_path.exists() else {}
    text = report.render(outcomes, run_id=args.run_id, skipped=skipped)
    (out_dir / "report.md").write_text(text)
    print(text)
    print(f"\nwritten to {out_dir / 'report.md'}")
    return 0


def _cmd_cleanup() -> int:
    registrar = _build_registrar()
    try:
        removed = registrar.cleanup_all()
    finally:
        registrar.close()
    print(f"removed {removed} MCP server row(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "report":
        return _cmd_report(args)
    return _cmd_cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
