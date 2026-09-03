"""Command-line entry point for the Workspace-Bench experiment.

    wsbench run --split dev --limit 3     # smoke: staging + rollout + collection
    wsbench judge <run_id>                # grade stored outputs with the LLM judge
    wsbench report <run_id> --compare <previous_run_id>

Environment:
    SUROGATES_SA_TOKEN     surogates service-account token (Bearer auth for
                           every /v1/api/* call) -- same token GAIA uses;
                           minting is documented in benchmarks/gaia/README.md.
    WSBENCH_BASE_URL       harness API base (default http://localhost:8000)
    WSBENCH_AGENT_ID       agent under test
    WSBENCH_JUDGE_BASE_URL OpenAI-compatible judge endpoint (judge cmd only)
    WSBENCH_JUDGE_KEY      judge API key
    WSBENCH_JUDGE_MODEL    judge model id (default claude-sonnet-5)

WSBENCH_BASE_URL / WSBENCH_AGENT_ID keep their own prefix on purpose:
SUROGATES_API_URL and SUROGATES_AGENT_ID are real harness variables, and
reusing them cross-talks when the harness and the benchmark share a shell.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import pathlib

from wsbench.client import Event, HarnessClient
from wsbench.dataset import Task, load_tasks
from wsbench.judge import RubricVerdict, judge_task, make_openai_complete
from wsbench.extract import extract_text
from wsbench.report import TaskOutcome, render
from wsbench.runner import final_assistant_message, run_split
from wsbench.staging import eligibility

RUNS_DIR = pathlib.Path(__file__).parent.parent / "runs"
DEFAULT_CONCURRENCY = 3
DEFAULT_JUDGE_MODEL = "claude-sonnet-5"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wsbench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run a split against the agent")
    run.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    run.add_argument("--limit", type=int, default=None,
                     help="Run only the first N tasks (pilot runs)")
    run.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    run.add_argument("--tasks", default=None, metavar="IDS",
                     help="Comma-separated task ids to run. For verifying "
                          "a specific fix; never the basis for a claim.")
    run.add_argument("--wall-clock-cap", type=float, default=1800.0)
    run.add_argument("--run-id", default=None)

    judge = sub.add_parser("judge", help="Grade a run's stored outputs")
    judge.add_argument("run_id")
    judge.add_argument("--split", choices=["dev", "holdout", "all"],
                       default="dev",
                       help="Split the run came from (to recover rubrics)")
    judge.add_argument("--concurrency", type=int, default=4)
    judge.add_argument("--overwrite", action="store_true",
                       help="Re-judge tasks that already have scores.json")

    report = sub.add_parser("report", help="Render a run report")
    report.add_argument("run_id")
    report.add_argument("--compare", default=None, metavar="RUN_ID")

    return parser


def next_run_id(runs_dir: pathlib.Path, prefix: str) -> str:
    """Next id in the ``<prefix>-NNN`` sequence, from 001.

    Numbered per prefix (smoke runs never shift dev numbering) and
    derived from the highest existing number rather than the entry
    count, so a stray file in runs/ cannot shift ids -- the wart the
    GAIA README warns about.
    """
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
        raise SystemExit(f"{name} is not set -- see wsbench/cli.py docstring")
    return value


def select_tasks(tasks: list[Task], spec: str | None) -> list[Task]:
    """Filter to the comma-separated ids in *spec*.

    An unmatched id is an error, not a silent skip: quietly running 2 of
    3 requested tasks would look like a passing verification of something
    that was never exercised.
    """
    if not spec:
        return tasks
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    by_id = {t.task_id: t for t in tasks}
    unmatched = [w for w in wanted if w not in by_id]
    if unmatched:
        raise SystemExit(f"no task in this split matches: {', '.join(unmatched)}")
    return [by_id[w] for w in wanted]


def save_outcomes(path: str, outcomes: list[TaskOutcome]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([dataclasses.asdict(o) for o in outcomes], fh, indent=2)


def load_outcomes(path: str) -> list[TaskOutcome]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [TaskOutcome(**row) for row in json.load(fh)]


async def _cmd_run(args: argparse.Namespace) -> int:
    # Naming convention: full-split runs are counted runs and take the
    # split's own sequence (dev-001, ...); anything filtered by --limit
    # or --tasks is a pilot and lands in the smoke-001, ... sequence.
    prefix = "smoke" if (args.limit or args.tasks) else args.split
    run_id = args.run_id or next_run_id(RUNS_DIR, prefix)
    out_dir = _run_dir(run_id)

    print("loading dataset (cached after the first download)...")
    tasks = load_tasks(args.split)

    # Eligibility gate: skipped-and-reported, never silently dropped.
    runnable: list[Task] = []
    skipped: list[tuple[str, str]] = []
    for t in tasks:
        reason = eligibility(t)
        if reason:
            skipped.append((t.task_id, reason))
        else:
            runnable.append(t)
    for task_id, reason in skipped:
        print(f"skip {task_id}: {reason}")

    runnable = select_tasks(runnable, args.tasks)
    if args.limit:
        runnable = runnable[: args.limit]

    print(f"run {run_id}: {len(runnable)} task(s), "
          f"{len(skipped)} skipped, concurrency {args.concurrency}")

    async with HarnessClient(
        base_url=os.environ.get("WSBENCH_BASE_URL", "http://localhost:8000"),
        token=_require_env("SUROGATES_SA_TOKEN"),
        agent_id=_require_env("WSBENCH_AGENT_ID"),
    ) as client:
        results = await run_split(
            client, runnable, out_dir=str(out_dir),
            concurrency=args.concurrency,
            wall_clock_cap_s=args.wall_clock_cap,
        )

    by_id = {t.task_id: t for t in runnable}
    for r in results:
        task = by_id[r.task_id]
        # Human-readable trace next to the raw events, so a failing task
        # can be understood by reading rather than by re-running it.
        from wsbench.judge import format_trajectory

        (out_dir / "tasks" / r.task_id / "trajectory.md").write_text(
            f"# task {r.task_id} ({task.difficulty}, {task.persona})\n\n"
            f"**Instruction:** {task.instruction}\n\n"
            f"**Expected outputs:** {', '.join(task.output_files)}\n\n"
            f"**Collected:** {', '.join(c.workspace_path for c in r.collected) or '(none)'}\n\n"
            f"**Missing:** {', '.join(r.missing_outputs) or '(none)'}\n\n"
            f"**Status:** {r.terminal_status} ({r.wall_clock_s:.1f}s)"
            + (f"\n\n**Error:** {r.error}" if r.error else "")
            + f"\n\n## Trajectory\n\n```\n{format_trajectory(r.events, 40000)}\n```\n",
            encoding="utf-8",
        )

    with open(out_dir / "rollout.json", "w", encoding="utf-8") as fh:
        json.dump({
            "run_id": run_id,
            "split": args.split,
            "skipped": [{"task_id": tid, "reason": why} for tid, why in skipped],
            "tasks": [
                {
                    "task_id": r.task_id,
                    "terminal_status": r.terminal_status,
                    "error": r.error,
                    "wall_clock_s": r.wall_clock_s,
                    "collected": len(r.collected),
                    "missing_outputs": r.missing_outputs,
                }
                for r in results
            ],
        }, fh, indent=2)

    done = sum(1 for r in results if r.terminal_status == "completed")
    print(f"run {run_id}: {done}/{len(results)} sessions completed")
    print(f"next: wsbench judge {run_id} --split {args.split}")
    return 0


def _load_events(out_dir: pathlib.Path, task_id: str) -> list[Event]:
    path = out_dir / "tasks" / task_id / "events.jsonl"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return [
            Event(id=row["id"], type=row["type"], data=row["data"])
            for row in map(json.loads, fh)
        ]


def _outcome_from_verdicts(
    task: Task,
    meta: dict,
    verdicts: list[RubricVerdict],
    judge_error: str | None,
) -> TaskOutcome:
    by_type: dict[str, list[int]] = {}
    for v in verdicts:
        agg = by_type.setdefault(v.rubric_type, [0, 0])
        agg[0] += int(v.passed)
        agg[1] += 1
    return TaskOutcome(
        task_id=task.task_id,
        persona=task.persona,
        difficulty=task.difficulty,
        total_rubrics=len(task.rubrics),
        passed_rubrics=sum(1 for v in verdicts if v.passed),
        by_rubric_type=by_type,
        missing_outputs=list(meta.get("missing_outputs") or []),
        terminal_status=str(meta.get("terminal_status") or ""),
        error=meta.get("error"),
        judge_error=judge_error,
    )


async def _cmd_judge(args: argparse.Namespace) -> int:
    out_dir = _run_dir(args.run_id)
    tasks_by_id = {t.task_id: t for t in load_tasks(args.split)}

    task_dirs = sorted(
        p for p in (out_dir / "tasks").glob("*") if (p / "meta.json").exists()
    ) if (out_dir / "tasks").exists() else []
    if not task_dirs:
        raise SystemExit(f"no task traces in run {args.run_id}")

    complete = make_openai_complete(
        base_url=_require_env("WSBENCH_JUDGE_BASE_URL"),
        api_key=_require_env("WSBENCH_JUDGE_KEY"),
        model=os.environ.get("WSBENCH_JUDGE_MODEL", DEFAULT_JUDGE_MODEL),
    )
    sem = asyncio.Semaphore(args.concurrency)

    async def one(task_dir: pathlib.Path) -> TaskOutcome | None:
        task_id = task_dir.name
        task = tasks_by_id.get(task_id)
        if task is None:
            print(f"  ! {task_id}: not in split {args.split}, skipping")
            return None
        with open(task_dir / "meta.json", encoding="utf-8") as fh:
            meta = json.load(fh)

        scores_path = task_dir / "scores.json"
        if scores_path.exists() and not args.overwrite:
            with open(scores_path, encoding="utf-8") as fh:
                stored = json.load(fh)
            verdicts = [RubricVerdict(**row) for row in stored["verdicts"]]
            return _outcome_from_verdicts(
                task, meta, verdicts, stored.get("judge_error")
            )

        events = _load_events(out_dir, task_id)
        files = []
        for c in meta.get("collected") or []:
            text, note = extract_text(str(task_dir / c["local_relpath"]))
            files.append({
                "workspace_path": c["workspace_path"],
                "text": text,
                "note": note,
            })

        judge_error: str | None = None
        if not files and not events:
            # Nothing to judge -- the rollout never got off the ground.
            # Score it locally instead of paying for a judge call that
            # can only say "no evidence".
            verdicts = [
                RubricVerdict(
                    index=i, rubric=r,
                    rubric_type=(list(task.rubric_types) + ["Unspecified"] * len(task.rubrics))[i],
                    passed=False, confidence=1.0,
                    evidence=f"no session output ({meta.get('error') or 'empty rollout'})",
                )
                for i, r in enumerate(task.rubrics)
            ]
        else:
            async with sem:
                try:
                    verdicts = await judge_task(
                        complete, task, files, events,
                        final_assistant_message(events),
                    )
                except Exception as exc:  # noqa: BLE001
                    # One bad verdict must not lose the rest of the run.
                    judge_error = f"{type(exc).__name__}: {exc}"
                    verdicts = [
                        RubricVerdict(
                            index=i, rubric=r, rubric_type="Unspecified",
                            passed=False, confidence=0.0,
                            evidence=f"judge failed: {judge_error[:200]}",
                        )
                        for i, r in enumerate(task.rubrics)
                    ]

        with open(scores_path, "w", encoding="utf-8") as fh:
            json.dump({
                "task_id": task_id,
                "judge_model": os.environ.get(
                    "WSBENCH_JUDGE_MODEL", DEFAULT_JUDGE_MODEL
                ),
                "judge_error": judge_error,
                "summary": {
                    "total": len(verdicts),
                    "passed": sum(1 for v in verdicts if v.passed),
                },
                "verdicts": [dataclasses.asdict(v) for v in verdicts],
            }, fh, indent=2)

        outcome = _outcome_from_verdicts(task, meta, verdicts, judge_error)
        print(f"  {task_id}: {outcome.passed_rubrics}/{outcome.total_rubrics}"
              + (f" (judge error)" if judge_error else ""))
        return outcome

    results = await asyncio.gather(*(one(d) for d in task_dirs))
    outcomes = [o for o in results if o is not None]
    save_outcomes(str(out_dir / "outcomes.json"), outcomes)

    total = sum(o.total_rubrics for o in outcomes)
    passed = sum(o.passed_rubrics for o in outcomes)
    pct = 100.0 * passed / total if total else 0.0
    print(f"run {args.run_id}: {passed}/{total} rubrics ({pct:.1f}%)")
    print(f"next: wsbench report {args.run_id}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    outcomes = load_outcomes(str(_run_dir(args.run_id) / "outcomes.json"))
    if not outcomes:
        raise SystemExit(
            f"no outcomes for run {args.run_id} -- run `wsbench judge` first"
        )
    previous = (
        load_outcomes(str(_run_dir(args.compare) / "outcomes.json"))
        if args.compare else None
    )
    text = render(outcomes, previous=previous, run_id=args.run_id)
    path = _run_dir(args.run_id) / "report.md"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nwritten to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "judge":
        return asyncio.run(_cmd_judge(args))
    return _cmd_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
