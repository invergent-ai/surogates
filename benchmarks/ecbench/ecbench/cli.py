"""Command-line entry point for the ECBench experiment.

    ecbench run --max-days 30 --episodes 1    # smoke: one short episode
    ecbench run --episodes 3                  # counted: 3 full-year episodes
    ecbench score <run_id>                    # settle + outcomes.json (offline)
    ecbench report <run_id>                   # render report.md

Environment:
    SUROGATES_SA_TOKEN   surogates service-account token (Bearer auth for
                         every /v1/api/* call) -- same token the sibling
                         benchmarks use; minting is documented in
                         benchmarks/gaia/README.md.
    ECBENCH_BASE_URL     harness API base (default http://localhost:8000)
    ECBENCH_AGENT_ID     agent under test
    ECBENCH_HOME         E-CommerceBench checkout (default vendor/E-CommerceBench)

ECBENCH_BASE_URL / ECBENCH_AGENT_ID keep their own prefix on purpose:
SUROGATES_API_URL and SUROGATES_AGENT_ID are real harness variables, and
reusing them cross-talks when the harness and the benchmark share a
shell. There is no judge configuration -- scoring is deterministic.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import pathlib

from ecbench import vendor
from ecbench.client import HarnessClient
from ecbench.report import render
from ecbench.runner import run_episodes
from ecbench.scorer import EpisodeOutcome, score_episode

RUNS_DIR = pathlib.Path(__file__).parent.parent / "runs"
FULL_HORIZON_DAYS = 365


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ecbench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run episodes against the agent")
    run.add_argument("--episodes", type=int, default=1)
    run.add_argument("--max-days", type=int, default=FULL_HORIZON_DAYS)
    run.add_argument("--balance", type=float, default=100000.0)
    run.add_argument("--concurrency", type=int, default=1,
                     help="Parallel episodes. Keep at 1: long provider-heavy "
                          "sessions are what the tier throttles on.")
    run.add_argument("--wall-clock-cap", type=float, default=14400.0,
                     help="Per-episode cap in seconds (default 4 h)")
    run.add_argument("--run-id", default=None)

    score = sub.add_parser("score", help="Score a run's stored artifacts")
    score.add_argument("run_id")

    report = sub.add_parser("report", help="Render a run report")
    report.add_argument("run_id")

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
        raise SystemExit(f"{name} is not set -- see ecbench/cli.py docstring")
    return value


async def _cmd_run(args: argparse.Namespace) -> int:
    commit = vendor.verify_pin()
    # Naming convention: full-horizon runs are counted and take the
    # ``year`` sequence; anything shorter is a pilot in ``smoke``.
    prefix = "year" if args.max_days >= FULL_HORIZON_DAYS else "smoke"
    run_id = args.run_id or next_run_id(RUNS_DIR, prefix)
    out_dir = _run_dir(run_id)

    print(f"run {run_id}: {args.episodes} episode(s) x {args.max_days} "
          f"day(s), pin {commit[:12]}, concurrency {args.concurrency}")

    async with HarnessClient(
        base_url=os.environ.get("ECBENCH_BASE_URL", "http://localhost:8000"),
        token=_require_env("SUROGATES_SA_TOKEN"),
        agent_id=_require_env("ECBENCH_AGENT_ID"),
    ) as client:
        results = await run_episodes(
            client, str(out_dir),
            episodes=args.episodes,
            max_days=args.max_days,
            balance=args.balance,
            concurrency=args.concurrency,
            wall_clock_cap_s=args.wall_clock_cap,
        )

    with open(out_dir / "rollout.json", "w", encoding="utf-8") as fh:
        json.dump({
            "run_id": run_id,
            "pin": commit,
            "max_days": args.max_days,
            "balance": args.balance,
            "episodes": [
                {
                    "episode": r.episode,
                    "terminal_status": r.terminal_status,
                    "error": r.error,
                    "wall_clock_s": r.wall_clock_s,
                    "artifacts": r.artifacts,
                }
                for r in results
            ],
        }, fh, indent=2)

    done = sum(1 for r in results if r.terminal_status == "completed")
    print(f"run {run_id}: {done}/{len(results)} sessions completed")
    print(f"next: ecbench score {run_id}")
    return 0


def _episode_dirs(out_dir: pathlib.Path) -> list[pathlib.Path]:
    base = out_dir / "episodes"
    if not base.is_dir():
        raise SystemExit(f"no episodes in run {out_dir.name}")
    return sorted(p for p in base.iterdir() if (p / "meta.json").exists())


def _cmd_score(args: argparse.Namespace) -> int:
    out_dir = _run_dir(args.run_id)
    outcomes = [score_episode(str(d)) for d in _episode_dirs(out_dir)]
    with open(out_dir / "outcomes.json", "w", encoding="utf-8") as fh:
        json.dump([dataclasses.asdict(o) for o in outcomes], fh, indent=2)
    for o in outcomes:
        assets = f"{o.final_assets:,.0f}" if o.final_assets is not None else "--"
        print(f"  episode {o.episode:02d}: {assets} ({o.source})")
    print(f"next: ecbench report {args.run_id}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    out_dir = _run_dir(args.run_id)
    path = out_dir / "outcomes.json"
    if not path.exists():
        raise SystemExit(
            f"no outcomes for run {args.run_id} -- run `ecbench score` first"
        )
    with open(path, encoding="utf-8") as fh:
        outcomes = [EpisodeOutcome(**row) for row in json.load(fh)]
    text = render(outcomes, run_id=args.run_id)
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    print(text)
    print(f"\nwritten to {out_dir / 'report.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "score":
        return _cmd_score(args)
    return _cmd_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
