"""Command-line entry point for the Agent Leaderboard experiment.

    galbench run --split dev --limit 3     # smoke: protocol + simulators
    galbench judge <run_id> --split dev    # AC/TSQ grading from stored traces
    galbench report <run_id> --compare <previous_run_id>

Environment:
    SUROGATES_SA_TOKEN     surogates service-account token -- same as the
                           sibling benchmarks (see benchmarks/gaia/README.md).
    GALILEO_BASE_URL       harness API base (default http://localhost:8000)
    GALILEO_AGENT_ID       agent under test
    GALILEO_SIM_BASE_URL   OpenAI-compatible endpoint powering the user
                           simulator, the tool simulator AND the judge
    GALILEO_SIM_KEY        its API key
    GALILEO_SIM_MODEL      its model id

GALILEO_BASE_URL / GALILEO_AGENT_ID keep their own prefix on purpose:
the SUROGATES_ equivalents are real harness variables, and reusing them
cross-talks when the harness and the benchmark share a shell.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import pathlib

from galbench.client import HarnessClient
from galbench.dataset import DOMAINS, Scenario, load_scenarios
from galbench.judge import (
    judge_action_completion,
    judge_tool_selection,
    make_openai_complete,
)
from galbench.protocol import tool_catalog
from galbench.report import ScenarioOutcome, render
from galbench.runner import run_split
from galbench.sim import make_chat

RUNS_DIR = pathlib.Path(__file__).parent.parent / "runs"
DEFAULT_CONCURRENCY = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="galbench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run a split against the agent")
    run.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    run.add_argument("--domains", default=None,
                     help="Comma-separated domain filter (default: all five)")
    run.add_argument("--limit", type=int, default=None,
                     help="Run only the first N scenarios (pilot runs)")
    run.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    run.add_argument("--scenarios", default=None, metavar="IDS",
                     help="Comma-separated scenario ids. For verifying a "
                          "specific fix; never the basis for a claim.")
    run.add_argument("--wall-clock-cap", type=float, default=2400.0)
    run.add_argument("--run-id", default=None)

    judge = sub.add_parser("judge", help="Grade a run's stored transcripts")
    judge.add_argument("run_id")
    judge.add_argument("--split", choices=["dev", "holdout", "all"],
                       default="dev")
    judge.add_argument("--concurrency", type=int, default=4)
    judge.add_argument("--overwrite", action="store_true",
                       help="Re-judge scenarios that already have scores.json")

    report = sub.add_parser("report", help="Render a run report")
    report.add_argument("run_id")
    report.add_argument("--compare", default=None, metavar="RUN_ID")

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
        raise SystemExit(f"{name} is not set -- see galbench/cli.py docstring")
    return value


def _chat_from_env():
    return make_chat(
        base_url=_require_env("GALILEO_SIM_BASE_URL"),
        api_key=_require_env("GALILEO_SIM_KEY"),
        model=_require_env("GALILEO_SIM_MODEL"),
    )


def select_scenarios(scenarios: list[Scenario], spec: str | None) -> list[Scenario]:
    """An unmatched id is an error, not a silent skip."""
    if not spec:
        return scenarios
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    by_id = {s.scenario_id: s for s in scenarios}
    unmatched = [w for w in wanted if w not in by_id]
    if unmatched:
        raise SystemExit(f"no scenario in this split matches: {', '.join(unmatched)}")
    return [by_id[w] for w in wanted]


def _domains(spec: str | None) -> tuple[str, ...]:
    if not spec:
        return DOMAINS
    picked = tuple(d.strip() for d in spec.split(",") if d.strip())
    unknown = [d for d in picked if d not in DOMAINS]
    if unknown:
        raise SystemExit(f"unknown domain(s): {', '.join(unknown)}")
    return picked


async def _cmd_run(args: argparse.Namespace) -> int:
    # Pilots (--limit/--scenarios/--domains subset) land in the smoke
    # sequence; full-split runs take the split's counted sequence.
    is_pilot = bool(args.limit or args.scenarios
                    or (args.domains and _domains(args.domains) != DOMAINS))
    prefix = "smoke" if is_pilot else args.split
    run_id = args.run_id or next_run_id(RUNS_DIR, prefix)
    out_dir = _run_dir(run_id)

    print("loading dataset (cached after the first download)...")
    scenarios = load_scenarios(args.split, domains=_domains(args.domains))
    scenarios = select_scenarios(scenarios, args.scenarios)
    if args.limit:
        scenarios = scenarios[: args.limit]

    print(f"run {run_id}: {len(scenarios)} scenario(s), "
          f"concurrency {args.concurrency}")

    chat = _chat_from_env()
    async with HarnessClient(
        base_url=os.environ.get("GALILEO_BASE_URL", "http://localhost:8000"),
        token=_require_env("SUROGATES_SA_TOKEN"),
        agent_id=_require_env("GALILEO_AGENT_ID"),
    ) as client:
        results = await run_split(
            client, chat, scenarios, out_dir=str(out_dir),
            concurrency=args.concurrency,
            wall_clock_cap_s=args.wall_clock_cap,
        )

    with open(out_dir / "rollout.json", "w", encoding="utf-8") as fh:
        json.dump({
            "run_id": run_id,
            "split": args.split,
            "scenarios": [
                {
                    "scenario_id": r.scenario_id,
                    "terminal_status": r.terminal_status,
                    "error": r.error,
                    "user_turns": r.user_turns,
                    "tool_calls": len(r.tool_calls),
                    "completed_marker": r.completed_marker,
                    "wall_clock_s": r.wall_clock_s,
                }
                for r in results
            ],
        }, fh, indent=2)

    done = sum(1 for r in results if r.terminal_status == "completed")
    print(f"run {run_id}: {done}/{len(results)} sessions completed")
    print(f"next: galbench judge {run_id} --split {args.split}")
    return 0


async def _cmd_judge(args: argparse.Namespace) -> int:
    out_dir = _run_dir(args.run_id)
    base = out_dir / "tasks"
    task_dirs = sorted(
        p for p in base.glob("*") if (p / "meta.json").exists()
    ) if base.exists() else []
    if not task_dirs:
        raise SystemExit(f"no scenario traces in run {args.run_id}")

    scenarios_by_id = {
        s.scenario_id: s for s in load_scenarios(args.split)
    }
    complete = make_openai_complete(
        base_url=_require_env("GALILEO_SIM_BASE_URL"),
        api_key=_require_env("GALILEO_SIM_KEY"),
        model=_require_env("GALILEO_SIM_MODEL"),
    )
    sem = asyncio.Semaphore(args.concurrency)

    async def one(task_dir: pathlib.Path) -> ScenarioOutcome | None:
        sid = task_dir.name
        scenario = scenarios_by_id.get(sid)
        if scenario is None:
            print(f"  ! {sid}: not in split {args.split}, skipping")
            return None
        with open(task_dir / "meta.json", encoding="utf-8") as fh:
            meta = json.load(fh)

        scores_path = task_dir / "scores.json"
        if scores_path.exists() and not args.overwrite:
            with open(scores_path, encoding="utf-8") as fh:
                stored = json.load(fh)
            return ScenarioOutcome(**stored["outcome"])

        transcript = list(meta.get("transcript") or [])
        calls = list(meta.get("tool_calls") or [])
        judge_error: str | None = None
        goal_rows: list = []
        call_rows: list = []
        async with sem:
            try:
                goal_rows = await judge_action_completion(
                    complete, scenario.user_goals, transcript
                )
                call_rows = await judge_tool_selection(
                    complete, calls, tool_catalog(scenario), transcript
                )
            except Exception as exc:  # noqa: BLE001 - one bad verdict
                judge_error = f"{type(exc).__name__}: {exc}"

        outcome = ScenarioOutcome(
            scenario_id=sid,
            domain=scenario.domain,
            goals_total=len(scenario.user_goals),
            goals_done=sum(1 for g in goal_rows if g.accomplished),
            calls_total=len(calls),
            calls_good=sum(1 for c in call_rows if c.good),
            user_turns=int(meta.get("user_turns") or 0),
            agent_messages=int(meta.get("agent_messages") or 0),
            completed_marker=bool(meta.get("completed_marker")),
            terminal_status=str(meta.get("terminal_status") or ""),
            error=meta.get("error"),
            judge_error=judge_error,
        )
        with open(scores_path, "w", encoding="utf-8") as fh:
            json.dump({
                "outcome": dataclasses.asdict(outcome),
                "goals": [dataclasses.asdict(g) for g in goal_rows],
                "calls": [dataclasses.asdict(c) for c in call_rows],
                "judge_model": os.environ.get("GALILEO_SIM_MODEL", ""),
            }, fh, indent=2)
        print(f"  {sid}: AC {outcome.ac:.2f}"
              + (f", TSQ {outcome.tsq:.2f}" if outcome.tsq is not None else "")
              + (" (judge error)" if judge_error else ""))
        return outcome

    results = await asyncio.gather(*(one(d) for d in task_dirs))
    outcomes = [o for o in results if o is not None]
    with open(out_dir / "outcomes.json", "w", encoding="utf-8") as fh:
        json.dump([dataclasses.asdict(o) for o in outcomes], fh, indent=2)

    if outcomes:
        from statistics import mean

        print(f"run {args.run_id}: AC {mean(o.ac for o in outcomes):.3f} "
              f"over {len(outcomes)} scenario(s)")
    print(f"next: galbench report {args.run_id}")
    return 0


def load_outcomes(path: pathlib.Path) -> list[ScenarioOutcome]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return [ScenarioOutcome(**row) for row in json.load(fh)]


def _cmd_report(args: argparse.Namespace) -> int:
    outcomes = load_outcomes(_run_dir(args.run_id) / "outcomes.json")
    if not outcomes:
        raise SystemExit(
            f"no outcomes for run {args.run_id} -- run `galbench judge` first"
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "judge":
        return asyncio.run(_cmd_judge(args))
    return _cmd_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
