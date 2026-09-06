"""Command-line entry point for the DABstep experiment.

    dabbench build-key                     # derive the local answer key (once)
    dabbench run --split dev --limit 5     # smoke: context upload + rollout
    dabbench score <run_id> --split dev    # grade with the vendored scorer
    dabbench report <run_id> --compare <previous_run_id>
    dabbench export <run_id>               # submission.jsonl for the leaderboard

Environment:
    SUROGATES_SA_TOKEN   surogates service-account token (Bearer auth for
                         every /v1/api/* call) -- same token the sibling
                         benchmarks use; minting is documented in
                         benchmarks/gaia/README.md.
    DABSTEP_BASE_URL     harness API base (default http://localhost:8000)
    DABSTEP_AGENT_ID     agent under test

DABSTEP_BASE_URL / DABSTEP_AGENT_ID keep their own prefix on purpose:
SUROGATES_API_URL and SUROGATES_AGENT_ID are real harness variables, and
reusing them cross-talks when the harness and the benchmark share a
shell. There is no judge -- grading is the vendored official scorer
against the derived key.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import pathlib

from dabbench.answers import build_key, load_key
from dabbench.client import HarnessClient
from dabbench.dataset import Task, load_tasks
from dabbench.official_scorer import question_scorer
from dabbench.report import TaskOutcome, render
from dabbench.runner import run_split

RUNS_DIR = pathlib.Path(__file__).parent.parent / "runs"
DEFAULT_CONCURRENCY = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dabbench")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("build-key", help="Derive the local answer key")

    run = sub.add_parser("run", help="Run a split against the agent")
    run.add_argument("--split",
                     choices=["dev", "holdout", "all", "upstream-dev"],
                     default="dev")
    run.add_argument("--limit", type=int, default=None,
                     help="Run only the first N tasks (pilot runs)")
    run.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    run.add_argument("--tasks", default=None, metavar="IDS",
                     help="Comma-separated task ids. For verifying a "
                          "specific fix; never the basis for a claim.")
    run.add_argument("--wall-clock-cap", type=float, default=1800.0)
    run.add_argument("--run-id", default=None)

    score = sub.add_parser("score", help="Grade a run's stored answers")
    score.add_argument("run_id")
    score.add_argument("--split",
                       choices=["dev", "holdout", "all", "upstream-dev"],
                       default="dev",
                       help="Split the run came from (to recover levels)")

    report = sub.add_parser("report", help="Render a run report")
    report.add_argument("run_id")
    report.add_argument("--compare", default=None, metavar="RUN_ID")

    export = sub.add_parser(
        "export", help="Write submission.jsonl in the leaderboard format"
    )
    export.add_argument("run_id")

    return parser


def next_run_id(runs_dir: pathlib.Path, prefix: str) -> str:
    """Next id in the ``<prefix>-NNN`` sequence, from 001, per prefix."""
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
        raise SystemExit(f"{name} is not set -- see dabbench/cli.py docstring")
    return value


def select_tasks(tasks: list[Task], spec: str | None) -> list[Task]:
    """An unmatched id is an error, not a silent skip."""
    if not spec:
        return tasks
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    by_id = {t.task_id: t for t in tasks}
    unmatched = [w for w in wanted if w not in by_id]
    if unmatched:
        raise SystemExit(f"no task in this split matches: {', '.join(unmatched)}")
    return [by_id[w] for w in wanted]


async def _cmd_run(args: argparse.Namespace) -> int:
    # Pilots (--limit/--tasks) land in the smoke sequence; full-split
    # runs take the split's own counted sequence.
    prefix = "smoke" if (args.limit or args.tasks) else args.split
    run_id = args.run_id or next_run_id(RUNS_DIR, prefix)
    out_dir = _run_dir(run_id)

    print("loading dataset (cached after the first download)...")
    tasks = load_tasks(args.split)
    tasks = select_tasks(tasks, args.tasks)
    if args.limit:
        tasks = tasks[: args.limit]

    print(f"run {run_id}: {len(tasks)} task(s), concurrency {args.concurrency}")

    async with HarnessClient(
        base_url=os.environ.get("DABSTEP_BASE_URL", "http://localhost:8000"),
        token=_require_env("SUROGATES_SA_TOKEN"),
        agent_id=_require_env("DABSTEP_AGENT_ID"),
    ) as client:
        results = await run_split(
            client, tasks, out_dir=str(out_dir),
            concurrency=args.concurrency,
            wall_clock_cap_s=args.wall_clock_cap,
        )

    with open(out_dir / "rollout.json", "w", encoding="utf-8") as fh:
        json.dump({
            "run_id": run_id,
            "split": args.split,
            "tasks": [
                {
                    "task_id": r.task_id,
                    "answer": r.answer,
                    "terminal_status": r.terminal_status,
                    "error": r.error,
                    "wall_clock_s": r.wall_clock_s,
                }
                for r in results
            ],
        }, fh, indent=2)

    answered = sum(1 for r in results if r.answer is not None)
    done = sum(1 for r in results if r.terminal_status == "completed")
    print(f"run {run_id}: {done}/{len(results)} sessions completed, "
          f"{answered} with a FINAL ANSWER")
    print(f"next: dabbench score {run_id} --split {args.split}")
    return 0


def _load_meta(out_dir: pathlib.Path) -> list[dict]:
    metas = []
    base = out_dir / "tasks"
    if base.is_dir():
        for task_dir in sorted(base.iterdir()):
            meta_path = task_dir / "meta.json"
            if meta_path.exists():
                with open(meta_path, encoding="utf-8") as fh:
                    metas.append(json.load(fh))
    return metas


def _cmd_score(args: argparse.Namespace) -> int:
    out_dir = _run_dir(args.run_id)
    metas = _load_meta(out_dir)
    if not metas:
        raise SystemExit(f"no task traces in run {args.run_id}")

    tasks_by_id = {t.task_id: t for t in load_tasks(args.split)}
    key = load_key()
    entries = key["entries"]

    outcomes: list[TaskOutcome] = []
    for meta in metas:
        task = tasks_by_id.get(meta["task_id"])
        if task is None:
            print(f"  ! {meta['task_id']}: not in split {args.split}, skipping")
            continue
        answer = meta.get("answer")
        flags = []
        if answer is None:
            flags.append("no_final_answer")
        if meta.get("error"):
            flags.append("infra_error")

        # The 10 upstream-dev tasks carry true ground truth on the task
        # itself; everything else grades against the derived key.
        reference = task.answer or None
        source = "upstream-dev" if reference else ""
        if reference is None:
            entry = entries.get(task.task_id)
            if entry:
                reference = entry["answer"]
                source = entry["source"]

        if reference is None:
            correct = None
            flags.append("no_key_entry")
        elif answer is None:
            correct = False
        else:
            correct = bool(question_scorer(answer, reference))

        outcomes.append(TaskOutcome(
            task_id=task.task_id,
            level=task.level,
            answer=answer,
            correct=correct,
            key_source=source,
            terminal_status=str(meta.get("terminal_status") or ""),
            error=meta.get("error"),
            flags=flags,
        ))

    with open(out_dir / "outcomes.json", "w", encoding="utf-8") as fh:
        json.dump([dataclasses.asdict(o) for o in outcomes], fh, indent=2)

    gradable = [o for o in outcomes if o.correct is not None]
    correct = sum(o.correct for o in gradable)
    pct = 100.0 * correct / len(gradable) if gradable else 0.0
    print(f"run {args.run_id}: {correct}/{len(gradable)} correct ({pct:.1f}%), "
          f"{len(outcomes) - len(gradable)} ungradable")
    print(f"next: dabbench report {args.run_id}")
    return 0


def load_outcomes(path: pathlib.Path) -> list[TaskOutcome]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return [TaskOutcome(**row) for row in json.load(fh)]


def _cmd_report(args: argparse.Namespace) -> int:
    outcomes = load_outcomes(_run_dir(args.run_id) / "outcomes.json")
    if not outcomes:
        raise SystemExit(
            f"no outcomes for run {args.run_id} -- run `dabbench score` first"
        )
    previous = (
        load_outcomes(_run_dir(args.compare) / "outcomes.json")
        if args.compare else None
    )
    text = render(outcomes, previous=previous, run_id=args.run_id)
    path = _run_dir(args.run_id) / "report.md"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nwritten to {path}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    out_dir = _run_dir(args.run_id)
    metas = _load_meta(out_dir)
    if not metas:
        raise SystemExit(f"no task traces in run {args.run_id}")
    path = out_dir / "submission.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for meta in metas:
            fh.write(json.dumps({
                "task_id": str(meta["task_id"]),
                "agent_answer": str(meta.get("answer") or "Not Applicable"),
            }, ensure_ascii=False) + "\n")
    print(f"{len(metas)} answers written to {path}")
    print("submit via the form on https://huggingface.co/spaces/adyen/DABstep")
    return 0


def _cmd_build_key() -> int:
    key = build_key()
    print(f"answer key: {key['covered']}/{key['total']} tasks covered")
    if key["uncovered_task_ids"]:
        print(f"uncovered: {', '.join(key['uncovered_task_ids'][:20])}"
              + (" ..." if len(key["uncovered_task_ids"]) > 20 else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "build-key":
        return _cmd_build_key()
    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "score":
        return _cmd_score(args)
    if args.command == "export":
        return _cmd_export(args)
    return _cmd_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
