"""Your edits picked up at each landing, and the redo of a thread's change that a newer file kept out."""

from __future__ import annotations

import asyncio

import pytest

import surogates.harness.loop as loop_module
from surogates.config import SHARED_WORK_QUEUE_KEY
from surogates.harness import landing as landing_module
from surogates.harness import loop_artifact_completion
from surogates.harness.loop_context_replay import unread_reports, worker_note
from surogates.harness.loop_pending import NAMES_ANSWERS
from surogates.runtime import SlashCommandConfig
from surogates.sandbox.history import History
from surogates.sandbox.pool import SandboxPool
from surogates.session.events import EventType
from surogates.workstreams.stream import STREAM_TYPES
from tests.test_steer_loop import _final_response, _make_loop_harness

from .test_command_wake_once import DiesWriting, workers  # noqa: F401  (workers is a fixture)
from .test_devices import api  # noqa: F401  (api is a fixture)
from .test_durable_landings import (  # noqa: F401  (a_short_fence is a fixture)
    a_short_fence,
    edited,
    ends,
    rows,
    stored,
    turn_ends,
)
from .test_thread_copies import (  # noqa: F401  (pods is a fixture)
    QUICK,
    a_thread,
    a_waking_thread_harness,
    git,
    its_first_turn_was_taken,
    open_pod,
    pods,
    reports,
)
from .test_turn_sagas import a_turn, calling
from .test_workstream_threads import queued
from .test_workstreams import create, master_of

pytestmark = pytest.mark.asyncio(loop_scope="session")


def outcomes_of(monkeypatch) -> list[dict]:
    """Each landing's outcome from here on, as its turn's end is answered it."""
    outcomes: list[dict] = []
    land_turn = landing_module.land_turn

    async def landed(**arguments):
        outcomes.append(await land_turn(**arguments))
        return outcomes[-1]

    monkeypatch.setattr(loop_artifact_completion, "land_turn", landed)
    return outcomes


