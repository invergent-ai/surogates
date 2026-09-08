"""Command-line entry point for the TheAgentCompany experiment.

    tacbench tasks                          # list tasks + points
    tacbench run --tasks admin-arrange-meeting-rooms   # smoke
    tacbench grade <run_id>                 # evaluators in task images
    tacbench report <run_id>

Environment:
    SUROGATES_SA_TOKEN  harness /v1/api/* auth (same as the siblings)
    TAC_BASE_URL        harness API base (default http://localhost:8000)
    TAC_AGENT_ID        agent under test (browser + terminal capable)
    TAC_HOSTNAME        where the company services are hosted, as the
                        agent reaches them (default the-agent-company.com)
    TAC_HOSTNAME_IP     the same host's IP, for the evaluator
                        containers' --add-host mapping
    TAC_IMAGE_REGISTRY  registry/namespace of the task images built by
                        upstream's evaluation/generate_task_images.py
    TAC_HOME            checkout (default vendor/TheAgentCompany)

Grading is upstream's own evaluators, run in upstream's own task
images -- no judge of ours.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import pathlib

from tacbench import vendor
from tacbench.client import HarnessClient
from tacbench.dataset import Task, category, load_tasks
from tacbench.grade import grade_task
from tacbench.report import TaskOutcome, render
from tacbench.runner import run_split

RUNS_DIR = pathlib.Path(__file__).parent.parent / "runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tacbench")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tasks", help="List tasks and their points")

    run = sub.add_parser("run", help="Run tasks against the agent")
    run.add_argument("--prefixes", default=None,
                     help="Comma-separated task-name prefixes (admin, hr, "
                          "pm, sde, ds, finance, research, ...)")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--tasks", default=None, metavar="IDS")
    run.add_argument("--wall-clock-cap", type=float, default=3600.0)
    run.add_argument("--run-id", default=None)

    grade = sub.add_parser("grade", help="Grade a run with upstream evaluators")
    grade.add_argument("run_id")

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
        raise SystemExit(f"{name} is not set -- see tacbench/cli.py docstring")
    return value


def _cmd_tasks() -> int:
    for task in load_tasks():
        print(f"{task.task_id}  ({task.total_points} pts, "
              f"{len(task.checkpoint_points)} checkpoints)")
    return 0


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
    commit = vendor.verify_pin()
    prefixes = tuple(p.strip() for p in args.prefixes.split(",")) \
        if args.prefixes else None
    tasks = load_tasks(prefixes)
    tasks = select_tasks(tasks, args.tasks)
    if args.limit:
        tasks = tasks[: args.limit]

    is_pilot = bool(args.limit or args.tasks or prefixes)
    run_id = args.run_id or next_run_id(
        RUNS_DIR, "smoke" if is_pilot else "full"
    )
    out_dir = _run_dir(run_id)
    hostname = os.environ.get("TAC_HOSTNAME", "the-agent-company.com")
    print(f"run {run_id}: {len(tasks)} task(s), pin {commit[:12]}, "
          f"services at {hostname}, sequential")

    async with HarnessClient(
        base_url=os.environ.get("TAC_BASE_URL", "http://localhost:8000"),
        token=_require_env("SUROGATES_SA_TOKEN"),
        agent_id=_require_env("TAC_AGENT_ID"),
    ) as client:
        results = await run_split(
            client, tasks, out_dir=str(out_dir), hostname=hostname,
            wall_clock_cap_s=args.wall_clock_cap,
        )

    with open(out_dir / "rollout.json", "w", encoding="utf-8") as fh:
        json.dump({
            "run_id": run_id,
            "pin": commit,
            "hostname": hostname,
            "tasks": [
                {"task_id": r.task_id, "terminal_status": r.terminal_status,
                 "error": r.error, "wall_clock_s": r.wall_clock_s,
                 "collected": len(r.collected)}
                for r in results
            ],
        }, fh, indent=2)

    done = sum(1 for r in results if r.terminal_status == "completed")
    print(f"run {run_id}: {done}/{len(results)} sessions completed")
    print(f"next: tacbench grade {run_id}")
    return 0


def _cmd_grade(args: argparse.Namespace) -> int:
    out_dir = _run_dir(args.run_id)
    base = out_dir / "tasks"
    task_dirs = sorted(
        p for p in base.glob("*") if (p / "meta.json").exists()
    ) if base.exists() else []
    if not task_dirs:
        raise SystemExit(f"no task traces in run {args.run_id}")

    hostname_ip = _require_env("TAC_HOSTNAME_IP")
    registry = _require_env("TAC_IMAGE_REGISTRY")
    tasks_by_id = {t.task_id: t for t in load_tasks()}
    outcomes: list[TaskOutcome] = []
    for task_dir in task_dirs:
        task_id = task_dir.name
        task = tasks_by_id.get(task_id)
        with open(task_dir / "meta.json", encoding="utf-8") as fh:
            meta = json.load(fh)
        workspace = task_dir / "workspace"
        workspace.mkdir(exist_ok=True)
        result = grade_task(
            task_id, str(workspace), hostname_ip, registry
        )
        with open(task_dir / "scores.json", "w", encoding="utf-8") as fh:
            json.dump(dataclasses.asdict(result), fh, indent=2)
        outcomes.append(TaskOutcome(
            task_id=task_id,
            category=category(task) if task else task_id.split("-", 1)[0],
            points_total=result.points_total,
            points_earned=result.points_earned,
            terminal_status=str(meta.get("terminal_status") or ""),
            error=meta.get("error"),
            grade_error=result.grade_error,
        ))
        points = (f"{result.points_earned}/{result.points_total}"
                  if result.points_total is not None
                  else f"ungradable ({(result.grade_error or '')[:60]})")
        print(f"  {task_id}: {points}")

    with open(out_dir / "outcomes.json", "w", encoding="utf-8") as fh:
        json.dump([dataclasses.asdict(o) for o in outcomes], fh, indent=2)
    print(f"next: tacbench report {args.run_id}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    path = _run_dir(args.run_id) / "outcomes.json"
    if not path.exists():
        raise SystemExit(
            f"no outcomes for run {args.run_id} -- run `tacbench grade` first"
        )
    with open(path, encoding="utf-8") as fh:
        outcomes = [TaskOutcome(**row) for row in json.load(fh)]
    text = render(outcomes, run_id=args.run_id)
    (_run_dir(args.run_id) / "report.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "tasks":
        return _cmd_tasks()
    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "grade":
        return _cmd_grade(args)
    return _cmd_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
