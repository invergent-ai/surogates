"""Command-line entry point for the GAIA experiment.

    gaia-bench run --split dev
    gaia-bench analyze <run_id>
    gaia-bench report <run_id> --compare <previous_run_id>

Environment:
    SUROGATES_SA_TOKEN  surogates service-account token (Bearer auth for
                        every /v1/api/* call). NOT a GAIA or HuggingFace
                        credential -- mint it with
                        POST /v1/admin/service-accounts.
    GAIA_BASE_URL       harness API base (default http://localhost:8000)
    GAIA_AGENT_ID       agent under test
    HF_TOKEN            HuggingFace token with GAIA terms accepted

GAIA_BASE_URL and GAIA_AGENT_ID deliberately keep the GAIA_ prefix: the
obvious SUROGATES_ equivalents are already taken by the harness itself
(SUROGATES_API_URL feeds its ApiSettings, SUROGATES_AGENT_ID is a real
harness variable), so reusing them would cross-talk when the harness and
the benchmark share a shell.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import pathlib

from gaia_bench.attribute import attribute, format_trajectory
from gaia_bench.client import Event, HarnessClient
from gaia_bench.dataset import load_tasks
from gaia_bench.detectors import detect
from gaia_bench.judge import make_openai_complete
from gaia_bench.report import TaskOutcome, render
from gaia_bench.runner import run_split
from gaia_bench.scorer import lenient_scorer, question_scorer

RUNS_DIR = pathlib.Path(__file__).parent.parent / "runs"
# The local worker runs concurrency: 10 (config.dev.yaml). Beyond that,
# tasks queue rather than run and per-task timings stop meaning anything.
DEFAULT_CONCURRENCY = 4
# Deliberately the base tier, not pro. Attribution is a labelling job, and
# opus-5 through the yunwu proxy can spend its whole max_tokens on hidden
# reasoning and return empty -- the exact failure a strict-JSON task hits.
DEFAULT_JUDGE_MODEL = "claude-sonnet-5"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gaia-bench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run a split against the agent")
    run.add_argument("--split", choices=["dev", "holdout", "all"],
                     default="dev")
    run.add_argument("--limit", type=int, default=None,
                     help="Run only the first N tasks (pilot runs)")
    run.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    run.add_argument("--only-failing", default=None, metavar="RUN_ID",
                     help="Re-run only tasks that failed in RUN_ID. Fast "
                          "filter; never a basis for a claim.")
    run.add_argument("--tasks", default=None, metavar="IDS",
                     help="Comma-separated task ids (8-char prefixes accepted) "
                          "to run. For verifying a specific fix.")
    run.add_argument("--wall-clock-cap", type=float, default=1800.0)
    run.add_argument("--run-id", default=None)

    analyze = sub.add_parser("analyze", help="Attribute failures in a run")
    analyze.add_argument("run_id")
    analyze.add_argument("--split", choices=["dev", "holdout", "all"],
                         default="dev",
                         help="Split the run came from (to recover questions)")
    analyze.add_argument("--concurrency", type=int, default=4)

    report = sub.add_parser("report", help="Render a run report")
    report.add_argument("run_id")
    report.add_argument("--compare", default=None, metavar="RUN_ID")

    return parser


def save_outcomes(path: str, outcomes: list[TaskOutcome]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([dataclasses.asdict(o) for o in outcomes], fh, indent=2)


def load_outcomes(path: str) -> list[TaskOutcome]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [TaskOutcome(**row) for row in json.load(fh)]


def select_tasks(tasks: list, spec: str | None) -> list:
    """Filter *tasks* to the comma-separated ids in *spec* (prefixes allowed).

    An unmatched id is an error, not a silent skip: quietly running 2 of 3
    requested tasks would look like a passing verification of something that
    was never exercised.
    """
    if not spec:
        return tasks
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    picked, unmatched = [], []
    for want in wanted:
        hits = [t for t in tasks if t.task_id.startswith(want)]
        if not hits:
            unmatched.append(want)
        picked.extend(hits)
    if unmatched:
        raise SystemExit(f"no task in this split matches: {', '.join(unmatched)}")
    return picked


def _run_dir(run_id: str) -> pathlib.Path:
    d = RUNS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set -- see gaia_bench/cli.py docstring")
    return value


async def _cmd_run(args: argparse.Namespace) -> int:
    run_id = args.run_id or f"{args.split}-{len(list(RUNS_DIR.glob('*'))) + 1:03d}"
    out_dir = _run_dir(run_id)

    tasks = load_tasks(args.split)

    if args.only_failing:
        prior = load_outcomes(str(_run_dir(args.only_failing) / "outcomes.json"))
        failing = {o.task_id for o in prior if not o.strict_pass}
        tasks = [t for t in tasks if t.task_id in failing]
        print(f"only-failing: {len(tasks)} task(s) from {args.only_failing}")

    tasks = select_tasks(tasks, args.tasks)

    if args.limit:
        tasks = tasks[: args.limit]

    print(f"run {run_id}: {len(tasks)} task(s), concurrency {args.concurrency}")

    async with HarnessClient(
        base_url=os.environ.get("GAIA_BASE_URL", "http://localhost:8000"),
        token=_require_env("SUROGATES_SA_TOKEN"),
        agent_id=_require_env("GAIA_AGENT_ID"),
    ) as client:
        results = await run_split(
            client, tasks, out_dir=str(out_dir),
            concurrency=args.concurrency,
            wall_clock_cap_s=args.wall_clock_cap,
        )

    by_id = {t.task_id: t for t in tasks}
    outcomes = []
    for r in results:
        task = by_id[r.task_id]
        answer = r.answer or ""

        # Human-readable trace next to the raw events, so a failing task
        # can be understood by reading rather than by re-running it.
        (out_dir / "tasks" / r.task_id / "trajectory.md").write_text(
            f"# {r.task_id} (level {task.level})\n\n"
            f"**Question:** {task.question}\n\n"
            f"**Expected:** {task.final_answer}\n\n"
            f"**Got:** {r.answer!r}\n\n"
            f"**Status:** {r.terminal_status} "
            f"({r.wall_clock_s:.1f}s)\n\n"
            f"## Trajectory\n\n```\n{format_trajectory(r.events)}\n```\n",
            encoding="utf-8",
        )

        outcomes.append(TaskOutcome(
            task_id=r.task_id,
            level=task.level,
            strict_pass=bool(answer) and question_scorer(answer, task.final_answer),
            lenient_pass=bool(answer) and lenient_scorer(answer, task.final_answer),
            flags=detect(r, task.level),
        ))

    save_outcomes(str(out_dir / "outcomes.json"), outcomes)
    passed = sum(1 for o in outcomes if o.strict_pass)
    print(f"run {run_id}: {passed}/{len(outcomes)} strict pass")
    print(f"next: gaia-bench analyze {run_id}")
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


async def _cmd_analyze(args: argparse.Namespace) -> int:
    out_dir = _run_dir(args.run_id)
    outcomes = load_outcomes(str(out_dir / "outcomes.json"))
    if not outcomes:
        raise SystemExit(f"no outcomes for run {args.run_id}")

    # Only the residue: failures no deterministic detector could explain.
    # Everything else already has an owner and would just cost tokens.
    unexplained = [o for o in outcomes if not o.strict_pass and not o.flags]
    failing = sum(1 for o in outcomes if not o.strict_pass)
    print(
        f"{len(unexplained)} unexplained failure(s) to attribute "
        f"({failing} failing of {len(outcomes)} total)"
    )
    if not unexplained:
        print("nothing to attribute")
        return 0

    complete = make_openai_complete(
        base_url=_require_env("GAIA_JUDGE_BASE_URL"),
        api_key=_require_env("GAIA_JUDGE_KEY"),
        model=os.environ.get("GAIA_JUDGE_MODEL", DEFAULT_JUDGE_MODEL),
    )
    tasks_by_id = {t.task_id: t for t in load_tasks(args.split)}
    sem = asyncio.Semaphore(args.concurrency)

    async def one(o: TaskOutcome) -> tuple[str, dict | None, str]:
        task = tasks_by_id.get(o.task_id)
        if task is None:
            return o.task_id, None, "task not in split"
        meta_path = out_dir / "tasks" / o.task_id / "meta.json"
        answer = None
        if meta_path.exists():
            with open(meta_path, encoding="utf-8") as fh:
                answer = json.load(fh).get("answer")
        async with sem:
            try:
                verdict = await attribute(
                    complete,
                    question=task.question,
                    ground_truth=task.final_answer,
                    model_answer=answer,
                    events=_load_events(out_dir, o.task_id),
                )
                return o.task_id, verdict, ""
            except Exception as exc:  # noqa: BLE001 - reported, not fatal
                # One bad verdict must not lose the other 18. An
                # unattributed failure is visible in the report as such.
                return o.task_id, None, f"{type(exc).__name__}: {exc}"

    results = await asyncio.gather(*(one(o) for o in unexplained))

    by_id = {o.task_id: o for o in outcomes}
    attributed = 0
    for task_id, verdict, err in results:
        if verdict is None:
            print(f"  ! {task_id[:8]} unattributed: {err[:120]}")
            continue
        by_id[task_id].root_cause = verdict["root_cause"]
        by_id[task_id].owner = verdict["owner"]
        by_id[task_id].evidence = verdict.get("evidence", "")
        by_id[task_id].hypothesis = verdict.get("hypothesis", "")
        attributed += 1

    save_outcomes(str(out_dir / "outcomes.json"), outcomes)
    print(f"attributed {attributed}/{len(unexplained)}")
    print(f"next: gaia-bench report {args.run_id}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    outcomes = load_outcomes(str(_run_dir(args.run_id) / "outcomes.json"))
    if not outcomes:
        raise SystemExit(f"no outcomes for run {args.run_id}")
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
    if args.command == "analyze":
        return asyncio.run(_cmd_analyze(args))
    return _cmd_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
