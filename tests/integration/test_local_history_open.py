"""A turn of a project's thread bound with a copy of its own opens the copy before its steps; one with nowhere to work runs none."""

from __future__ import annotations

import asyncio
import contextlib
import os
from uuid import UUID

import pytest
from sqlalchemy import text

import surogates.devices.operations as operations_module
from surogates.devices.binding import THREAD_KINDS
from surogates.devices.history import NO_COPY, NOWHERE, OPEN_TRIES, thread_copy
from surogates.devices.operations import OperationRequest
from surogates.devices.workspace import DeviceOperationError
from surogates.harness.landing import TURN_ENDS
from surogates.harness.tool_exec import _open_local_copy
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from tests.test_steer_loop import _final_response

from .test_device_sessions import is_bound
from .test_devices import FOLDER, NONCE, api, eventually, link_url  # noqa: F401  (api and link_url are fixtures)
from .test_local_history_threads import computer, made_with_copy, picture  # noqa: F401  (computer is a fixture)
from .test_local_threads import begin, begun_local, confirmed, journal, laptop  # noqa: F401  (laptop is a fixture)
from .test_turn_sagas import a_turn, calling

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def begun_with_copy(api, computer, *, nonce: str = NONCE):
    """A project, its master and a thread made on *computer*, bound with a copy of its own by its app, which is connected, and begun."""
    project, master, thread = await made_with_copy(api, computer.device_id)
    computer.app.prepare(nonce, FOLDER)
    await journal(api).bind(session_id=thread.id, device_id=UUID(computer.device_id), folder=FOLDER, nonce=nonce, history=thread.id)
    if not computer.app.connected:
        await computer.app.connect()
    await eventually(lambda: is_bound(api, str(thread.id)))
    assert (await begin(api, project, str(thread.id))).status_code == 201
    return project, master, await api.app.state.session_store.get_session(thread.id)


def asked(computer, kind: str | None = None) -> list[tuple[str, str]]:
    """The thread kinds *computer*'s app ran, each as (invocation, action), in order."""
    return [(invocation, action) for invocation, _, asked_kind, action in computer.app.places.asked if kind in (None, asked_kind)]


async def opens_of(api, session) -> list[tuple[str, str]]:
    """Each open the journal holds for *session*, oldest first: its name, and how it ended (``ok``, its refusal's type, or ``open``)."""
    async with api.app.state.session_factory() as db:
        rows = (await db.execute(text(
            "SELECT invocation_id, outcome FROM device_operations"
            " WHERE calling_session_id = :id AND kind = 'history' AND args->>'action' = 'open' ORDER BY created_at"
        ), {"id": session.id})).all()
    return [(name, "open" if outcome is None else "ok" if "ok" in outcome else outcome["error"]["type"]) for name, outcome in rows]


async def operations_of(api, session) -> list[tuple[str, str]]:
    """Every operation the journal holds for *session*'s folder, oldest first, as (kind, invocation)."""
    async with api.app.state.session_factory() as db:
        rows = (await db.execute(text(
            "SELECT kind, invocation_id FROM device_operations WHERE root_session_id = :id ORDER BY created_at, ordinal"
        ), {"id": session.id})).all()
    return [tuple(row) for row in rows]


async def turn_end(api, session) -> int:
    """The id of *session*'s last turn end: the next turn's name."""
    return (await api.app.state.session_store.last_event(session.id, *TURN_ENDS)).id


async def failure(api, session) -> dict:
    """How *session*'s last turn failed, as its end says it."""
    [*_, failed] = await api.app.state.session_store.get_events(session.id, types=[EventType.SESSION_FAIL])
    return failed.data


async def steps_of(api, session, *, after: int = 0) -> list[str]:
    """The tools *session*'s steps called since event *after*."""
    return [e.data["name"] for e in await api.app.state.session_store.get_events(session.id, after=after, types=[EventType.TOOL_CALL])]


def refusing_opens(computer, times: int, error: dict | None = None) -> None:
    """*computer* refuses its next *times* opens, as its guest does for a history that did not answer in time."""
    left = [times]
    error = error or {"type": "history", "code": "no_answer", "message": "This folder's history did not answer"}

    def refuse(frame, outcome):
        if (frame["kind"], frame["args"].get("action")) == ("history", "open") and left[0]:
            left[0] -= 1
            return {"error": error}
        return outcome

    computer.app.lie = refuse


