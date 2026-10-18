"""The oload command: the two checks before a ramp, the ramp to the knee, and a run's report.

Each run goes to ``runs/<run-id>/``: ``run.json`` (what ran, with no secret), a dir per step (``step-<n>``) or per
editors count (``editors-<n>``) with its events, its result and the office namespace's metrics, and ``report.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from oload.clients import OPEN_TIMEOUT_S, Fleet, NoClients, RuntimeOffice, Timed
from oload.docserver import CallbackState, make_app
from oload.events import Event, EventLog
from oload.metrics import IDENTIFIER, Postgres, Sampler, settle
from oload.pace import WORKLOADS, Pace, Workload
from oload.ramp import HOLD_S, RAMP_S, StepResult, StepRunner, ramp, run_step, step_dir
from oload.report import render
from oload.session import VirtualSession
from oload.settings import MissingSettings, Settings, load_env_file

ROOT = Path(__file__).resolve().parents[1]  # benchmarks/office_load, installed editable
RUNS = ROOT / "runs"
ENV_FILE = ROOT / ".env"
SECRETS = ("jwt_secret", "client_token")  # never written into a run
# A run id goes into every document key, which the document server takes as [0-9a-zA-Z._=-], at most 128 long.
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}")
GATE_TIMEOUT_S = OPEN_TIMEOUT_S + 30.0  # every editor's open has answered or timed out by then
EDITORS_DEADLINE_S = 3600.0  # the editors check's one turn starts well before it
PG = Postgres()


class Refused(Exception):
    """The command cannot run as asked."""


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.run(args)
    except (MissingSettings, NoClients, Refused) as exc:
        print(f"oload: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("oload: stopped; `oload report <run-id>` reports the steps that finished", file=sys.stderr)
        return 130


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="oload",
        description="How many concurrent editing sessions one office document server holds while every save meets "
                    "the bar. Settings come from the OLOAD_* names in the environment or in .env; kubectl reads "
                    "KUBECONFIG. Never point it at a document server people use.",
    )
    commands = top.add_subparsers(dest="command", required=True, metavar="<command>")

    measured = argparse.ArgumentParser(add_help=False)
    database = measured.add_argument_group("the document server's database, for its connections")
    database.add_argument("--pg-namespace", default=PG.namespace,
                          help="its CNPG cluster's namespace (default: %(default)s)")
    database.add_argument("--pg-cluster", default=PG.cluster, help="the CNPG cluster (default: %(default)s)")
    database.add_argument("--pg-database", default=PG.database, type=_identifier,
                          help="the database (default: %(default)s)")
    timed = argparse.ArgumentParser(add_help=False)
    timed.add_argument("--ramp-s", type=_seconds, default=RAMP_S,
                       help="seconds a step's sessions start over (default: %(default)g)")
    timed.add_argument("--hold-s", type=_seconds, default=HOLD_S,
                       help="seconds a step holds from its last start (default: %(default)g)")

    clients = commands.add_parser(
        "preflight-clients", parents=[measured, timed], help="how many pages one load client holds",
        description="One load client (the first pod) at the rollout pace: each page count in turn, every page on "
                    "that client, until one breaks the bar.")
    clients.add_argument("--pages", type=_counts, required=True, help="page counts, e.g. 1,2,4,8,16,32")
    clients.add_argument("--profile", default="", help="the release's resource profile, as recorded")
    clients.add_argument("--run-id", type=_run_id, help="default: clients-<UTC time>")
    clients.set_defaults(run=_preflight_clients)

    cap = commands.add_parser(
        "preflight-cap", parents=[measured], help="whether that many editors at once can each edit and save",
        description="That many editors open at once, spread over the fleet; once all have opened, each makes one "
                    "edit and one save. Reports how many flushed true and saved their marker.")
    cap.add_argument("--editors", type=_counts, required=True, help="editor counts, e.g. 25,50")
    cap.add_argument("--profile", default="", help="the release's resource profile, as recorded")
    cap.add_argument("--run-id", type=_run_id, help="default: editors-<UTC time>")
    cap.set_defaults(run=_preflight_cap)

    ramping = commands.add_parser(
        "ramp", parents=[measured, timed], help="steps of concurrent sessions to the knee",
        description="Steps of 5, 10, 20, 40… concurrent sessions until one breaks the bar, then bisected to the "
                    "knee. --steps, --ramp-s and --hold-s are for smoke runs; the defaults are the measurement's.")
    ramping.add_argument("--workload", choices=WORKLOADS, required=True)
    ramping.add_argument("--profile", required=True, help="the release's resource profile, as recorded")
    ramping.add_argument("--run-id", type=_run_id, required=True)
    ramping.add_argument("--steps", type=_counts, help="these steps instead, e.g. 4,8")
    ramping.add_argument("--pages-per-client", type=_count,
                         help="pages placed on each load client (default: what each pod holds)")
    ramping.set_defaults(run=_ramp)

    reporting = commands.add_parser("report", help="(re)write a run's report.md and print it")
    reporting.add_argument("run_id", type=_run_id)
    reporting.set_defaults(run=_report)
    return top


# The commands.

def _ramp(args: argparse.Namespace) -> int:
    settings = _settings()
    return asyncio.run(_ramp_run(args, settings))


async def _ramp_run(args: argparse.Namespace, settings: Settings) -> int:
    pg = _postgres(args)
    fleet = await _discover(settings, args.pages_per_client)
    run_dir = _new_run(args.run_id)
    _record(run_dir, run_record(settings, "ramp", run_id=args.run_id, workload=args.workload, profile=args.profile,
                                fleet=fleet, extra={"steps": args.steps, "ramp_s": args.ramp_s,
                                                    "hold_s": args.hold_s, "postgres": asdict(pg)}))
    async with _serving(settings) as state:
        step = _sampled_step(args.workload, fleet, settings, state, run_id=args.run_id, run_dir=run_dir,
                             ramp_s=args.ramp_s, hold_s=args.hold_s, postgres=pg)
        await ramp(step, sizes=args.steps)
    _finish(run_dir)
    return 0


def _preflight_clients(args: argparse.Namespace) -> int:
    settings = _settings()
    return asyncio.run(_preflight_clients_run(args, settings))


async def _preflight_clients_run(args: argparse.Namespace, settings: Settings) -> int:
    pg = _postgres(args)
    fleet = await _discover(settings, None)
    if max(args.pages) > fleet.pages_per_client:
        raise Refused(f"the load clients hold {fleet.pages_per_client} pages each, fewer than {max(args.pages)}: "
                      f"raise their pagesPerClient")
    one = Fleet(fleet.clients[:1], fleet.pages_per_client)
    run_id = args.run_id or _default_id("clients")
    run_dir = _new_run(run_id)
    _record(run_dir, run_record(settings, "preflight-clients", run_id=run_id, workload="rollout",
                                profile=args.profile, fleet=one,
                                extra={"pages": args.pages, "ramp_s": args.ramp_s, "hold_s": args.hold_s,
                                       "postgres": asdict(pg)}))
    async with _serving(settings) as state:
        step = _sampled_step("rollout", one, settings, state, run_id=run_id, run_dir=run_dir, ramp_s=args.ramp_s,
                             hold_s=args.hold_s, postgres=pg)
        for pages in args.pages:
            if not (await step(pages)).holds:
                break
    _finish(run_dir)
    return 0


def _preflight_cap(args: argparse.Namespace) -> int:
    settings = _settings()
    return asyncio.run(_preflight_cap_run(args, settings))


async def _preflight_cap_run(args: argparse.Namespace, settings: Settings) -> int:
    pg = _postgres(args)
    fleet = await _discover(settings, None)
    if max(args.editors) > fleet.capacity:
        raise Refused(f"the load clients hold {fleet.capacity} pages in all, fewer than {max(args.editors)} "
                      f"editors: scale them")
    gate = OpenGate()
    gated = Fleet([Gated(client, gate) for client in fleet.clients], fleet.pages_per_client)  # type: ignore[misc]
    run_id = args.run_id or _default_id("editors")
    run_dir = _new_run(run_id)
    _record(run_dir, run_record(settings, "preflight-cap", run_id=run_id, workload="rollout", profile=args.profile,
                                fleet=fleet, extra={"editors": args.editors, "postgres": asdict(pg)}))
    async with _serving(settings) as state:
        for n in args.editors:
            outcome = await editors_check(n, gated, gate, settings, state, run_id=run_id, run_dir=run_dir,
                                          postgres=pg)
            print(f"{n} editors: {outcome['opened']} opened, {outcome['flushed']} flushed true, "
                  f"{outcome['saved']} saved their marker", flush=True)
    _finish(run_dir)
    return 0


def _report(args: argparse.Namespace) -> int:
    run_dir = RUNS / args.run_id
    if not (run_dir / "run.json").is_file():
        raise Refused(f"no run {args.run_id!r} in {RUNS}")
    print(render(run_dir), end="")
    return 0


# Steps, sampled.

def _sampled_step(workload: Workload, fleet: Fleet, settings: Settings, state: CallbackState, *, run_id: str,
                  run_dir: Path, ramp_s: float, hold_s: float,
                  postgres: Postgres) -> StepRunner:
    """The step the ramp runs: :func:`run_step`, with the office namespace's metrics sampled through it."""

    async def step(n: int) -> StepResult:
        settled, waited = await settle(settings.namespace)
        print(f"step {n}: the conversion queues {'settled' if settled else 'did NOT settle'} after {waited:.0f} s",
              flush=True)
        print(f"step {n}: {n} {workload} session(s)", flush=True)
        async with Sampler(settings.namespace, out_dir=step_dir(run_dir, n), postgres=postgres):
            result = await run_step(n, workload, fleet, settings, state, run_id=run_id, run_dir=run_dir,
                                    ramp_s=ramp_s, hold_s=hold_s)
        if result.invalid:
            said = f"invalid: {result.verdict.reason}"
        elif result.holds:
            said = "holds"
        else:
            said = ("client-limited: " if result.lower_bound else "breaks: ") + result.verdict.broken[0]
        print(f"step {n}: {said}", flush=True)
        return result

    return step


