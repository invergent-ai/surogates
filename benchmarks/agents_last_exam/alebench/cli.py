"""Command-line entry point for the Agents' Last Exam experiment.

    alebench tasks                        # list tasks + eligibility
    alebench run --domains demo --limit 1 # smoke
    alebench grade <run_id>               # offline, free, repeatable
    alebench report <run_id>

Environment:
    SUROGATES_SA_TOKEN  harness /v1/api/* auth (same as the siblings)
    ALE_BASE_URL        harness API base (default http://localhost:8000)
    ALE_AGENT_ID        agent under test (sandbox/terminal capable)
    ALE_HOME            checkout (default vendor/agents-last-exam)
    ALE_DATA_DIR        extracted gated data archive (default task-data/)

Grading is the task's own deterministic grader script -- no judge.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import pathlib

from alebench import vendor
from alebench.client import HarnessClient
from alebench.dataset import Task, eligibility, load_tasks
from alebench.grade import grade_task
from alebench.report import TaskOutcome, render
from alebench.runner import run_split

RUNS_DIR = pathlib.Path(__file__).parent.parent / "runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alebench")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tasks", help="List tasks and their eligibility")

    run = sub.add_parser("run", help="Run tasks against the agent")
    run.add_argument("--domains", default=None)
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--tasks", default=None, metavar="IDS")
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--wall-clock-cap", type=float, default=3600.0)
    run.add_argument("--run-id", default=None)

    grade = sub.add_parser("grade", help="Grade a run's collected outputs")
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
        raise SystemExit(f"{name} is not set -- see alebench/cli.py docstring")
    return value


def _cmd_tasks() -> int:
    for task in load_tasks():
        reason = eligibility(task)
        mark = "ok " if reason is None else "SKIP"
        print(f"{mark} {task.task_id}"
              + (f"  ({reason})" if reason else ""))
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
    domains = tuple(d.strip() for d in args.domains.split(",")) \
        if args.domains else None
    tasks = load_tasks(domains)

    runnable: list[Task] = []
    skipped: list[tuple[str, str]] = []
    for t in tasks:
        reason = eligibility(t)
        (skipped.append((t.task_id, reason)) if reason
         else runnable.append(t))
    for task_id, reason in skipped:
        print(f"skip {task_id}: {reason}")

    runnable = select_tasks(runnable, args.tasks)
    if args.limit:
        runnable = runnable[: args.limit]

    is_pilot = bool(args.limit or args.tasks or domains)
    run_id = args.run_id or next_run_id(
        RUNS_DIR, "smoke" if is_pilot else "full"
    )
    out_dir = _run_dir(run_id)
    print(f"run {run_id}: {len(runnable)} task(s), {len(skipped)} skipped, "
          f"pin {commit[:12]}")

    async with HarnessClient(
        base_url=os.environ.get("ALE_BASE_URL", "http://localhost:8000"),
        token=_require_env("SUROGATES_SA_TOKEN"),
        agent_id=_require_env("ALE_AGENT_ID"),
    ) as client:
        results = await run_split(
            client, runnable, out_dir=str(out_dir),
            concurrency=args.concurrency,
            wall_clock_cap_s=args.wall_clock_cap,
        )

    with open(out_dir / "rollout.json", "w", encoding="utf-8") as fh:
        json.dump({
            "run_id": run_id,
            "pin": commit,
            "skipped": [{"task_id": t, "reason": r} for t, r in skipped],
            "tasks": [
                {"task_id": r.task_id, "terminal_status": r.terminal_status,
                 "error": r.error, "wall_clock_s": r.wall_clock_s,
                 "collected": len(r.collected)}
                for r in results
            ],
        }, fh, indent=2)

    done = sum(1 for r in results if r.terminal_status == "completed")
    print(f"run {run_id}: {done}/{len(results)} sessions completed")
    print(f"next: alebench grade {run_id}")
    return 0


def _cmd_grade(args: argparse.Namespace) -> int:
    out_dir = _run_dir(args.run_id)
    base = out_dir / "tasks"
    task_dirs = sorted(
        p for p in base.glob("*") if (p / "meta.json").exists()
    ) if base.exists() else []
    if not task_dirs:
        raise SystemExit(f"no task traces in run {args.run_id}")

    tasks_by_id = {t.task_id: t for t in load_tasks()}
    outcomes: list[TaskOutcome] = []
    for task_dir in task_dirs:
        task_id = task_dir.name.replace("__", "/")
        task = tasks_by_id.get(task_id)
        with open(task_dir / "meta.json", encoding="utf-8") as fh:
            meta = json.load(fh)
        if task is None:
            print(f"  ! {task_id}: unknown task, skipping")
            continue
        result = grade_task(
            task_id, task.grader_script,
            pred_dir=str(task_dir / "pred"),
            reference_dir=task.reference_dir,
        )
        with open(task_dir / "scores.json", "w", encoding="utf-8") as fh:
            json.dump(dataclasses.asdict(result), fh, indent=2, default=str)
        outcomes.append(TaskOutcome(
            task_id=task_id,
            domain=task.domain,
            total_score=result.total_score,
            terminal_status=str(meta.get("terminal_status") or ""),
            error=meta.get("error"),
            grade_error=result.grade_error,
            collected_files=len(meta.get("collected") or []),
        ))
        score = f"{result.total_score:g}" if result.total_score is not None \
            else f"ungradable ({(result.grade_error or '')[:60]})"
        print(f"  {task_id}: {score}")

    with open(out_dir / "outcomes.json", "w", encoding="utf-8") as fh:
        json.dump([dataclasses.asdict(o) for o in outcomes], fh, indent=2)
    print(f"next: alebench report {args.run_id}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    path = _run_dir(args.run_id) / "outcomes.json"
    if not path.exists():
        raise SystemExit(
            f"no outcomes for run {args.run_id} -- run `alebench grade` first"
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