async def a_thread_turn(api, monkeypatch, session, replies, **more):
    """One real turn of *session* (``a_turn``), whose model is asked once more for the file its goal names."""
    return await a_turn(api, monkeypatch, session, [*replies, _final_response("That is all.")], **more)


WRITES = calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"}), ("terminal", {"command": "echo made > made.txt"}))


# -- a turn's open -----------------------------------------------------------------------------------------------


async def test_each_turn_opens_its_threads_copy_before_its_first_step_and_its_computer_hears_it_once_a_turn(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    copy = computer.app.places.copy(str(thread.id))

    async def you_save(harness) -> None:
        # You save the file while the thread's turn runs: within a turn the thread sees only its own changes.
        (computer.folder / "Report.docx").write_bytes(b"PK report v2, by you")

    await a_thread_turn(api, monkeypatch, thread, [
        calling(("read_file", {"path": "Report.docx"})),
        calling(("memory", {"action": "add", "content": "The report is the user's."})),
        calling(("read_file", {"path": "Report.docx"})),
        _final_response("Read it."),
    ], during=you_save)
    assert (copy / "Report.docx").read_bytes() == b"PK report v1"
    first = await turn_end(api, thread)
    # Its next turn starts from the folder as it is now.
    await a_thread_turn(api, monkeypatch, thread, [calling(("read_file", {"path": "Report.docx"})), _final_response("Read it.")])
    assert (copy / "Report.docx").read_bytes() == b"PK report v2, by you"
    # Under each turn's own name, once a turn, though each of its steps opened the copy first.
    assert asked(computer, "history") == [("open:0", "open"), (f"open:{first}", "open")]
    assert await opens_of(api, thread) == [("open:0", "ok"), (f"open:{first}", "ok")]
    assert (await api.app.state.session_store.get_session(thread.id)).status == "completed"


@pytest.mark.parametrize("code", ["no_answer", "no_whole_copy", "move_unfinished", "record_unfinished"])
async def test_an_open_refused_in_a_way_asking_again_passes_is_asked_again_at_the_turns_next_step(api, computer, monkeypatch, code):
    _, _, thread = await begun_with_copy(api, computer)
    refusing_opens(computer, 1, {"type": "history", "code": code, "message": "Not now"})
    await a_thread_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"})),
        calling(("read_file", {"path": "Budget.xlsx"})),
        _final_response("The totals are in Budget.xlsx."),
    ])
    # No step was stopped by it: each worked in the copy, which the app opens itself, and never in the folder.
    assert await steps_of(api, thread) == ["write_file", "read_file"]
    assert (computer.app.places.copy(str(thread.id)) / "Budget.xlsx").read_text() == "Total,42\n"
    assert not (computer.folder / "Budget.xlsx").exists()
    # The next step asked again, under the turn's next name, and was answered.
    assert await opens_of(api, thread) == [("open:0", "history"), ("open:0:1", "ok")]
    assert (await api.app.state.session_store.get_session(thread.id)).status == "completed"


async def test_a_turn_whose_open_is_never_answered_asks_it_a_bounded_number_of_times_then_fails_saying_why(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    refusing_opens(computer, 100)
    await a_thread_turn(api, monkeypatch, thread, [
        calling(("read_file", {"path": "Report.docx"})),
        calling(("read_file", {"path": "Plans/Q3.md"})),
        WRITES,
        calling(("read_file", {"path": "Report.docx"})),
        _final_response("Done."),
    ])
    # Each step asked again, and the last refusal stood: the turn's third step and every one after it ran not.
    assert await steps_of(api, thread) == ["read_file", "read_file"]
    assert await opens_of(api, thread) == [(f"open:0{f':{n}' if n else ''}", "history") for n in range(OPEN_TRIES)]
    assert not (computer.app.places.copy(str(thread.id)) / "Budget.xlsx").exists()
    failed = await failure(api, thread)
    assert (failed["reason"], failed["why"], failed["code"], failed["error_title"]) == ("nowhere_to_work", "refused", "no_answer", NOWHERE["refused"])
    assert (await api.app.state.session_store.get_session(thread.id)).status == "failed"


async def test_a_turns_open_waits_for_a_computer_that_is_away_and_its_steps_wait_with_it(api, computer, monkeypatch):
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    _, _, thread = await begun_with_copy(api, computer)
    copy = computer.app.places.copy(str(thread.id))
    await computer.app.disconnect()
    turn = asyncio.create_task(a_thread_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"})), _final_response("Done."),
    ]))

    async def waiting() -> bool:
        return await opens_of(api, thread) == [("open:0", "open")]

    await eventually(waiting)
    await asyncio.sleep(0.3)
    assert not turn.done() and await steps_of(api, thread) == [] and not copy.exists()
    await computer.app.connect()
    await asyncio.wait_for(turn, 30)
    # Back, it heard the open once, and the step ran after it, in the copy.
    assert asked(computer, "history") == [("open:0", "open")]
    assert (copy / "Budget.xlsx").read_text() == "Total,42\n" and not (computer.folder / "Budget.xlsx").exists()