async def test_your_upload_during_a_turn_is_its_landings_pickup_by_you(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    (pods.project / "brief.pdf").write_bytes(b"%PDF uploaded while the thread worked")
    outcomes = outcomes_of(monkeypatch)
    await ends(api, pool, thread)
    durable = pods.project / "_history"
    [row] = await rows(api, thread)
    pickup = git(durable, "rev-parse", f"{row.commit}^1")
    assert git(durable, "log", "-1", "--format=%ae", pickup) == f"user:{thread.user_id}@surogate"
    assert git(durable, "log", "-1", "--format=%(trailers:key=Surogate-Kind,valueonly)", pickup) == "pickup"
    assert git(durable, "log", "-1", "--format=%(trailers:key=Surogate-Saga,valueonly)", pickup) == row.saga_id
    assert [(f["path"], f["before"]) for f in row.picked_up] == [("brief.pdf", None)]
    assert row.steps[0]["tool_name"] == "history.pickup"
    assert [o["picked_up"] for o in outcomes] == [row.picked_up]


async def test_a_file_a_landing_left_out_names_who_changed_it_in_its_rows_commit_step(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md && echo 'a file where you made a folder' > Plans && printf ' by A' >> notes.txt")
    (pods.project / "Plans").mkdir()
    (pods.project / "Plans" / "q1.md").write_text("q1, uploaded by you")
    (pods.project / "notes.txt").write_text("v2 notes, saved by you\n")
    await ends(api, pool, thread)
    [row] = await rows(api, thread)
    commit = row.steps[1]
    # Your folder is in no history but this landing's own pickup, which the commit step is told.
    assert commit["tool_name"] == "history.commit"
    assert {o["path"]: (o["reason"], o.get("by")) for o in commit["result"]["overlapped"]} == {
        "Plans": ("shape", {"kind": "you"}), "notes.txt": ("changed", {"kind": "you"}),
    }
    assert sorted((f["path"], f["merged"]) for f in row.files) == [("Plans", False), ("a.md", True), ("notes.txt", False)]


async def test_a_turn_that_changed_nothing_records_none_of_your_edits(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    await ends(api, pool, thread)
    await edited(pool, thread, "cat notes.txt")
    (pods.project / "notes.txt").write_text("v2 notes, saved by you\n")
    await ends(api, pool, thread)
    # It lands nothing: it leaves no row, main does not move, and your edit is in no history yet.
    [landed] = await rows(api, thread)
    durable = pods.project / "_history"
    assert git(durable, "rev-parse", "refs/heads/main") == landed.commit
    assert git(durable, "show", "refs/heads/main:notes.txt") == "v1 notes"


async def test_a_landing_whose_push_answer_was_lost_lists_your_edits_all_the_same(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    (pods.project / "brief.pdf").write_bytes(b"%PDF uploaded while the thread worked")
    call = landing_module._call

    async def the_push_answer_is_lost(sandbox_pool, owner, action, **arguments):
        result = await call(sandbox_pool, owner, action, **arguments)
        if action == "record":
            raise landing_module.LandingStepError("the pod's step timed out")
        return result

    monkeypatch.setattr(landing_module, "_call", the_push_answer_is_lost)
    outcomes = outcomes_of(monkeypatch)
    await ends(api, pool, thread)
    # The push happened: the landing counts, and the row it is settled into names your upload as one that ended well does.
    [row] = await rows(api, thread)
    assert (row.saga_state, row.commit) == ("completed", git(pods.project / "_history", "rev-parse", "refs/heads/main"))
    assert [(f["path"], f["before"]) for f in row.picked_up] == [("brief.pdf", None)]
    [outcome] = outcomes
    assert (outcome["commit"], outcome["picked_up"]) == (row.commit, row.picked_up)


async def test_a_landing_whose_pickup_failed_keeps_the_turn_on_its_branch(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    call = landing_module._call

    async def the_pickup_fails(sandbox_pool, owner, action, **arguments):
        if action == "pickup":
            raise landing_module.LandingStepError("git add failed: Input/output error")
        return await call(sandbox_pool, owner, action, **arguments)

    with monkeypatch.context() as patch:
        patch.setattr(landing_module, "_call", the_pickup_fails)
        await ends(api, pool, thread)
    # It never reached its commit step: the turn's end put the turn in the history, before its pod went.
    assert git(pods.project / "_history", "show", f"refs/heads/threads/{thread.id}:a.md") == "a"
    assert [done["saved"] for done in await turn_ends(api, thread)] == [True]
    assert pods.real_names() == ["Report.docx", "notes.txt"]
    # And again on its one retry.
    assert [(row.saga_state, [(s["tool_name"], s["state"]) for s in row.steps]) for row in await rows(api, thread)] == [
        ("compensated", [("history.pickup", "failed")]),
    ] * 2
    # The landing answered, as one whose commit step failed does: nothing of it reached the real files.
    assert [report["landing"] for report in await reports(api, master)] == ["compensated"]
    await ends(api, SandboxPool(pods), thread)  # its next turn, with no tool
    assert pods.real_names() == ["Report.docx", "a.md", "notes.txt"]


async def a_clash(api, monkeypatch, pods, pool, thread, yours: bytes) -> None:
    """*thread*'s copy made, then you save the report, then its turn edits the report too and ends."""
    await open_pod(pool, thread)
    (pods.project / "Report.docx").write_bytes(yours)
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "printf ' by A' >> Report.docx && echo a > a.md"})),
        _final_response("Edited the report."),
    ], pool=pool)


