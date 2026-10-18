"""Steps of concurrent sessions, the ramp from step to step and the bisection to the knee."""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from oload import ramp as ramp_module
from oload.bar import Verdict
from oload.clients import ClientDown, Fleet, FleetFull, Timed
from oload.docserver import CallbackState
from oload.events import Event, EventLog
from oload.ramp import StepResult, bisect, ramp, run_step, steps
from oload.settings import Settings

SETTINGS = Settings(
    ds_url="http://office-docs.example.svc", command_url="http://127.0.0.1:9", jwt_secret="s" * 32,
    client_token="a-test-client-token", listen="127.0.0.1:9", public_base="http://127.0.0.1:9",
    namespace="office-load",
)
CLOSE_S = 0.05  # how long a fake session takes to close its pages


def test_steps_double_from_5() -> None:
    assert list(itertools.islice(steps(), 8)) == [5, 10, 20, 40, 80, 160, 320, 640]


def test_bisect_stops_within_a_tenth_of_the_holding_step_or_2_sessions() -> None:
    assert bisect(40, 80) == 60
    assert bisect(40, 44) is None
    assert bisect(45, 50) == 47  # 5 apart is more than a tenth of 45
    assert bisect(47, 50) is None
    assert bisect(5, 10) == 7 and bisect(8, 10) is None  # 2 sessions apart, below 20


# The ramp, with a fake step.

def result(n: int, *, holds: bool = True, client_limited: bool = False, invalid: bool = False) -> StepResult:
    verdict = Verdict(holds=holds, broken=[] if holds else ["open: 1 failed"], client_limited=client_limited,
                      stats={}, invalid=invalid, reason="the tool's own request was refused" if invalid else None)
    return StepResult(n=n, workload="rollout", verdict=verdict, started=0.0, hold_from=0.0, ended=0.0)


def fake_step(judged: Callable[[int], StepResult]) -> tuple[Callable[[int], Awaitable[StepResult]], list[int]]:
    tried: list[int] = []

    async def step(n: int) -> StepResult:
        tried.append(n)
        return judged(n)

    return step, tried


async def test_the_ramp_climbs_to_the_first_failing_step_then_bisects_to_the_knee() -> None:
    step, tried = fake_step(lambda n: result(n, holds=n < 50))
    results = await ramp(step)
    assert tried == [5, 10, 20, 40, 80, 60, 50, 45, 47]
    assert [r.n for r in results if r.holds] == [5, 10, 20, 40, 45, 47]
    assert [r.n for r in results if not r.holds] == [80, 60, 50]
    assert not any(r.lower_bound or r.invalid for r in results)


async def test_a_client_limited_step_stops_the_ramp_with_a_lower_bound() -> None:
    step, tried = fake_step(lambda n: result(n, holds=n < 40, client_limited=n >= 40))
    results = await ramp(step)
    assert tried == [5, 10, 20, 40]
    assert results[-1].lower_bound and not results[-1].holds


async def test_a_client_limited_step_stops_the_bisection_too() -> None:
    step, tried = fake_step(lambda n: result(n, holds=n < 50, client_limited=n == 60))
    results = await ramp(step)
    assert tried == [5, 10, 20, 40, 80, 60]
    assert results[-1].lower_bound


async def test_an_invalid_step_stops_the_ramp_and_is_no_knee() -> None:
    step, tried = fake_step(lambda n: result(n, holds=n < 20, invalid=n == 20))
    results = await ramp(step)
    assert tried == [5, 10, 20]
    assert results[-1].invalid and not results[-1].holds and not results[-1].lower_bound


async def test_an_invalid_step_stops_the_bisection_too() -> None:
    step, tried = fake_step(lambda n: result(n, holds=n < 50, invalid=n == 60))
    results = await ramp(step)
    assert tried == [5, 10, 20, 40, 80, 60]
    assert results[-1].invalid


async def test_given_steps_that_all_hold_end_the_ramp() -> None:
    step, tried = fake_step(lambda n: result(n))
    assert [r.n for r in await ramp(step, sizes=[4, 8])] == [4, 8]
    assert tried == [4, 8]


async def test_a_first_step_that_fails_has_nothing_to_bisect_from() -> None:
    step, tried = fake_step(lambda n: result(n, holds=False))
    await ramp(step)
    assert tried == [5]