# The editors check.

class OneEdit(Pace):
    """One edit a turn: the editors check's pace."""

    def __init__(self) -> None:
        super().__init__("rollout")

    def edits_in_turn(self) -> int:
        return 1


class OpenGate:
    """Holds the editors' edits until every editor expected has opened, or failed to; at most GATE_TIMEOUT_S."""

    def __init__(self) -> None:
        self._left = 0
        self._opened = asyncio.Event()

    def expect(self, n: int) -> None:
        self._left, self._opened = n, asyncio.Event()

    def settled(self) -> None:
        self._left -= 1
        if self._left <= 0:
            self._opened.set()

    async def wait(self) -> None:
        try:
            async with asyncio.timeout(GATE_TIMEOUT_S):
                await self._opened.wait()
        except TimeoutError:
            pass


class Gated:
    """A load client whose reads and edits wait at *gate*: no editor edits before all are open."""

    def __init__(self, client: RuntimeOffice, gate: OpenGate) -> None:
        self.client = client
        self.gate = gate

    def __repr__(self) -> str:
        return f"Gated({self.client!r})"

    async def open(self, *args: Any, **kwargs: Any) -> Timed[str]:
        try:
            return await self.client.open(*args, **kwargs)
        finally:
            self.gate.settled()

    async def read(self, page: str, request: dict[str, Any]) -> Timed[dict[str, Any]]:
        await self.gate.wait()
        return await self.client.read(page, request)

    async def edit(self, page: str, ops: list[Any]) -> Timed[dict[str, Any]]:
        await self.gate.wait()
        return await self.client.edit(page, ops)

    async def flush(self, page: str) -> Timed[bool]:
        return await self.client.flush(page)

    async def close(self, page: str) -> None:
        await self.client.close(page)