async def test_your_edit_then_a_landing_tells_the_thread_to_redo_and_the_redo_lands_both_changes(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v2 by you")
    # Your report stays, its other file lands, and your edit is a pickup by you.
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v2 by you"
    assert (pods.project / "a.md").read_text() == "a\n"
    durable = pods.project / "_history"
    landing = git(durable, "rev-parse", "refs/heads/main")
    assert git(durable, "log", "-1", "--format=%ae", f"{landing}^1") == f"user:{thread.user_id}@surogate"
    # The thread is told, after its turn's end and past its cursor.
    [redo] = await store.get_events(thread.id, types=[EventType.HISTORY_REDO])
    assert redo.data["files"] == [{"path": "Report.docx", "reason": "changed", "by": {"kind": "you"}}]
    [done] = await store.get_events(thread.id, types=[EventType.SESSION_COMPLETE])
    assert redo.id > done.id and redo.id > await store.get_harness_cursor(thread.id)
    note = {"role": "user", "content": (
        "[Your changes to these files were not applied:\n"
        "- Report.docx: it changed since you started (by the user)\n"
        "Re-apply your change to the current version of each.]"
    )}
    # Its next request reads it, as a master's reads a report: replay leaves it out until then.
    assert unread_reports(await store.get_events(thread.id)) == [note]
    assert note not in _make_loop_harness(session_store=store)._rebuild_messages(await store.get_events(thread.id))
    # The master hears the file is being redone: not finished.
    [report] = await reports(api, master)
    assert {f["ref"]: f["landing"] for f in report["files"]} == {"Report.docx": "redoing", "a.md": "landed"}
    assert worker_note(EventType.WORKER_COMPLETE.value, report)["content"].endswith(
        "Files: a.md\nBeing redone: Report.docx"
    )
    # The redo turn starts on the landing, with your version, and lands its edit on it.
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "printf ' by A' >> Report.docx"})),
        _final_response("Redid the edit."),
    ], pool=pool)
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v2 by you by A"
    assert len(await store.get_events(thread.id, types=[EventType.HISTORY_REDO])) == 1
    # Replay puts the redo where that turn's first request read it: after the turn before it, on its own.
    replayed = _make_loop_harness(session_store=store)._rebuild_messages(await store.get_events(thread.id))
    at = replayed.index(note)
    assert (replayed[at - 1]["role"], replayed[at - 1]["content"]) == ("assistant", "Edited the report.")
    assert replayed[at + 1]["tool_calls"][0]["function"]["name"] == "terminal"
    assert unread_reports(await store.get_events(thread.id)) == []


async def test_a_redo_wakes_its_finished_thread(api, monkeypatch, pods):
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await api.app.state.redis.delete(SHARED_WORK_QUEUE_KEY)
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v2 by you")
    # Queued by its redo, as a report queues its master.
    assert (await store.get_session(thread.id)).status == "completed" and await queued(api, thread)
    ran: list = []

    async def the_redo_turn(session, *_, **__):
        ran.append(session.status)

    await a_waking_thread_harness(api, monkeypatch, pool, the_redo_turn).wake(thread.id)
    assert ran == ["active"]
    [resumed] = await store.get_events(thread.id, types=[EventType.SESSION_RESUME])
    assert resumed.data == {"source": "history_redo"}


async def test_a_redo_the_users_limit_holds_back_waits_for_the_next_message(api, monkeypatch, pods):
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v2 by you")
    ran: list = []

    async def the_redo_turn(session, *_, **__):
        ran.append(session.id)

    async def refused(session, content):
        return "You have used this month's allowance."

    harness = a_waking_thread_harness(api, monkeypatch, pool, the_redo_turn)
    harness._admit_turn = refused
    await harness.wake(thread.id)
    # No turn, and no failure: the redo waits for the user's next message, as a report does.
    assert ran == [] and (await store.get_session(thread.id)).status == "completed"
    assert await store.get_events(thread.id, types=[EventType.SESSION_FAIL]) == []