async def test_a_turn_whose_worker_was_killed_in_its_open_waits_for_that_asking_and_its_computer_hears_it_once(api, computer, monkeypatch):
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    _, _, thread = await begun_with_copy(api, computer)
    await computer.app.disconnect()
    # As the worker recorded it before it waited, and was killed with nothing run after; its computer has it, unanswered.
    await journal(api)._record(OperationRequest(
        device_id=UUID(computer.device_id), root_session_id=thread.id, calling_session_id=thread.id,
        invocation_id="open:0", ordinal=1, kind="history", args={"action": "open"},
    ))
    held, let_go = holding_opens(computer, thread)
    await computer.app.connect()

    async def holds() -> bool:
        return bool(held)

    await eventually(holds)
    # The next worker's turn waits for that asking: no step runs before it is answered.
    turn = asyncio.create_task(a_thread_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "Notes.md", "content": "notes\n"})), _final_response("Noted."),
    ]))
    await asyncio.sleep(0.5)
    assert not turn.done() and await steps_of(api, thread) == []
    await let_go()
    await asyncio.wait_for(turn, 30)
    assert asked(computer, "history") == [("open:0", "open")]
    assert await opens_of(api, thread) == [("open:0", "ok")]
    assert (computer.app.places.copy(str(thread.id)) / "Notes.md").read_text() == "notes\n"


async def test_a_worker_stopped_in_a_turns_open_leaves_nothing_the_next_worker_misreads(api, computer, monkeypatch):
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    _, _, thread = await begun_with_copy(api, computer)
    store, factory, redis = api.app.state.session_store, api.app.state.session_factory, api.app.state.redis
    await computer.app.disconnect()
    lost = await store.try_acquire_lease(thread.id, "worker-a", ttl_seconds=60)
    opening = asyncio.create_task(_open_local_copy(thread, store, lost, session_factory=factory, redis=redis))

    async def waiting() -> bool:
        return await opens_of(api, thread) == [("open:0", "open")]

    await eventually(waiting)
    # Stopped while it waits, as a worker that lost its turn is: what it asked is closed, so its computer runs nothing of it.
    opening.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await opening
    assert await opens_of(api, thread) == [("open:0", "cancelled")]
    # It holds the thread's lease no more: what it asks now is recorded nowhere.
    await store.release_lease(thread.id, lost.lease_token)
    taker = await store.try_acquire_lease(thread.id, "worker-b", ttl_seconds=60)
    _, turn, opened = await _open_local_copy(thread, store, lost, session_factory=factory, redis=redis)
    assert (turn, opened) == (0, None)
    assert await opens_of(api, thread) == [("open:0", "cancelled")]
    await store.release_lease(thread.id, taker.lease_token)
    # The next worker reads the closed asking as one the next may pass, and asks under the turn's next name.
    await computer.app.connect()
    await a_thread_turn(api, monkeypatch, thread, [calling(("write_file", {"path": "Notes.md", "content": "notes\n"})), _final_response("Noted.")])
    assert await opens_of(api, thread) == [("open:0", "cancelled"), ("open:0:1", "ok")]
    assert asked(computer, "history") == [("open:0:1", "open")]
    assert (computer.app.places.copy(str(thread.id)) / "Notes.md").read_text() == "notes\n"


# -- a thread with nowhere to work -------------------------------------------------------------------------------