async def editors_check(n: int, fleet: Fleet, gate: OpenGate, settings: Settings, state: CallbackState, *,
                        run_id: str, run_dir: Path, postgres: Postgres) -> dict[str, Any]:
    """*n* editors at once on *fleet* (its clients gated by *gate*), one edit and one save each: the outcome,
    also written to ``editors-<n>/cap.json``."""
    out = Path(run_dir) / f"editors-{n}"
    out.mkdir()
    room = fleet.capacity - sum(fleet.placed)
    if n > room:
        outcome = {"editors": n, "opened": 0, "flushed": 0, "saved": 0, "holds": False,
                   "failures": [f"the load clients have room for {room} editors, not {n}"]}
    else:
        log = EventLog(out / "events.jsonl")
        rng = random.Random()
        until = time.monotonic() + EDITORS_DEADLINE_S
        gate.expect(n)
        async with Sampler(settings.namespace, out_dir=out, postgres=postgres), asyncio.TaskGroup() as group:
            for i in range(n):
                editor = VirtualSession(i, "rollout", fleet, settings, state, log, random.Random(rng.getrandbits(64)),
                                        run_id=run_id, pace=OneEdit())
                group.create_task(editor.run(until, turns=1))
        outcome = editors_outcome(n, log.events)
    (out / "cap.json").write_text(json.dumps(outcome, indent=1) + "\n")
    return outcome