@pytest.mark.parametrize("command, skill", [
    ("/report-writer Edit the report.", "report-writer"), ("/deep-research Q3 revenue by region", None),
], ids=["skill", "deep-research"])
async def test_a_redo_wake_reads_the_redo_and_runs_no_command_of_the_users_again(api, monkeypatch, pods, command, skill):
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await its_first_turn_was_taken(store, thread)
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": command})
    if skill:
        # Its own wake expanded it.
        await store.emit_event(thread.id, EventType.SKILL_INVOKED, {"skill": skill, "raw_message": command})
    else:
        # Its own wake answered it: a thread is refused it.
        await a_waking_thread_harness(api, monkeypatch, pool, None).wake(thread.id)
        [refusal] = [e for e in await store.get_events(thread.id, types=[EventType.LLM_RESPONSE]) if "answers" in e.data]
        assert refusal.data["message"]["content"].startswith("A thread can't start /deep-research yet")
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v2 by you")
    expanded: list = []

    async def a_skill(**kwargs):
        expanded.append(kwargs["text"])
        return "Follow the report-writer skill: edit the report.", "report-writer", None, "skill"

    monkeypatch.setattr(loop_module, "expand_slash_skill", a_skill)
    monkeypatch.setattr(loop_module, "expand_skill_again", a_skill)
    seen: list = []

    async def the_redo_turn(session, messages, *_, all_events, **__):
        # What the turn's first request ends on: the news no request has read.
        seen.append([*messages, *unread_reports(all_events)][-1]["content"])

    harness = a_waking_thread_harness(api, monkeypatch, pool, the_redo_turn)
    del harness._rebuild_messages  # the log replayed as it is
    await harness.wake(thread.id)
    # The turn reads what it was woken for; the user's command is neither run nor expanded again.
    assert seen and seen[-1].startswith("[Your changes to these files were not applied:")
    assert expanded == []
    assert len([e for e in await store.get_events(thread.id, types=[EventType.LLM_RESPONSE]) if "answers" in e.data]) == (not skill)


YOURS = b"PK\x03\x04 report v2 by you"
REDO_OF_THE_REPORT = (
    "[Your changes to these files were not applied:\n"
    "- Report.docx: it changed since you started (by the user)\n"
    "Re-apply your change to the current version of each.]"
)
A_COMMAND = "/loop 5m Add a line to Report.docx."
REFUSED = "A thread can't start /loop yet: do this step in the thread itself."


async def a_clash_among(workers, pods, pool, thread) -> None:
    """``a_clash`` with you, its turn's scripted model gone after it: the workers' own answers their wakes."""
    with pytest.MonkeyPatch.context() as scripted:
        await a_clash(workers.api, scripted, pods, pool, thread, YOURS)


async def resumed_by(store, thread) -> list[str]:
    return [e.data.get("source") for e in await store.get_events(thread.id, types=[EventType.SESSION_RESUME])]


async def test_a_command_typed_after_a_redo_is_answered_first_and_the_redo_keeps_its_turn(workers, monkeypatch, pods):
    api, store = workers.api, workers.store
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    workers.sandbox_pool = pool = SandboxPool(pods)
    await a_clash_among(workers, pods, pool, thread)
    [redo] = await store.get_events(thread.id, types=[EventType.HISTORY_REDO])
    # Before the redo's own wake comes, its user types a command.
    await workers.says(thread.id, A_COMMAND)
    await workers.nobody_is_queued()
    await workers.wake(thread.id, SlashCommandConfig())
    # Answered with no turn of the model's.  The redo is not passed over: the thread rests before it, queued for it.
    assert (await workers.said(thread.id))[-1] == REFUSED and workers.requests == []
    assert await workers.status(thread.id) == "completed"
    assert await store.get_harness_cursor(thread.id) < redo.id and await queued(api, thread)
    await workers.nobody_is_queued()
    await workers.wake(thread.id, SlashCommandConfig())
    # The redo's turn.  Its request ends on the redo, as a provider takes it: the command's answer stands before.
    [request] = workers.requests
    assert [m["content"] for m in request[-3:]] == [A_COMMAND, REFUSED, REDO_OF_THE_REPORT]
    assert (await resumed_by(store, thread))[-1] == "history_redo"
    # Replay rebuilds the request that was sent.
    events = await store.get_events(thread.id)
    asked = max(i for i, e in enumerate(events) if e.type == EventType.LLM_REQUEST.value)
    assert workers.worker()._rebuild_messages(events[:asked + 1]) == request
    # Read, the redo wakes nobody again, and the command was answered once.
    assert not await queued(api, thread)
    await workers.wake(thread.id, SlashCommandConfig())
    assert len(workers.requests) == 1 and (await workers.said(thread.id)).count(REFUSED) == 1


