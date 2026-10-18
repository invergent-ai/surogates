"""Steps of concurrent sessions judged against the bar: up to the first that breaks it, then bisected to the knee.

A step's files go to ``<run_dir>/step-<n>/``: ``events.jsonl``, every event as it came, and ``step.json``, the
step's result once judged (:meth:`StepResult.read` reads it back).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from oload.bar import Verdict, judge
from oload.clients import Fleet, FleetFull
from oload.docserver import CallbackState
from oload.events import EventLog
from oload.pace import Workload
from oload.session import VirtualSession
from oload.settings import Settings

FIRST_STEP = 5
RAMP_S = 60.0  # a step's sessions start spread over this
HOLD_S = 600.0  # then it holds this long from its last session's start: two autosave cycles


@dataclass(frozen=True, slots=True)
class StepResult:
    n: int
    workload: str
    verdict: Verdict
    started: float  # time.time() as the step began
    hold_from: float  # time.time() of its last session's start, from which the bar is applied
    ended: float  # time.time() once its every session closed its pages
    leaked: int = 0  # the run's slots leaked in the fleet so far, once the step ended
    dir: Path | None = None  # where its files are

    @property
    def holds(self) -> bool:
        return self.verdict.holds

    @property
    def invalid(self) -> bool:
        return self.verdict.invalid

    @property
    def lower_bound(self) -> bool:
        """The load clients ran out first: the capacity is at least the last holding step's."""
        return self.verdict.client_limited

    def write(self) -> None:
        assert self.dir is not None
        fields = {name: value for name, value in asdict(self).items() if name != "dir"}
        (self.dir / "step.json").write_text(json.dumps(fields, indent=1) + "\n")

    @classmethod
    def read(cls, step_dir: str | Path) -> StepResult:
        fields = json.loads((Path(step_dir) / "step.json").read_text())
        return cls(**{**fields, "verdict": Verdict(**fields["verdict"]), "dir": Path(step_dir)})


StepRunner = Callable[[int], Awaitable[StepResult]]


def steps() -> Iterator[int]:
    """5, 10, 20, 40, 80, 160, then doubling."""
    n = FIRST_STEP
    while True:
        yield n
        n *= 2


def bisect(good: int, bad: int) -> int | None:
    """The step between holding *good* and failing *bad*; None once they are within a tenth of *good*, or 2."""
    if bad - good <= max(2, good // 10):
        return None
    return (good + bad) // 2


async def ramp(step: StepRunner, *, sizes: Iterable[int] | None = None) -> list[StepResult]:
    """Run *step* at each of *sizes*, by default :func:`steps`, until one fails; then bisect to the knee.

    A step that fails on the load clients alone, or that they ran out of room for before anything else broke, ends
    the ramp: its result is a lower bound. A step the tool spoilt ends it too: it says nothing of the document server.
    """
    results: list[StepResult] = []
    good: int | None = None
    bad: int | None = None
    for n in steps() if sizes is None else sizes:
        result = await step(n)
        results.append(result)
        if result.holds:
            good = n
            continue
        if result.lower_bound or result.invalid:
            return results
        bad = n
        break
    if good is None or bad is None:
        return results
    while (n := bisect(good, bad)) is not None:
        result = await step(n)
        results.append(result)
        if result.lower_bound or result.invalid:
            break
        if result.holds:
            good = n
        else:
            bad = n
    return results


def step_dir(run_dir: str | Path, n: int) -> Path:
    return Path(run_dir) / f"step-{n}"


async def run_step(n: int, workload: Workload, fleet: Fleet, settings: Settings, state: CallbackState, *,
                   run_id: str, run_dir: str | Path, ramp_s: float = RAMP_S, hold_s: float = HOLD_S,
                   rng: random.Random | None = None) -> StepResult:
    """*n* sessions starting spread evenly over *ramp_s*, all to a deadline *hold_s* after the last start; judged.

    Every session has closed its pages and given back its slots when it returns. A fleet that still has pages placed
    from before, or no room for *n* sessions, runs nothing: the step is client-limited.
    """
    out = step_dir(run_dir, n)
    if out.exists():
        raise FileExistsError(f"{out} holds a step already: a run id names one run")
    out.mkdir(parents=True)
    rng = rng if rng is not None else random.Random()
    pages = 2 if workload == "chat" else 1
    started = time.time()

    def unrun(why: str) -> StepResult:
        result = StepResult(n, workload, Verdict(False, [f"fleet: {why}"], True, {}), started, started, time.time(),
                            sum(fleet.leaked), out)
        result.write()
        return result

    if any(fleet.placed):
        return unrun(f"{sum(fleet.placed)} page(s) still placed before the step: a session did not give its slot back")
    room = sum((fleet.pages_per_client - leaked) // pages for leaked in fleet.leaked)
    if n > room:
        return unrun(f"room for {room} {workload} session(s), not {n} ({sum(fleet.leaked)} slot(s) leaked)")

    log = EventLog(out / "events.jsonl")
    t0, wall0 = time.monotonic(), time.time()
    last = ramp_s if n > 1 else 0.0
    until = t0 + last + hold_s

    async def session(i: int) -> None:
        await asyncio.sleep(max(0.0, t0 + last * i / max(1, n - 1) - time.monotonic()))
        await VirtualSession(i, workload, fleet, settings, state, log, random.Random(rng.getrandbits(64)),
                             run_id=run_id).run(until)

    full: FleetFull | None = None
    try:
        async with asyncio.TaskGroup() as group:
            for i in range(n):
                group.create_task(session(i))
    except* FleetFull as group:  # the others were stopped, their pages closed
        full = group.exceptions[0]  # type: ignore[assignment]
    hold_from = wall0 + last
    verdict = judge(log.events, hold_from)
    if full is not None and not verdict.invalid:
        if verdict.holds or verdict.client_limited:  # the step never had its n sessions: a lower bound
            verdict = replace(verdict, holds=False, broken=[f"fleet: {full}", *verdict.broken], client_limited=True)
        else:  # the document server broke the bar before the fleet ran out: still a failing step
            verdict = replace(verdict, broken=[*verdict.broken, f"fleet: {full}"])
    result = StepResult(n, workload, verdict, started, hold_from, time.time(), sum(fleet.leaked), out)
    result.write()
    return result
