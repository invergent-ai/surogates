"""A folder's history on the user's computer is pruned after a landing there, keeping every thread that works in it.

Each scene runs a worker's real wake on the tests' computer, whose folder's
history is the real one (``surogates.sandbox.local_history``).  A turn whose
landing recorded asks its computer, last, to prune that history, under an
invocation of its own: once its end and its report are written, and for a
while at most.  The computer prunes at most once a day by the server's clock,
and is told the branch and the base of every thread on that folder,
whichever project's.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import insert, text, update

from surogates.db.models import WorkstreamThread
from surogates.harness import local_landing
from surogates.workstreams.history import kept_refs_in
from surogates.workstreams.threads import make_thread
from tests.test_steer_loop import _final_response

from .test_devices import FOLDER, api, eventually, link_url, register  # noqa: F401  (api and link_url are fixtures)
from .test_local_history_open import asked, begun_with_copy, turn_end, woken
from .test_local_history_settle import SECOND, left_open
from .test_local_history_threads import computer  # noqa: F401  (a fixture)
from .test_local_history_waits import back, landing_away
from .test_local_threads import device_card
from .test_turn_sagas import calling

pytestmark = pytest.mark.asyncio(loop_scope="session")

WORK = calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"}))


def prunings(computer) -> list[dict]:
    """Each pruning *computer*'s app is asked, as it was asked and answered: its invocation, what it keeps, its time and its answer."""
    heard: list[dict] = []

    def lie(frame, outcome):
        if frame["args"].get("action") == "prune":
            args = frame["args"]
            heard.append({"under": frame["invocation_id"], "keep": args["keep"], "now": args["now"], "answer": outcome})
        return outcome

    computer.app.lie = lie
    return heard


def refs_of(*threads) -> list[str]:
    return sorted(f"refs/{kind}/{thread.id}" for thread in threads for kind in ("heads/threads", "bases"))


async def test_a_folders_history_is_pruned_after_a_landing_keeping_every_thread_that_works_in_it(api, computer, monkeypatch):
    _, master, thread = await begun_with_copy(api, computer)
    _, _, other = await begun_with_copy(api, computer, nonce=SECOND)
    pruned = prunings(computer)
    await woken(api, monkeypatch, thread, [WORK, _final_response("Done.")])
    # Asked once the landing is recorded and forgotten, under an invocation of its own, of the thread's alone.
    [first] = pruned
    assert asked(computer)[-2:] == [("land:0", "forget"), ("land:0:prune", "prune")]
    # Kept: the branch and the base of each thread that works in the folder, another project's too, and no other ref.
    assert sorted(first["keep"]) == refs_of(thread, other) and first["answer"]["ok"]["pruned"] is True
    # By the server's clock.
    assert abs(first["now"] - time.time()) < 60
    # Asked after the turn's end and its report: neither waits for a pruning, or for a computer that went away before one.
    async with api.app.state.session_factory() as db:
        assert (await db.execute(text(
            "SELECT (SELECT created_at FROM device_operations WHERE calling_session_id = :thread AND invocation_id = 'land:0:prune')"
            " > (SELECT max(created_at) FROM events WHERE (session_id = :thread AND type = 'session.complete')"
            " OR (session_id = :master AND type = 'worker.complete'))"
        ), {"thread": thread.id, "master": master.id})).scalar_one() is True
    # A landing later the same day asks again, under its own turn's name, and its computer does nothing.
    turn = await turn_end(api, thread)
    await woken(api, monkeypatch, thread, [calling(("terminal", {"command": "echo more >> Notes.md"})), _final_response("Done.")], said="More.")
    assert [(p["under"], p["answer"]["ok"]["pruned"]) for p in pruned] == [("land:0:prune", True), (f"land:{turn}:prune", False)]
    # A turn's end that recorded nothing asks for no pruning: its copy changed nothing, and it gives the folder back.
    await woken(api, monkeypatch, thread, [calling(("terminal", {"command": "true"})), _final_response("Done.")], said="Again.")
    assert len(pruned) == 2 and [action for _, action in asked(computer)[-2:]] == ["changed", "forget"]


async def test_a_pruning_its_computer_does_not_answer_holds_the_turns_end_no_longer_than_a_pruning_takes(api, computer, monkeypatch):
    # Eleven minutes, a request of the folder's history being cut off at ten there; here it is half a second.
    monkeypatch.setattr(local_landing, "PRUNE_WAIT", 0.5)
    _, _, thread = await begun_with_copy(api, computer)
    turn, _ = await landing_away(api, monkeypatch, computer, thread, "prune", replies=[WORK, _final_response("Done.")], invocation="land:0:prune")
    await asyncio.wait_for(turn, 10.0)
    # The landing stands, and nothing of the pruning is left open to run when the computer is back.
    assert (computer.folder / "Budget.xlsx").read_text() == "Total,42\n"
    assert await left_open(api, thread) == []
    await back(computer)
    await asyncio.sleep(0.5)
    assert [action for under, action in asked(computer) if under == "land:0:prune"] == ["prune"]


async def begun_on(api, device_id: str, folder: str = FOLDER):
    """A project's thread on *folder* of the computer *device_id*, with its row: a thread of the project's, as its start makes it."""
    project, master, proposal = await device_card(api)
    thread = await make_thread(
        session_store=api.app.state.session_store, session_factory=api.app.state.session_factory, master=master, live_config=None,
        title="Check the totals", device=SimpleNamespace(id=UUID(device_id), name="Flavius's ThinkPad"), folder=folder,
        card={"proposal_id": proposal, "key": "2"},
    )
    async with api.app.state.session_factory() as db:
        await db.execute(insert(WorkstreamThread).values(session_id=thread.id, workstream_id=UUID(project["id"]), title="Check the totals"))
        await db.commit()
    return thread


async def test_the_refs_a_pruning_keeps_are_each_threads_on_that_folder_of_that_computer_live_or_resolved_within_the_window(api):
    device = await register(api)
    live, lately, long_ago = [await begun_on(api, device["id"]) for _ in range(3)]
    # Another folder of the same computer, and the same folder of another computer.
    elsewhere = await begun_on(api, device["id"], f"{FOLDER}/Elsewhere")
    computer = await register(api)
    another = await begun_on(api, computer["id"])
    async with api.app.state.session_factory() as db:
        for thread, days in ((lately, 30), (long_ago, 120)):
            await db.execute(
                update(WorkstreamThread).where(WorkstreamThread.session_id == thread.id).values(resolved_at=datetime.now(UTC) - timedelta(days=days))
            )
        await db.commit()
    assert sorted(await kept_refs_in(api.app.state.session_factory, UUID(device["id"]), FOLDER)) == refs_of(live, lately)
    assert sorted(await kept_refs_in(api.app.state.session_factory, UUID(device["id"]), f"{FOLDER}/Elsewhere")) == refs_of(elsewhere)
    assert sorted(await kept_refs_in(api.app.state.session_factory, UUID(computer["id"]), FOLDER)) == refs_of(another)