async def test_a_command_typed_during_a_turn_whose_landing_clashes_does_not_pass_the_redo_over(workers, monkeypatch, pods):
    api, store = workers.api, workers.store
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    workers.sandbox_pool = pool = SandboxPool(pods)
    await its_first_turn_was_taken(store, thread)
    # The wake of the turn that clashes, as this harness begins one.
    await store.emit_event(thread.id, EventType.HARNESS_WAKE, {"worker_id": "worker-sagas", "cursor": 0, NAMES_ANSWERS: True})
    await open_pod(pool, thread)
    (pods.project / "Report.docx").write_bytes(YOURS)

    async def its_user_types_a_command(_harness) -> None:
        await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": A_COMMAND})

    with pytest.MonkeyPatch.context() as scripted:
        await a_turn(api, scripted, thread, [
            calling(("memory", {"action": "add", "content": "The report is a docx."})),
            calling(("terminal", {"command": "printf ' by A' >> Report.docx"})),
            _final_response("Edited the report."),
        ], pool=pool, during=its_user_types_a_command)
    [redo] = await store.get_events(thread.id, types=[EventType.HISTORY_REDO])
    await workers.nobody_is_queued()
    # The turn left the command for its own wake, which the redo's finds waiting: it answers the command.
    await workers.wake(thread.id, SlashCommandConfig())
    assert (await workers.said(thread.id))[-1] == REFUSED and workers.requests == []
    assert await store.get_harness_cursor(thread.id) < redo.id and await queued(api, thread)
    # And the redo has its turn after: the command stands where the turn it was typed in ended.
    await workers.nobody_is_queued()
    await workers.wake(thread.id, SlashCommandConfig())
    [request] = workers.requests
    assert [m["content"] for m in request[-4:]] == ["Edited the report.", A_COMMAND, REFUSED, REDO_OF_THE_REPORT]
    assert (await resumed_by(store, thread))[-1] == "history_redo"
    assert not await queued(api, thread)
    await workers.wake(thread.id, SlashCommandConfig())
    assert len(workers.requests) == 1 and (await workers.said(thread.id)).count(REFUSED) == 1


async def test_a_redo_turn_cut_off_before_its_first_request_is_still_the_redos(workers, monkeypatch, pods):
    api, store = workers.api, workers.store
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    workers.sandbox_pool = pool = SandboxPool(pods)
    await its_first_turn_was_taken(store, thread)
    # The thread's last message is a command, answered at its own wake.
    assert await workers.types(thread.id, A_COMMAND, SlashCommandConfig()) == REFUSED
    await a_clash_among(workers, pods, pool, thread)
    await workers.nobody_is_queued()
    # The redo's wake revives the thread, and its worker dies before the model is asked.
    dying = workers.worker(SlashCommandConfig(), store=DiesWriting(store, lambda kind, _: kind == EventType.LLM_REQUEST))
    with pytest.raises(asyncio.CancelledError):
        await dying.wake(thread.id)
    assert (await resumed_by(store, thread))[-1] == "history_redo"
    assert (await workers.status(thread.id), workers.requests) == ("active", [])
    # The wake that recovers the thread finds it active: the turn is the redo's all the same, and the
    # command is neither run again nor taken for the turn's end.
    await workers.wake(thread.id, SlashCommandConfig())
    [request] = workers.requests
    assert request[-1] == {"role": "user", "content": REDO_OF_THE_REPORT}
    assert (await workers.said(thread.id)).count(REFUSED) == 1 and workers.ran == []


