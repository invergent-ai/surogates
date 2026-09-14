"""Command-line entry point for the AutomationBench experiment.

    abbench run --domains sales --limit 2   # smoke: bridge + rubric
    abbench run                             # counted: 600 public tasks
    abbench report <run_id>

Environment:
    SUROGATES_SA_TOKEN  harness /v1/api/* auth (same as the siblings)
    AB_BASE_URL         harness API base (default http://localhost:8000)
    AB_AGENT_ID         agent under test -- lean, chat-only: the task
                        tools are simulated by the orchestrator, real
                        harness tools are pure contamination

Scoring is upstream's assertion rubric, run in-process -- no judge.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import pathlib

from abbench.bridge import Task, load_domain_tasks
from abbench.client import HarnessClient
from abbench.report import TaskOutcome, render
from abbench.runner import run_split

RUNS_DIR = pathlib.Path(__file__).parent.parent / "runs"
PUBLIC_DOMAINS = ("sales", "marketing", "operations", "support",
                  "finance", "hr")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="abbench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run tasks against the agent")
    run.add_argument("--domains", default=None,
                     help="Comma-separated domains (default: the six "
                          "public ones; 'simple' available for probes)")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--tasks", default=None, metavar="IDS",
                     help="Comma-separated task ids (domain/example_id)")
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--wall-clock-cap", type=float, default=2400.0)
    run.add_argument("--run-id", default=None)

    report = sub.add_parser("report", help="Render a run report")
    report.add_argument("run_id")

    return parser


def next_run_id(runs_dir: pathlib.Path, prefix: str) -> str:
    highest = 0
    for path in runs_dir.glob(f"{prefix}-*"):
        suffix = path.name.removeprefix(f"{prefix}-")
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"{prefix}-{highest + 1:03d}"


def _run_dir(run_id: str) -> pathlib.Path:
    d = RUNS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set -- see abbench/cli.py docstring")
    return value


def select_tasks(tasks: list[Task], spec: str | None) -> list[Task]:
    if not spec:
        return tasks
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    by_id = {t.task_id: t for t in tasks}
    unmatched = [w for w in wanted if w not in by_id]
    if unmatched:
        raise SystemExit(f"no task matches: {', '.join(unmatched)}")
    return [by_id[w] for w in wanted]


async def _cmd_run(args: argparse.Namespace) -> int:
    domains = tuple(d.strip() for d in args.domains.split(",")) \
        if args.domains else PUBLIC_DOMAINS
    tasks: list[Task] = []
    for domain in domains:
        tasks.extend(load_domain_tasks(domain))
    tasks = select_tasks(tasks, args.tasks)
    if args.limit:
        tasks = tasks[: args.limit]

    is_pilot = bool(args.limit or args.tasks
                    or tuple(domains) != PUBLIC_DOMAINS)
    run_id = args.run_id or next_run_id(
        RUNS_DIR, "smoke" if is_pilot else "full"
    )
    out_dir = _run_dir(run_id)
    print(f"run {run_id}: {len(tasks)} task(s) across "
          f"{len(domains)} domain(s), concurrency {args.concurrency}")

    async with HarnessClient(
        base_url=os.environ.get("AB_BASE_URL", "http://localhost:8000"),
        token=_require_env("SUROGATES_SA_TOKEN"),
        agent_id=_require_env("AB_AGENT_ID"),
    ) as client:
        results = await run_split(
            client, tasks, out_dir=str(out_dir),
            concurrency=args.concurrency,
            wall_clock_cap_s=args.wall_clock_cap,
        )

    by_id = {t.task_id: t for t in tasks}
    outcomes = [
        TaskOutcome(
            task_id=r.task_id,
            domain=by_id[r.task_id].domain,
            partial_credit=r.partial_credit,
            strict=r.strict,
            tool_calls=r.tool_calls,
            completed_marker=r.completed_marker,
            terminal_status=r.terminal_status,
            error=r.error,
            score_error=r.score_error,
        )
        for r in results
    ]
    with open(out_dir / "outcomes.json", "w", encoding="utf-8") as fh:
        json.dump([dataclasses.asdict(o) for o in outcomes], fh, indent=2)

    passed = sum(1 for o in outcomes if o.strict == 1.0)
    print(f"run {run_id}: {passed}/{len(outcomes)} passed strictly")
    print(f"next: abbench report {run_id}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    path = _run_dir(args.run_id) / "outcomes.json"
    if not path.exists():
        raise SystemExit(f"no outcomes for run {args.run_id}")
    with open(path, encoding="utf-8") as fh:
        outcomes = [TaskOutcome(**row) for row in json.load(fh)]
    text = render(outcomes, run_id=args.run_id)
    (_run_dir(args.run_id) / "report.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    return _cmd_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