async def test_a_folder_with_no_history_runs_no_step_of_its_threads_turn_which_fails_saying_why(api, computer, monkeypatch):
    # A file whose name history cannot keep: the folder's own history finds it has none.
    os.close(os.open(os.fsencode(computer.folder) + b"/caf\xe9.txt", os.O_CREAT | os.O_WRONLY, 0o644))
    _, _, thread = await begun_with_copy(api, computer)
    folder, place = picture(computer.folder), picture(computer.app.places.place)
    await a_thread_turn(api, monkeypatch, thread, [WRITES, _final_response("Done.")])
    # No step ran: the folder and the place its copies are kept in are as they were, and the computer heard the open alone.
    assert picture(computer.folder) == folder and picture(computer.app.places.place) == place
    assert not computer.app.places.copy(str(thread.id)).exists()
    assert await steps_of(api, thread) == [] and await operations_of(api, thread) == [("bind", "bind"), ("history", "open:0")]
    failed = await failure(api, thread)
    assert (failed["reason"], failed["why"], failed["error_title"], failed["retryable"]) == (
        "nowhere_to_work", "names", NOWHERE["names"], False,
    )
    assert (await api.app.state.session_store.get_session(thread.id)).status == "failed"


async def test_an_app_that_keeps_no_copy_for_the_thread_runs_no_step_of_its_turn_which_fails_saying_to_update_it(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    copy = computer.app.places.copy(str(thread.id))
    await a_thread_turn(api, monkeypatch, thread, [calling(("write_file", {"path": "Notes.md", "content": "notes\n"})), _final_response("Noted.")])
    assert (copy / "Notes.md").read_text() == "notes\n"
    # Its computer's app now has the thread bound to the folder itself, as an app older than copies binds it.
    del computer.app.places.threads[str(thread.id)]
    folder, kept, before = picture(computer.folder), picture(copy), await operations_of(api, thread)
    turn, after = await turn_end(api, thread), (await api.app.state.session_store.get_events(thread.id))[-1].id
    await a_thread_turn(api, monkeypatch, thread, [WRITES, _final_response("Done.")])
    # Nothing of the turn reached the folder, nor the copy: its computer was asked the turn's open alone.
    assert picture(computer.folder) == folder and picture(copy) == kept
    assert (await operations_of(api, thread))[len(before):] == [("history", f"open:{turn}")]
    assert await steps_of(api, thread, after=after) == []
    failed = await failure(api, thread)
    assert (failed["reason"], failed["why"], failed["code"], failed["error_title"]) == ("nowhere_to_work", NO_COPY, NO_COPY, NOWHERE[NO_COPY])
    assert failed["retryable"] is False


@pytest.mark.parametrize(("case", "lie", "code"), [
    ("a history that refuses the project's", {"error": {"type": "history", "code": "history_refused", "message": "no"}}, "history_refused"),
    ("a folder no longer there", {"error": {"type": "folder_unavailable", "message": "The folder is gone"}}, "folder_unavailable"),
    ("what is no answer", {"ok": {"copy": "/etc", "session": "another"}}, "not_an_answer"),
])
async def test_an_open_refused_in_a_way_asking_again_would_not_pass_runs_no_step_and_fails_the_turn(api, computer, monkeypatch, case, lie, code):
    _, _, thread = await begun_with_copy(api, computer)
    computer.app.lie = lambda frame, outcome: lie if frame["args"].get("action") == "open" else outcome
    folder = picture(computer.folder)
    await a_thread_turn(api, monkeypatch, thread, [WRITES, _final_response("Done.")])
    assert await steps_of(api, thread) == [], case
    assert picture(computer.folder) == folder and not (computer.app.places.copy(str(thread.id)) / "Budget.xlsx").exists()
    assert asked(computer) == [("open:0", "open")]
    failed = await failure(api, thread)
    assert (failed["why"], failed["code"], failed["error_title"], failed["retryable"]) == ("refused", code, NOWHERE["refused"], True)


# -- only a thread's own turn opens ------------------------------------------------------------------------------


def holding_opens(computer, thread):
    """*computer* takes *thread*'s opens and answers none until let go; the frames it holds, and how to let them go."""
    held: list = []
    handle = computer.app._handle

    async def holding(frame, ws) -> None:
        if frame["kind"] == "history" and frame["session_id"] == str(thread.id) and frame["args"].get("action") == "open":
            held.append((frame, ws))
            return
        await handle(frame, ws)

    async def let_go() -> None:
        computer.app._handle = handle
        for frame, ws in held:
            await handle(frame, ws)

    computer.app._handle = holding
    return held, let_go


async def test_only_a_threads_own_turn_opens_its_copy_and_no_other_sessions_step_waits_for_it(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    held, let_go = holding_opens(computer, thread)
    opening = asyncio.create_task(thread_copy(
        thread, session_factory=api.app.state.session_factory, redis=api.app.state.redis, lease_token=None,
    ).opened(0))

    async def holds() -> bool:
        return bool(held)

    await eventually(holds)
    # Its helper's turn works in the thread's copy, opens none, and waits for no open of the thread's.
    helper = await create_child_session(store=api.app.state.session_store, parent=thread, channel="worker")
    await asyncio.wait_for(a_thread_turn(api, monkeypatch, helper, [
        calling(("write_file", {"path": "Notes.md", "content": "notes\n"})), _final_response("Noted."),
    ]), 30)
    assert (computer.app.places.copy(str(thread.id)) / "Notes.md").read_text() == "notes\n"
    # Another thread on the folder opens its own copy, under its own turn, and works in it.
    _, _, other = await begun_with_copy(api, computer, nonce="second-thread-nonce-0002")
    await asyncio.wait_for(a_thread_turn(api, monkeypatch, other, [
        calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"})), _final_response("Done."),
    ]), 30)
    assert (computer.app.places.copy(str(other.id)) / "Budget.xlsx").read_text() == "Total,42\n"
    assert not opening.done()
    await let_go()
    assert "copy" in await asyncio.wait_for(opening, 30)
    # Each thread opened its own, as itself; no session under one, nor any other, asked any.
    async with api.app.state.session_factory() as db:
        opens = (await db.execute(text(
            "SELECT calling_session_id, root_session_id, invocation_id FROM device_operations WHERE kind = 'history'"
            " AND root_session_id IN (:a, :b) ORDER BY created_at"
        ), {"a": thread.id, "b": other.id})).all()
    assert [tuple(row) for row in opens] == [(thread.id, thread.id, "open:0"), (other.id, other.id, "open:0")]
    assert not (computer.folder / "Notes.md").exists() and not (computer.folder / "Budget.xlsx").exists()


async def test_a_thread_or_a_chat_bound_to_its_folder_itself_asks_no_open_and_works_there_as_before(api, laptop, monkeypatch):
    store = api.app.state.session_store
    _, _, thread = await begun_local(api, laptop)
    await a_thread_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"})), _final_response("Done."),
    ])
    laptop.app.prepare("chat-binding-nonce-0001", FOLDER)
    created = await api.client.post(
        "/v1/sessions", json={"execution": confirmed(laptop.device_id, nonce="chat-binding-nonce-0001")}, headers=api.auth(),
    )
    assert created.status_code == 201, created.text
    await eventually(lambda: is_bound(api, created.json()["id"]))
    chat = await store.get_session(UUID(created.json()["id"]))
    await a_turn(api, monkeypatch, chat, [calling(("write_file", {"path": "Notes.md", "content": "notes\n"})), _final_response("Noted.")])
    for session, name, data in ((thread, "Budget.xlsx", "Total,42\n"), (chat, "Notes.md", "notes\n")):
        assert (laptop.folder / name).read_text() == data
        assert (await store.get_session(session.id)).status == "completed"
        # Its computer was asked none of a thread's own kinds, and no step waited for one.
        assert [kind for kind, _ in await operations_of(api, session) if kind in THREAD_KINDS] == []
    assert not set(laptop.app.ran) & THREAD_KINDS


# -- a stopped thread's open --------------------------------------------------------------------------------------


async def test_a_paused_threads_open_reaches_its_computer_and_its_files_and_commands_do_not(api, computer):
    _, _, thread = await begun_with_copy(api, computer)
    await api.app.state.session_store.update_session_status(thread.id, "paused")
    copy = thread_copy(thread, session_factory=api.app.state.session_factory, redis=api.app.state.redis, lease_token=None)
    assert await asyncio.wait_for(copy.opened(0), 30) == {"copy": "made"}
    assert asked(computer) == [("open:0", "open")]
    for kind, args in [("write", {"key": f"{computer.folder}/a.txt", "data": ""}), ("run", {"command": "ls"})]:
        with pytest.raises(DeviceOperationError, match="This session was stopped"):
            await journal(api).run(OperationRequest(
                device_id=UUID(computer.device_id), root_session_id=thread.id, calling_session_id=thread.id,
                invocation_id="17:call_1", ordinal=1, kind=kind, args=args,
            ))
    assert sorted(path.name for path in computer.app.places.copy(str(thread.id)).iterdir()) == ["Plans", "Report.docx"]