async def test_a_message_typed_after_a_redo_has_the_turn_and_its_skill_runs_once(workers, monkeypatch, pods):
    api, store = workers.api, workers.store
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    workers.sandbox_pool = pool = SandboxPool(pods)
    await a_clash_among(workers, pods, pool, thread)
    expanded: list = []

    async def a_skill(**kwargs):
        expanded.append(kwargs["text"])
        return "Follow the report-writer skill: tidy the notes.", "report-writer", None, "skill"

    monkeypatch.setattr(loop_module, "expand_slash_skill", a_skill)
    await workers.says(thread.id, "/report-writer Tidy the notes.")
    await workers.wake(thread.id, SlashCommandConfig())
    # The message is new: its skill runs, once, and the request reads the redo after it, on its own.
    [request] = workers.requests
    assert [m["content"] for m in request[-2:]] == ["Follow the report-writer skill: tidy the notes.", REDO_OF_THE_REPORT]
    assert expanded == ["/report-writer Tidy the notes."]


THE_EDIT = calling(("terminal", {"command": "printf ' by A' >> Report.docx"}))
A_SKILL = "/report-writer Tidy the notes."
THE_SKILL = "Follow the report-writer skill: tidy the notes."


def skills_expanded(monkeypatch) -> list[str]:
    """Each message a wake expands a skill for from here on, at its own wake or again at a later one."""
    expanded: list[str] = []

    async def a_skill(**kwargs):
        expanded.append(kwargs["text"])
        return THE_SKILL, "report-writer", None, "skill"

    monkeypatch.setattr(loop_module, "expand_slash_skill", a_skill)
    monkeypatch.setattr(loop_module, "expand_skill_again", a_skill)
    return expanded


def with_real_tools(workers, store=None):
    """A worker whose tools run for real in the thread's pod, so that its turn's end lands what it changed."""
    harness = workers.worker(SlashCommandConfig(), store=store)
    del harness._tools.dispatch
    harness._saga_settings = QUICK
    return harness


async def a_clash_while_its_user_types(workers, pods, pool, thread, typed: str) -> None:
    """``a_clash_among``, its user typing *typed* as the turn lands: after its last request, before its redo is written."""
    land_turn = landing_module.land_turn

    async def typed_meanwhile(**arguments):
        await workers.store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": typed})
        return await land_turn(**arguments)

    with pytest.MonkeyPatch.context() as scripted:
        scripted.setattr(loop_artifact_completion, "land_turn", typed_meanwhile)
        await a_clash(workers.api, scripted, pods, pool, thread, YOURS)


def dies_as_the_model_answers(store) -> DiesWriting:
    """The store of a worker that dies once the model was asked, before its answer is written."""
    return DiesWriting(store, lambda kind, data: kind == EventType.LLM_RESPONSE and "answers" not in data)


