"""Command-line entry point for the AppWorld experiment.

    awbench run --split dev --limit 1      # smoke: bind + tunnel + evaluate
    awbench run --split test_normal        # counted run
    awbench report <run_id>
    awbench cleanup                        # stray MCP rows after a crash

Environment:
    SUROGATES_SA_TOKEN  harness /v1/api/* auth (same as the siblings)
    AW_BASE_URL         harness API base (default http://localhost:8000)
    AW_AGENT_ID         agent under test -- lean: AppWorld MCP tools only
    AW_PROJECT_ID       ops project owning the MCP registrations
    AW_OPS_USER / AW_OPS_PASSWORD / AW_OPS_TOKEN   ops login
    AW_APIS_URL         local `appworld serve apis` (default :9000)
    AW_MCP_URL          local `appworld serve mcp http` (default :10000)
    AW_ADAPTER_PUBLIC_URL  optional: your own tunnel to the MCP server

Scoring is upstream's stateful test suites -- deterministic, no judge.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import pathlib

from awbench.client import HarnessClient
from awbench.registrar import Registrar
from awbench.report import TaskOutcome, render
from awbench.runner import run_task, write_trace
from awbench.tunnel import expose_adapter

RUNS_DIR = pathlib.Path(__file__).parent.parent / "runs"
SPLITS = ("train", "dev", "test_normal", "test_challenge")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="awbench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run a split against the agent")
    run.add_argument("--split", choices=SPLITS, default="dev")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--tasks", default=None, metavar="IDS")
    run.add_argument("--wall-clock-cap", type=float, default=1800.0)
    run.add_argument("--run-id", default=None)

    report = sub.add_parser("report", help="Render a run report")
    report.add_argument("run_id")

    sub.add_parser("cleanup", help="Remove stray aw MCP rows after a crash")

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
        raise SystemExit(f"{name} is not set -- see awbench/cli.py docstring")
    return value


def _registrar() -> Registrar:
    base = os.environ.get("AW_OPS_BASE_URL") or (
        "https://ops.surogate.ai"
        if "surogate.ai" in os.environ.get("AW_BASE_URL", "")
        else "http://localhost:8888"
    )
    return Registrar(
        base_url=base,
        project_id=_require_env("AW_PROJECT_ID"),
        agent_id=_require_env("AW_AGENT_ID"),
        token=os.environ.get("AW_OPS_TOKEN"),
        username=os.environ.get("AW_OPS_USER"),
        password=os.environ.get("AW_OPS_PASSWORD"),
    )


def _mcp_port(mcp_url: str) -> int:
    from urllib.parse import urlparse

    return urlparse(mcp_url).port or 10000


def select_ids(task_ids: list[str], spec: str | None) -> list[str]:
    if not spec:
        return task_ids
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    unmatched = [w for w in wanted if w not in task_ids]
    if unmatched:
        raise SystemExit(f"no task in this split matches: {', '.join(unmatched)}")
    return wanted


async def _cmd_run(args: argparse.Namespace) -> int:
    from appworld import load_task_ids

    task_ids = list(load_task_ids(args.split))
    task_ids = select_ids(task_ids, args.tasks)
    if args.limit:
        task_ids = task_ids[: args.limit]

    is_pilot = bool(args.limit or args.tasks) or args.split == "dev"
    run_id = args.run_id or next_run_id(
        RUNS_DIR, "smoke" if is_pilot else args.split
    )
    out_dir = _run_dir(run_id)
    apis_url = os.environ.get("AW_APIS_URL", "http://localhost:9000")
    mcp_url = os.environ.get("AW_MCP_URL", "http://localhost:10000")
    harness_base = os.environ.get("AW_BASE_URL", "http://localhost:8000")
    print(f"run {run_id}: {len(task_ids)} task(s), split {args.split}, "
          f"apis {apis_url}, sequential")

    registrar = _registrar()
    exposure = expose_adapter(
        harness_base, _mcp_port(mcp_url),
        public_url=os.environ.get("AW_ADAPTER_PUBLIC_URL"),
    )
    outcomes: list[TaskOutcome] = []
    try:
        async with HarnessClient(
            base_url=harness_base,
            token=_require_env("SUROGATES_SA_TOKEN"),
            agent_id=_require_env("AW_AGENT_ID"),
        ) as client:
            for task_id in task_ids:
                result = await run_task(
                    client, registrar,
                    exposure.public_base or mcp_url,
                    task_id, apis_url,
                    experiment_name=run_id,
                    wall_clock_cap_s=args.wall_clock_cap,
                )
                write_trace(str(out_dir), result)
                failures = result.test_report.get("failures")
                outcomes.append(TaskOutcome(
                    task_id=task_id,
                    passed=result.passed,
                    terminal_status=result.terminal_status,
                    error=result.error,
                    evaluate_error=result.evaluate_error,
                    failures=len(failures) if isinstance(failures, list) else 0,
                    wall_clock_s=result.wall_clock_s,
                ))
                mark = {True: "PASS", False: "FAIL", None: "??"}[result.passed]
                print(f"  {task_id}: {mark} ({result.terminal_status}, "
                      f"{result.wall_clock_s:.0f}s)")
    finally:
        exposure.close()

    with open(out_dir / "outcomes.json", "w", encoding="utf-8") as fh:
        json.dump([dataclasses.asdict(o) for o in outcomes], fh, indent=2)
    passed = sum(1 for o in outcomes if o.passed)
    print(f"run {run_id}: {passed}/{len(outcomes)} passed")
    print(f"next: awbench report {run_id}")
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


def _cmd_cleanup() -> int:
    removed = _registrar().cleanup_all()
    print(f"removed {removed} stray row(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "cleanup":
        return _cmd_cleanup()
    return _cmd_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
