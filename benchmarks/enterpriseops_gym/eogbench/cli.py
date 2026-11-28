"""Command-line entry point for the EnterpriseOps-Gym experiment.

    eogbench run --domains csm --limit 1     # smoke: seed + tunnel + verify
    eogbench run                             # all tasks in the tasks dir
    eogbench report <run_id>

Environment:
    SUROGATES_SA_TOKEN   harness /v1/api/* auth (same as the siblings)
    EOG_BASE_URL         harness API base (default http://localhost:8000)
    EOG_AGENT_ID         agent under test (lean -- gym tools only)
    EOG_PROJECT_ID       ops project owning the MCP registrations
    EOG_OPS_BASE_URL     ops control plane (defaults like claweval's)
    EOG_OPS_USER / EOG_OPS_PASSWORD / EOG_OPS_TOKEN   ops login
    EOG_ADAPTER_PUBLIC_URL   optional: your own tunnel to the gym proxy
    EOG_TASKS_DIR        task JSONs (default: vendored data/revised)
    EOG_GYM_URL          override the gym server URL for every single-gym task
                         (a multi-gym task keeps each gym's own URL)
                         (default: each task's own, i.e. localhost ports)

Scoring is deterministic (upstream's SQL verifiers) -- no judge.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import pathlib

from eogbench import vendor
from eogbench.client import HarnessClient
from eogbench.dataset import Task, load_tasks
from eogbench.proxy import GymProxy
from eogbench.registrar import Registrar
from eogbench.report import TaskOutcome, render
from eogbench.runner import RolloutResult, kept_results, run_task, write_trace
from eogbench.tunnel import expose_adapter

RUNS_DIR = pathlib.Path(__file__).parent.parent / "runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eogbench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run tasks against the agent")
    run.add_argument("--domains", default=None,
                     help="Comma-separated domain filter (default: all)")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--tasks", default=None, metavar="IDS",
                     help="Comma-separated task ids (domain/stem)")
    run.add_argument("--wall-clock-cap", type=float, default=1800.0)
    run.add_argument("--run-id", default=None)
    run.add_argument("--scoring", choices=["upstream", "all-verifiers"], default="upstream",  # = runner.SCORING_RULES
                     help="upstream (default): upstream's verdict (one result per verifier name, "
                          "the last wins; a verifier query the gym rejects as a query error is a "
                          "fail; upstream also fails a verifier on a transport or database fault, "
                          "which stays an infrastructure error here); all-verifiers: every "
                          "verifier passed, a rejected query is an infrastructure error (this "
                          "adapter's earlier rule)")

    report = sub.add_parser("report", help="Render a run report")
    report.add_argument("run_id")

    sub.add_parser("cleanup", help="Remove stray eog MCP rows after a crash")
    collect = sub.add_parser("import-tasks", help="Import a pinned public dataset and inventory unsupported tasks")
    collect.add_argument("--revision", required=True)
    collect.add_argument("--mode", default="oracle", choices=["oracle", "plus_5_tools", "plus_10_tools", "plus_15_tools"])
    collect.add_argument("--output", type=pathlib.Path, required=True)

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
        raise SystemExit(f"{name} is not set -- see eogbench/cli.py docstring")
    return value


def _registrar() -> Registrar:
    base = os.environ.get("EOG_OPS_BASE_URL") or (
        "https://ops.surogate.ai"
        if "surogate.ai" in os.environ.get("EOG_BASE_URL", "")
        else "http://localhost:8888"
    )
    return Registrar(
        base_url=base,
        project_id=_require_env("EOG_PROJECT_ID"),
        agent_id=_require_env("EOG_AGENT_ID"),
        token=os.environ.get("EOG_OPS_TOKEN"),
        username=os.environ.get("EOG_OPS_USER"),
        password=os.environ.get("EOG_OPS_PASSWORD"),
    )


def select_tasks(tasks: list[Task], spec: str | None) -> list[Task]:
    if not spec:
        return tasks
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    by_id = {t.task_id: t for t in tasks}
    unmatched = [w for w in wanted if w not in by_id]
    if unmatched:
        raise SystemExit(f"no task matches: {', '.join(unmatched)}")
    return [by_id[w] for w in wanted]


def _outcome(meta: dict, domain: str) -> TaskOutcome:
    # The results the verdict read (upstream: the last of a repeated verifier name), so that passed agrees with
    # verifiers_passed == verifiers_total.
    results = kept_results(list(meta.get("verifier_results") or []), meta.get("scoring") or "upstream")
    return TaskOutcome(
        task_id=meta["task_id"],
        domain=domain,
        passed=meta.get("passed"),
        verifiers_total=len(results),
        verifiers_passed=sum(1 for v in results if v.get("passed")),
        terminal_status=str(meta.get("terminal_status") or ""),
        error=meta.get("error"),
        verify_error=meta.get("verify_error"),
        failed_verifiers=[str(v.get("name")) for v in results
                          if not v.get("passed")],
    )


async def _run_multi_gym_task(client, registrar, harness_base: str, task: Task,
                              args: argparse.Namespace) -> RolloutResult:
    """A multi-gym (hybrid) task: one proxy, and one address the harness can
    reach, per gym, for this task only. EOG_ADAPTER_PUBLIC_URL fronts one
    proxy, so such a task is recorded as not run (an infrastructure row)."""
    if os.environ.get("EOG_ADAPTER_PUBLIC_URL"):
        return RolloutResult(
            task_id=task.task_id, session_id="", terminal_status="env_error",
            scoring=args.scoring,
            error="multi-gym task: EOG_ADAPTER_PUBLIC_URL fronts one gym proxy, "
                  "and this task needs one per gym",
        )
    # One proxy per GYM, never per URL: a proxy holds one task scope (database, tools, context,
    # credentials), so two gyms sharing a URL still get one each.
    proxies: dict[str, GymProxy] = {}
    exposures = []
    try:
        try:
            gym_proxies = {}
            for gym in task.gyms:
                proxy = proxies[gym.name] = GymProxy(gym.url).start()
                exposure = expose_adapter(harness_base, proxy.port)
                exposures.append(exposure)
                gym_proxies[gym.name] = (exposure.public_base or proxy.local_url, proxy)
        except Exception as exc:  # noqa: BLE001 - setup failure: this task's infrastructure row, the run goes on
            return RolloutResult(
                task_id=task.task_id, session_id="", terminal_status="env_error",
                scoring=args.scoring, error=f"multi-gym setup: {type(exc).__name__}: {exc}",
            )
        return await run_task(
            client, registrar, "", None, task,
            wall_clock_cap_s=args.wall_clock_cap,
            gym_proxies=gym_proxies, scoring=args.scoring,
        )
    finally:
        for exposure in exposures:
            try:
                exposure.close()
            except Exception:  # noqa: BLE001 - every proxy below is still stopped
                pass
        for proxy in proxies.values():
            try:
                proxy.stop()
            except Exception:  # noqa: BLE001
                pass


async def _cmd_run(args: argparse.Namespace) -> int:
    commit = vendor.verify_pin()
    domains = tuple(d.strip() for d in args.domains.split(",")) \
        if args.domains else None
    tasks = load_tasks(domains, tuple(t.strip() for t in args.tasks.split(",")) if args.tasks else None)
    tasks = select_tasks(tasks, args.tasks)
    if args.limit:
        tasks = tasks[: args.limit]

    is_pilot = bool(args.limit or args.tasks or domains)
    run_id = args.run_id or next_run_id(
        RUNS_DIR, "smoke" if is_pilot else "full"
    )
    out_dir = _run_dir(run_id)
    imported = pathlib.Path(os.environ.get("EOG_TASKS_DIR", "")) / "import.json"
    run_config: dict = {"scoring": args.scoring}
    if imported.is_file():
        metadata = json.loads(imported.read_text())
        run_config.update({"mode": metadata["mode"], "dataset_revision": metadata["revision"]})
    (out_dir / "run-config.json").write_text(json.dumps(run_config) + "\n")
    print(f"run {run_id}: {len(tasks)} task(s), pin {commit[:12]}, sequential")

    gym_override = os.environ.get("EOG_GYM_URL")
    if gym_override:
        tasks = [dataclasses.replace(t, gym_url=gym_override) for t in tasks]

    # One proxy per distinct gym URL; one tunnel fronting the first proxy
    # -- tasks are sequential, so the proxy's upstream is re-pointed per
    # task via a fresh GymProxy when the gym URL changes.
    registrar = _registrar()
    outcomes: list[TaskOutcome] = []
    harness_base = os.environ.get("EOG_BASE_URL", "http://localhost:8000")

    async with HarnessClient(
        base_url=harness_base,
        token=_require_env("SUROGATES_SA_TOKEN"),
        agent_id=_require_env("EOG_AGENT_ID"),
    ) as client:
        current_url = None
        proxy = None
        exposure = None
        try:
            for task in tasks:
                if task.gyms:
                    result = await _run_multi_gym_task(client, registrar, harness_base, task, args)
                    write_trace(str(out_dir), result)
                    outcomes.append(_outcome({
                        "task_id": result.task_id,
                        "passed": result.passed,
                        "verifier_results": result.verifier_results,
                        "scoring": result.scoring,
                        "terminal_status": result.terminal_status,
                        "error": result.error,
                        "verify_error": result.verify_error,
                    }, task.domain))
                    mark = {True: "PASS", False: "FAIL", None: "??"}[result.passed]
                    print(f"  {result.task_id}: {mark} ({result.terminal_status}, "
                          f"{result.wall_clock_s:.0f}s, {len(task.gyms)} gyms)")
                    continue
                if task.gym_url != current_url:
                    if exposure is not None:
                        exposure.close()
                    if proxy is not None:
                        proxy.stop()
                    proxy = GymProxy(task.gym_url).start()
                    exposure = expose_adapter(
                        harness_base, proxy.port,
                        public_url=os.environ.get("EOG_ADAPTER_PUBLIC_URL"),
                    )
                    current_url = task.gym_url
                registered_base = exposure.public_base or proxy.local_url
                result = await run_task(
                    client, registrar, registered_base, proxy, task,
                    wall_clock_cap_s=args.wall_clock_cap,
                    scoring=args.scoring,
                )
                write_trace(str(out_dir), result)
                outcomes.append(_outcome({
                    "task_id": result.task_id,
                    "passed": result.passed,
                    "verifier_results": result.verifier_results,
                    "scoring": result.scoring,
                    "terminal_status": result.terminal_status,
                    "error": result.error,
                    "verify_error": result.verify_error,
                }, task.domain))
                mark = {True: "PASS", False: "FAIL", None: "??"}[result.passed]
                print(f"  {result.task_id}: {mark} "
                      f"({result.terminal_status}, {result.wall_clock_s:.0f}s)")
        finally:
            if exposure is not None:
                exposure.close()
            if proxy is not None:
                proxy.stop()

    with open(out_dir / "outcomes.json", "w", encoding="utf-8") as fh:
        json.dump([dataclasses.asdict(o) for o in outcomes], fh, indent=2)
    passed = sum(1 for o in outcomes if o.passed)
    print(f"run {run_id}: {passed}/{len(outcomes)} passed")
    print(f"next: eogbench report {run_id}")
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
    if args.command == "import-tasks":
        from eogbench.import_tasks import download
        result = download(args.revision, args.mode, args.output)
        print(f"Imported {len(result['accepted'])}/{result['rows']} tasks; {len(result['rejected'])} unsupported; inventory: {args.output / 'import.json'}")
        return 0
    return _cmd_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