@pytest.mark.parametrize("typed", ["as its last message before the clash", "while the clashing turn landed", "after the redo"])
async def test_a_redo_turn_cut_off_after_the_model_was_asked_keeps_its_redo_beside_a_skill(workers, monkeypatch, pods, typed):
    api, store = workers.api, workers.store
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    workers.sandbox_pool = pool = SandboxPool(pods)
    expanded = skills_expanded(monkeypatch)
    if typed == "as its last message before the clash":
        await its_first_turn_was_taken(store, thread)
        # Its own wake ran the skill, and the model answered it.
        assert await workers.types(thread.id, A_SKILL, SlashCommandConfig()) == "Noted."
        assert workers.requests[-1][-1]["content"] == THE_SKILL
        await a_clash_among(workers, pods, pool, thread)
    elif typed == "while the clashing turn landed":
        await a_clash_while_its_user_types(workers, pods, pool, thread, A_SKILL)
    else:
        await a_clash_among(workers, pods, pool, thread)
        await workers.says(thread.id, A_SKILL)
    await workers.nobody_is_queued()
    workers.requests.clear()
    # The turn that reads the redo is cut off once the model was asked; the wake that recovers it asks again.
    workers.replies = [_final_response("Never written."), THE_EDIT, _final_response("Redid the edit.")]
    with pytest.raises(asyncio.CancelledError):
        await with_real_tools(workers, store=dies_as_the_model_answers(store)).wake(thread.id)
    await with_real_tools(workers).wake(thread.id)
    cut_off, recovered = workers.requests[:2]
    # The same request: the redo in it once, and no skill written over it.
    assert recovered == cut_off and [m["content"] for m in recovered].count(REDO_OF_THE_REPORT) == 1
    assert recovered[-1] == {"role": "user", "content": REDO_OF_THE_REPORT}
    if typed == "as its last message before the clash":
        # That message's skill ran at its own turn: it is not given to the model again as something new.
        assert expanded == [A_SKILL] and [m["content"] for m in recovered].count(THE_SKILL) == 0
    elif typed == "after the redo":
        assert [m["content"] for m in recovered[-2:]] == [THE_SKILL, REDO_OF_THE_REPORT]
        assert [m["content"] for m in recovered].count(THE_SKILL) == 1
    # The redo is done, once, and its master reads the file landed.
    assert (pods.project / "Report.docx").read_bytes() == YOURS + b" by A"
    assert len(await store.get_events(thread.id, types=[EventType.HISTORY_REDO])) == 1
    assert {f["ref"]: f["landing"] for f in (await reports(api, master))[-1]["files"]} == {"Report.docx": "landed"}
    assert await workers.status(thread.id) == "completed"
    asked = len(workers.requests)
    await with_real_tools(workers).wake(thread.id)
    assert len(workers.requests) == asked


async def test_a_redo_revives_no_thread_whose_turn_failed(api, monkeypatch, pods):
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await a_clash(api, monkeypatch, pods, pool, thread, YOURS)
    # A failed thread is its user's to retry: their next message's turn reads the redo.
    await store.update_session_status(thread.id, "failed")
    ran: list = []

    async def a_turn_of_its_own(session, *_, **__):
        ran.append(session.id)

    await a_waking_thread_harness(api, monkeypatch, pool, a_turn_of_its_own).wake(thread.id)
    assert ran == [] and (await store.get_session(thread.id)).status == "failed"
    assert await store.get_events(thread.id, types=[EventType.SESSION_RESUME]) == []


async def test_a_turn_that_makes_its_own_file_a_folder_is_not_told_to_redo_it(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo plan > Plans")
    await ends(api, pool, thread)
    await edited(pool, thread, "rm Plans && mkdir Plans && echo q1 > Plans/q1.md")
    await ends(api, pool, thread)
    # Nobody else changed it: the real files cannot take the turn's own change of shape, and no one is asked to redo it.
    assert await api.app.state.session_store.get_events(thread.id, types=[EventType.HISTORY_REDO]) == []
    _, second = await reports(api, master)
    assert {f["ref"]: (f["landing"], f.get("reason")) for f in second["files"]} == {
        "Plans": ("not_merged", "shape"), "Plans/q1.md": ("not_merged", "shape"),
    }


async def test_a_redo_note_names_who_changed_each_file_and_no_one_for_a_file_held_with_them():
    from surogates.harness.loop_context_replay import redo_note

    note = redo_note({"files": [
        {"path": "Report.docx", "reason": "changed", "by": {"kind": "thread", "id": "t2", "title": "Draft B"}},
        {"path": "Plans", "reason": "shape", "by": {"kind": "routine", "name": "Health check"}},
        {"path": "notes.txt", "reason": "with"},
    ]})
    assert note == {"role": "user", "content": (
        "[Your changes to these files were not applied:\n"
        '- Report.docx: it changed since you started (by thread "Draft B")\n'
        "- Plans: the project now has a folder where you made a file, or a file where you made a folder"
        ' (by the routine "Health check")\n'
        "- notes.txt: it goes with a change that was not applied\n"
        "Re-apply your change to the current version of each.]"
    )}


async def test_a_file_changed_between_the_pickup_and_its_apply_is_retried_as_an_overlap(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    apply, saved = History.apply, pods.root / "saved"

    def your_save_lands_first(self, path, before, after):
        # Once: the pod forks a child per call, which inherits this.
        if path == "c.md" and not saved.exists():
            saved.touch()
            (self.project / "c.md").write_text("saved by you just now")
        return apply(self, path, before, after)

    monkeypatch.setattr(History, "apply", your_save_lands_first)
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "for f in a b c d e; do echo $f > $f.md; done"})),
        _final_response("Wrote five notes."),
    ], pool=SandboxPool(pods), saga_settings=QUICK)
    # Rolled back whole, then tried once more: your c.md stays, the other four land.
    assert pods.real_names() == ["Report.docx", "a.md", "b.md", "c.md", "d.md", "e.md", "notes.txt"]
    assert (pods.project / "c.md").read_text() == "saved by you just now"
    put_back, retried = await rows(api, thread)
    assert (put_back.saga_state, retried.saga_state) == ("compensated", "completed")
    # Your save is the retry's pickup, by you.
    assert [f["path"] for f in retried.picked_up] == ["c.md"]
    [report] = await reports(api, master)
    assert "landing" not in report and {f["ref"]: f["landing"] for f in report["files"]}["c.md"] == "redoing"
    [redo] = await api.app.state.session_store.get_events(thread.id, types=[EventType.HISTORY_REDO])
    assert redo.data["files"] == [{"path": "c.md", "reason": "changed", "by": {"kind": "you"}}]


