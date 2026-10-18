"""Command-line entry point for the office-document benchmark.

    obench list                                # the tasks and their checks
    obench run --pilot                         # the six pilot tasks, one run each
    obench run --runs 3                        # every task, three runs each
    obench run --kind xlsx --mode edit         # a filtered subset
    obench check <run_id>                      # re-score stored documents (after changing a check)
    obench invalidate <run_id> TASK[:rK] --reason "..."   # set an infrastructure-broken rollout aside
    obench report <run_id> [<run_id> ...]      # per-task table + failed checks, over one run or several

Sessions run one at a time, always: a harness worker has failed under concurrent sessions (stale-lease recovery), and
a failed rollout from load looks exactly like a model failure.

Environment:
    SUROGATES_SA_TOKEN         the per-operator Work-chat service account's token (ops-chat-*): only it may create a
                               channel=studio session, the channel that offers office_read/office_edit
    OFFICEBENCH_BASE_URL       harness API base (default http://localhost:8000)
    OFFICEBENCH_AGENT_ID       agent under test
    OFFICEBENCH_OPS_USER_ID    the operator's ops user id, stamped in config.ops as ops stamps a Work chat
    OFFICEBENCH_OPS_USERNAME   the operator's ops username, likewise
    OFFICEBENCH_RENDER_SHELL   a command prefix opening a shell where the document server's converter, x2t, runs
                               (e.g. "kubectl exec -i <converter pod> -c <container> --"): it renders a deck to PDF and
                               text overflow is measured on that. Unset, overflow is unknown; a kept render/ is reused
    OFFICEBENCH_X2T            x2t's path in that shell
                               (default /var/www/euro-office/documentserver/server/FileConverter/bin/x2t)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

from obench.client import HarnessClient
from obench.evaluate import evaluate, trajectory
from obench.report import render
from obench.runner import FatalRunError, run_task, write_rollout
from obench.tasks import TASKS, Task, by_id

RUNS_DIR = pathlib.Path(__file__).parent.parent / "runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obench")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List the tasks and their checks")

    run = sub.add_parser("run", help="Run tasks against the agent, one session at a time")
    run.add_argument("--tasks", default=None, metavar="IDS", help="Comma-separated task ids")
    run.add_argument("--kind", default=None, help="Comma-separated kinds: docx,xlsx,pptx,pdf")
    run.add_argument("--mode", choices=["create", "edit"], default=None)
    run.add_argument("--pilot", action="store_true", help="Only the pilot tasks")
    run.add_argument("--limit", type=int, default=None, help="Only the first N selected tasks")
    run.add_argument("--runs", type=int, default=1, help="Runs per task (default 1)")
    run.add_argument("--wall-clock-cap", type=float, default=1800.0, help="Seconds per session")
    run.add_argument("--save-wait", type=float, default=60.0, help="Seconds to wait for the turn's saves to land")
    run.add_argument("--keep-sessions", action="store_true",
                     help="Keep each session after collecting it (default: delete it, freeing its editor pages)")
    run.add_argument("--run-id", default=None)

    chk = sub.add_parser("check", help="Re-score a run's stored documents")
    chk.add_argument("run_id")

    inv = sub.add_parser("invalidate", help="Set rollouts aside as infrastructure-invalid, so a re-run redoes them")
    inv.add_argument("run_id")
    inv.add_argument("rollouts", nargs="+", metavar="TASK[:rK]", help="task id, optionally :r<k> (default r1)")
    inv.add_argument("--reason", required=True)

    rep = sub.add_parser("report", help="Render a report over one run, or several taken together")
    rep.add_argument("run_ids", nargs="+", metavar="run_id")
    return parser


def next_run_id(runs_dir: pathlib.Path, prefix: str) -> str:
    """Next ``<prefix>-NNN``, from the highest existing number (strays cannot shift it)."""
    highest = 0
    for path in runs_dir.glob(f"{prefix}-*"):
        suffix = path.name.removeprefix(f"{prefix}-")
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"{prefix}-{highest + 1:03d}"


def select_tasks(tasks: list[Task], ids: str | None = None, kind: str | None = None, mode: str | None = None,
                 pilot: bool = False, limit: int | None = None) -> list[Task]:
    """Filter the suite. An unknown id or kind is an error, never a silent skip."""
    out = list(tasks)
    if ids:
        wanted = [s.strip() for s in ids.split(",") if s.strip()]
        known = {t.id: t for t in tasks}
        unknown = [w for w in wanted if w not in known]
        if unknown:
            raise SystemExit(f"no task matches: {', '.join(unknown)}")
        out = [known[w] for w in wanted]
    if kind:
        kinds = {k.strip() for k in kind.split(",") if k.strip()}
        bad = kinds - {"docx", "xlsx", "pptx", "pdf"}
        if bad:
            raise SystemExit(f"unknown kind: {', '.join(sorted(bad))}")
        out = [t for t in out if t.kind in kinds]
    if mode:
        out = [t for t in out if t.mode == mode]
    if pilot:
        out = [t for t in out if t.pilot]
    if limit:
        out = out[:limit]
    if not out:
        raise SystemExit("no task selected")
    return out


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set -- see obench/cli.py docstring")
    return value


def rollout_dirs(run_dir: pathlib.Path) -> list[tuple[Task, pathlib.Path]]:
    tasks = by_id()
    out = []
    for meta in sorted(run_dir.glob("*/r*/meta.json")):
        task = tasks.get(meta.parent.parent.name)
        if task is not None:
            out.append((task, meta.parent))
    return out


def score(task: Task, rollout_dir: pathlib.Path) -> dict:
    result = evaluate(task, str(rollout_dir))
    (rollout_dir / "trajectory.md").write_text(trajectory(task, str(rollout_dir), result), encoding="utf-8")
    return result


def one_line(result: dict) -> str:
    m = result["metrics"]
    flags = []
    if result.get("channel") != "studio":
        flags.append(f"channel={result.get('channel')}")
    if m["refusals"]:
        flags.append(f"{len(m['refusals'])} refused")
    if m["office_errors"]:
        flags.append(f"{len(m['office_errors'])} office errors")
    if result["claims"]["contradicted"]:
        flags.append("claim contradicted")
    if result.get("asks"):
        flags.append(f"asked the user {len(result['asks'])}x")
    return (f"{result['task_id']} r{result['index']}: {result['passed']}/{result['passed'] + result['failed']} checks, "
            f"{result['terminal_status']}, {result['wall_clock_s']}s, {m['model_calls']} model calls, "
            f"{m['tool_calls_total']} tool calls" + (f" [{'; '.join(flags)}]" if flags else ""))


async def _cmd_run(args: argparse.Namespace) -> int:
    tasks = select_tasks(TASKS, args.tasks, args.kind, args.mode, args.pilot, args.limit)
    filtered = bool(args.tasks or args.kind or args.mode or args.limit)
    prefix = "pilot" if args.pilot and not filtered else ("smoke" if filtered else "full")
    run_id = args.run_id or next_run_id(RUNS_DIR, prefix)
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": run_id,
        "tasks": [t.id for t in tasks],
        "runs": args.runs,
        "base_url": os.environ.get("OFFICEBENCH_BASE_URL", "http://localhost:8000"),
        "agent_id": _require_env("OFFICEBENCH_AGENT_ID"),
    }, indent=2), encoding="utf-8")
    print(f"run {run_id}: {len(tasks)} task(s) x {args.runs} run(s), one session at a time", flush=True)

    async with HarnessClient(
        base_url=os.environ.get("OFFICEBENCH_BASE_URL", "http://localhost:8000"),
        token=_require_env("SUROGATES_SA_TOKEN"),
        agent_id=_require_env("OFFICEBENCH_AGENT_ID"),
    ) as client:
        for index in range(1, args.runs + 1):
            for task in tasks:
                rollout_dir = run_dir / task.id / f"r{index}"
                if (rollout_dir / "checks.json").exists():
                    print(f"{task.id} r{index}: already done, skipped", flush=True)
                    continue
                rollout_dir.mkdir(parents=True, exist_ok=True)
                try:
                    rollout = await run_task(client, task, str(rollout_dir), index=index,
                                             wall_clock_cap_s=args.wall_clock_cap, save_cap_s=args.save_wait,
                                             release=not args.keep_sessions)
                except FatalRunError as exc:
                    print(f"stopping the run: {exc}", file=sys.stderr)
                    return 2
                write_rollout(str(rollout_dir), task, rollout)
                print(one_line(score(task, rollout_dir)), flush=True)
    print(f"next: obench report {run_id}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    run_dir = RUNS_DIR / args.run_id
    pairs = rollout_dirs(run_dir)
    if not pairs:
        raise SystemExit(f"no rollouts in {run_dir}")
    for task, d in pairs:
        print(one_line(score(task, d)))
    return 0


def invalidate(run_dir: pathlib.Path, spec: str, reason: str) -> pathlib.Path:
    """Move a rollout to <task>/invalid-r<k>-<n>/ with INFRA-INVALID.txt: kept as evidence, left out of every report
    and check, and redone by the next ``run`` of that run id (which only skips rollouts with checks.json)."""
    import datetime as dt

    task_id, _, index = spec.partition(":")
    index = index or "r1"
    src = run_dir / task_id / index
    if not src.is_dir():
        raise SystemExit(f"no rollout {src}")
    n = 1
    while (run_dir / task_id / f"invalid-{index}-{n}").exists():
        n += 1
    dst = run_dir / task_id / f"invalid-{index}-{n}"
    src.rename(dst)
    (dst / "INFRA-INVALID.txt").write_text(
        f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}\n{reason}\n", encoding="utf-8")
    return dst


def invalid_rollouts(run_dir: pathlib.Path) -> list[tuple[str, str]]:
    out = []
    for marker in sorted(run_dir.glob("*/invalid-*/INFRA-INVALID.txt")):
        out.append((f"{marker.parent.parent.name}/{marker.parent.name}", marker.read_text().strip().replace("\n", " ")))
    return out


def _cmd_invalidate(args: argparse.Namespace) -> int:
    run_dir = RUNS_DIR / args.run_id
    for spec in args.rollouts:
        print(f"set aside: {invalidate(run_dir, spec, args.reason)}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    results = []
    for run_id in args.run_ids:
        run_dir = RUNS_DIR / run_id
        found = rollout_dirs(run_dir)
        if not found:
            raise SystemExit(f"no rollouts in {run_dir}")
        for task, d in found:
            path = d / "checks.json"
            results.append(json.loads(path.read_text()) if path.exists() else score(task, d))
    label = " + ".join(args.run_ids)
    invalid = [(f"{run_id}/{name}", why) for run_id in args.run_ids for name, why in invalid_rollouts(RUNS_DIR / run_id)]
    text = render(results, run_id=label, invalid=invalid)
    out = RUNS_DIR / args.run_ids[-1] / ("report.md" if len(args.run_ids) == 1 else f"report-{'+'.join(args.run_ids)}.md")
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nwritten to {out}")
    return 0


def _cmd_list() -> int:
    for t in TASKS:
        print(f"{t.id} ({t.kind}, {t.mode}{', pilot' if t.pilot else ''}) -> {t.target}")
        print(f"  {t.prompt}")
        for c in t.checks:
            print(f"   - {c.name}")
    print(f"\n{len(TASKS)} tasks: {sum(t.mode == 'create' for t in TASKS)} create, {sum(t.mode == 'edit' for t in TASKS)} edit")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "list":
        return _cmd_list()
    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "invalidate":
        return _cmd_invalidate(args)
    return _cmd_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