# A step, with fake sessions.

class FakeSession:
    """A session as a step drives it: it places its pages, does *behave*, stays to the deadline, then closes."""

    made: list[FakeSession] = []
    behave: Callable[[FakeSession], Awaitable[None]] | None = None

    def __init__(self, index: int, workload: str, fleet: Fleet, settings: Settings, state: CallbackState,
                 log: EventLog, rng: Any, *, run_id: str, pace: Any = None) -> None:
        self.index, self.workload, self.fleet, self.log, self.run_id = index, workload, fleet, log, run_id
        self.until = self.started_mono = self.started_wall = 0.0
        self.closed: float | None = None
        self.leaks = False
        FakeSession.made.append(self)

    def event(self, what: str, ok: bool = True, cause: str | None = None) -> None:
        self.log.add(Event(self.index, "docx", what, ok, 0.1, cause, time.time()))  # type: ignore[arg-type]

    async def run(self, until: float, *, turns: int | None = None) -> None:
        self.started_mono, self.started_wall, self.until = time.monotonic(), time.time(), until
        pages = 2 if self.workload == "chat" else 1
        client = self.fleet.place(pages)
        try:
            self.event("open")
            if FakeSession.behave is not None:
                await FakeSession.behave(self)
            await asyncio.sleep(max(0.0, until - time.monotonic()))
        finally:
            await asyncio.sleep(CLOSE_S)
            self.closed = time.monotonic()
            (self.fleet.leak if self.leaks else self.fleet.release)(client, pages)


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> type[FakeSession]:
    FakeSession.made, FakeSession.behave = [], None
    monkeypatch.setattr(ramp_module, "VirtualSession", FakeSession)
    return FakeSession


def fleet(clients: int = 2, pages: int = 4) -> Fleet:
    return Fleet([object() for _ in range(clients)], pages_per_client=pages)  # type: ignore[misc]


async def step(n: int, run_dir: Path, the_fleet: Fleet, *, workload: str = "rollout", ramp_s: float = 0.6,
               hold_s: float = 0.3) -> StepResult:
    return await run_step(n, workload, the_fleet, SETTINGS, CallbackState(SETTINGS),  # type: ignore[arg-type]
                          run_id="r1", run_dir=run_dir, ramp_s=ramp_s, hold_s=hold_s)


async def test_a_step_spreads_its_starts_and_holds_from_the_last(sessions: type[FakeSession],
                                                                   tmp_path: Path) -> None:
    async def lose_a_save_in_the_ramp_up(session: FakeSession) -> None:
        if session.index < 3:
            session.event("marker", ok=False, cause="lost")

    sessions.behave = lose_a_save_in_the_ramp_up
    the_fleet = fleet()
    started = time.monotonic()
    done = await step(4, tmp_path, the_fleet)
    returned = time.monotonic()

    made = sessions.made
    assert [s.index for s in made] == [0, 1, 2, 3] and {s.run_id for s in made} == {"r1"}
    first = made[0].started_mono
    assert first - started < 0.05
    for i, session in enumerate(made):
        assert session.started_mono - first == pytest.approx(0.2 * i, abs=0.05)  # 0.6 s spread over 4 starts
    assert {s.until for s in made} == {made[0].until}  # one deadline
    assert made[0].until - made[3].started_mono == pytest.approx(0.3, abs=0.05)  # held from the last start
    assert done.hold_from == pytest.approx(made[3].started_wall, abs=0.05)
    assert all(s.closed is not None and s.closed <= returned for s in made)  # each page closed before it returns
    assert returned - made[0].until >= CLOSE_S
    assert the_fleet.placed == (0, 0)

    assert done.n == 4 and done.workload == "rollout"
    assert done.holds  # the saves lost in the ramp-up are not judged
    assert done.dir == tmp_path / "step-4"
    assert [e.what for e in EventLog.read(done.dir / "events.jsonl")].count("open") == 4
    assert done.started <= done.hold_from <= done.ended
    assert StepResult.read(done.dir) == done


async def test_a_lost_save_in_the_hold_breaks_the_step(sessions: type[FakeSession], tmp_path: Path) -> None:
    async def lose_a_save(session: FakeSession) -> None:
        await asyncio.sleep(max(0.0, session.until - time.monotonic() - 0.1))
        session.event("marker", ok=False, cause="lost")

    sessions.behave = lose_a_save
    done = await step(2, tmp_path, fleet())
    assert not done.holds and not done.lower_bound and done.verdict.broken[0].startswith("lost saves")