def editors_outcome(n: int, events: Sequence[Event]) -> dict[str, Any]:
    """How many of *n* editors opened, flushed true and saved their marker, and what failed."""

    def editors(what: str) -> int:
        return len({e.session for e in events if e.what == what and e.ok})

    failures = [f"session {e.session} ({e.kind}) {e.what} {e.cause}" + (f": {e.detail}" if e.detail else "")
                for e in events if not e.ok]
    saved = editors("marker")
    return {"editors": n, "opened": editors("open"), "flushed": editors("flush"), "saved": saved,
            "holds": saved == n and not failures, "failures": failures[:20]}


# The parts every run shares.

def run_record(settings: Settings, command: str, *, run_id: str, workload: str, profile: str, fleet: Fleet,
               extra: dict[str, Any]) -> dict[str, Any]:
    """What ``run.json`` says of a run: what ran, where and on what; never a secret."""
    return {
        "run_id": run_id, "command": command, "workload": workload, "profile": profile,
        "pages_per_client": fleet.pages_per_client, "clients": len(fleet.clients),
        "settings": {name: value for name, value in asdict(settings).items() if name not in SECRETS},
        "git_sha": _git_sha(), "started_at": time.time(), **extra,
    }


def _settings() -> Settings:
    load_env_file(ENV_FILE)
    return Settings.from_env()


async def _discover(settings: Settings, pages_per_client: int | None) -> Fleet:
    try:
        return await Fleet.discover(settings.namespace, settings.client_token, pages_per_client=pages_per_client)
    except ValueError as exc:
        raise Refused(str(exc)) from None


def _new_run(run_id: str) -> Path:
    run_dir = RUNS / run_id
    if run_dir.exists():
        raise Refused(f"{run_dir} holds a run already: a run id names one run")
    run_dir.mkdir(parents=True)
    return run_dir


def _record(run_dir: Path, record: dict[str, Any]) -> None:
    (run_dir / "run.json").write_text(json.dumps(record, indent=1) + "\n")


def _finish(run_dir: Path) -> None:
    render(run_dir)
    print(f"report: {run_dir / 'report.md'}")


@asynccontextmanager
async def _serving(settings: Settings) -> AsyncIterator[CallbackState]:
    """The tool's document-server side on OLOAD_LISTEN for the whole run: the files and the callbacks."""
    state = CallbackState(settings)
    host, port = _listen(settings.listen)
    runner = web.AppRunner(make_app(state), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
        yield state
    finally:
        await runner.cleanup()


def _listen(listen: str) -> tuple[str, int]:
    host, _, port = listen.rpartition(":")
    if not host or not port.isdigit():
        raise Refused(f"OLOAD_LISTEN is host:port, not {listen!r}")
    return host.strip("[]"), int(port)


def _postgres(args: argparse.Namespace) -> Postgres:
    return Postgres(args.pg_namespace, args.pg_cluster, args.pg_database)


def _git_sha() -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


def _default_id(kind: str) -> str:
    return f"{kind}-{datetime.now(UTC):%Y%m%dt%H%M%S}"


# Option types.

def _count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"at least 1, not {value}")
    return value


def _counts(text: str) -> list[int]:
    values = [_count(part.strip()) for part in text.split(",")]
    if len(set(values)) != len(values):
        raise argparse.ArgumentTypeError(f"each count once: {text!r}")
    return values


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"not negative: {value:g}")
    return value


def _run_id(text: str) -> str:
    if not RUN_ID.fullmatch(text):
        raise argparse.ArgumentTypeError(f"letters, digits, - and _, at most 48, not {text!r}")
    return text


def _identifier(text: str) -> str:
    if not IDENTIFIER.fullmatch(text):
        raise argparse.ArgumentTypeError(f"letters, digits and underscores, not {text!r}")
    return text