async def test_a_file_whose_author_a_prunings_cut_hides_is_not_redone():
    # Its landing names no one for it (the thread's base is behind the cut): no redo is started on a guess.
    outcome = {
        "state": "completed", "saved": False, "overlapped": [{"path": "notes.txt", "reason": "changed"}],
        "files": [{"ref": "notes.txt", "landing": "not_merged"}],
    }
    landing_module._tell(outcome)
    assert (outcome["redo"], outcome["saved"], outcome["files"][0]["landing"]) == ([], False, "not_merged")
    # Beside a file someone is named for, that one alone is redone, and the turn's end saved less than its work.
    outcome["overlapped"].append({"path": "Report.docx", "reason": "changed", "by": {"kind": "you"}})
    outcome["files"].append({"ref": "Report.docx", "landing": "not_merged"})
    landing_module._tell(outcome)
    assert outcome["redo"] == [{"path": "Report.docx", "reason": "changed", "by": {"kind": "you"}}]
    assert ([f["landing"] for f in outcome["files"]], outcome["saved"]) == (["not_merged", "redoing"], False)


async def test_a_redo_takes_the_files_held_with_a_clash_and_only_a_landing_that_completed_tells_of_one():
    held = [
        {"path": "Draft.docx", "reason": "changed", "by": {"kind": "thread", "id": "t2", "title": "Draft B"}},
        {"path": "Final.docx", "reason": "with"},
    ]
    files = [{"ref": "Draft.docx", "landing": "not_merged"}, {"ref": "Final.docx", "landing": "not_merged"}]
    # A move whose old name changed meanwhile: both names are redone, and the turn's end saved its work.
    outcome = {"state": "completed", "saved": False, "overlapped": held, "files": [dict(f) for f in files]}
    landing_module._tell(outcome)
    assert outcome["redo"] == [
        {"path": "Draft.docx", "reason": "changed", "by": {"kind": "thread", "id": "t2", "title": "Draft B"}},
        {"path": "Final.docx", "reason": "with"},
    ]
    assert ([f["landing"] for f in outcome["files"]], outcome["saved"]) == (["redoing", "redoing"], True)
    # A landing put back for good applied nothing: its turn is on its branch, and lands whole with the next.
    put_back = {"state": "compensated", "saved": True, "overlapped": held, "files": [dict(f) for f in files]}
    landing_module._tell(put_back)
    assert "redo" not in put_back and [f["landing"] for f in put_back["files"]] == ["not_merged", "not_merged"]
    landing_module._tell(None)


async def test_a_projects_stream_carries_a_redo():
    assert EventType.HISTORY_REDO in STREAM_TYPES