async def test_a_step_dir_is_never_written_twice(sessions: type[FakeSession], tmp_path: Path) -> None:
    await step(1, tmp_path, fleet(), ramp_s=0, hold_s=0)
    with pytest.raises(FileExistsError):
        await step(1, tmp_path, fleet(), ramp_s=0, hold_s=0)


async def test_pages_still_placed_before_a_step_make_it_client_limited(sessions: type[FakeSession],
                                                                       tmp_path: Path) -> None:
    the_fleet = fleet()
    the_fleet.place(1)
    done = await step(2, tmp_path, the_fleet)
    assert sessions.made == []
    assert not done.holds and done.verdict.client_limited and done.lower_bound
    assert "1 page" in done.verdict.broken[0]
    assert StepResult.read(done.dir).lower_bound


async def test_a_step_the_fleet_has_no_room_for_is_a_lower_bound(sessions: type[FakeSession],
                                                                  tmp_path: Path) -> None:
    the_fleet = fleet(clients=2, pages=2)
    full = await step(3, tmp_path, the_fleet, workload="chat")  # 2 pages each, one client each
    assert sessions.made == [] and full.lower_bound and not full.holds

    the_fleet.leak(the_fleet.place(1))  # a page that may live on in its pod: its slot is gone for the run
    smaller = await step(2, tmp_path, the_fleet, workload="chat")
    assert sessions.made == [] and smaller.lower_bound and smaller.leaked == 1
    assert (await step(1, tmp_path, the_fleet, workload="chat")).holds


async def test_fleet_full_in_a_session_stops_the_step_at_a_lower_bound(sessions: type[FakeSession],
                                                                        tmp_path: Path) -> None:
    async def full(session: FakeSession) -> None:
        if session.index == 2:
            raise FleetFull("no load client has room for 1 more page(s)")

    sessions.behave = full
    the_fleet = fleet()
    started = time.monotonic()
    done = await step(3, tmp_path, the_fleet, ramp_s=0.2, hold_s=30)
    assert time.monotonic() - started < 5  # the others stopped, not held to the deadline
    assert done.lower_bound and not done.holds and "no load client has room" in done.verdict.broken[0]
    assert all(s.closed is not None for s in sessions.made) and the_fleet.placed == (0, 0)


async def test_leaked_slots_are_reported_and_the_next_step_still_runs(sessions: type[FakeSession],
                                                                      tmp_path: Path) -> None:
    async def leak(session: FakeSession) -> None:
        session.leaks = session.index == 0

    sessions.behave = leak
    the_fleet = fleet()
    first = await step(2, tmp_path, the_fleet, ramp_s=0, hold_s=0.1)
    assert first.holds and first.leaked == 1 and the_fleet.placed == (0, 0)
    second = await step(3, tmp_path, the_fleet, ramp_s=0, hold_s=0.1)
    assert second.holds and second.leaked == 2


# A load client dying mid-step, with the real session.

class DyingOffice:
    """A load client that opens its pages, then is gone: every later call cannot reach it."""

    def __init__(self) -> None:
        self.opened = 0

    async def open(self, doc_key: str, session_id: str, config: dict[str, Any],
                   document_server_url: str) -> Timed[str]:
        await asyncio.sleep(0.01)
        self.opened += 1
        return Timed(f"{doc_key}.{session_id}", 0.01)

    async def _gone(self, *_: Any) -> Any:
        await asyncio.sleep(0.01)
        raise ClientDown("http://10.0.0.1:8080 could not be reached: ConnectionRefused", seconds=0.01)

    read = edit = flush = close = _gone


async def test_a_load_client_dying_mid_step_makes_it_client_limited(tmp_path: Path) -> None:
    office = DyingOffice()
    the_fleet = Fleet([office], pages_per_client=4)  # type: ignore[list-item]
    done = await step(2, tmp_path, the_fleet, ramp_s=0, hold_s=0.3)
    assert office.opened == 2
    assert not done.holds and done.verdict.client_limited and done.lower_bound and not done.invalid
    assert done.verdict.stats["failures"]["client"] > 0
    assert the_fleet.placed == (0,)
