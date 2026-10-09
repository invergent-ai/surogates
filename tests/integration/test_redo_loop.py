"""Your edits picked up at each landing, and the redo of a thread's change that a newer file kept out."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import surogates.harness.loop as loop_module
import surogates.workstreams.history as rows_module
from surogates.channels.memory_boundary import PROJECT_BOUNDARY_PREFIX
from surogates.config import SHARED_WORK_QUEUE_KEY
from surogates.db.models import InboxItem, WorkstreamHistory
from surogates.harness import landing as landing_module
from surogates.harness import loop_artifact_completion
from surogates.harness.loop_context_replay import news, unread_reports, worker_note
from surogates.harness.loop_pending import NAMES_ANSWERS
from surogates.jobs.inbox_expire import expire_inbox_items
from surogates.harness.tool_exec import _build_session_sandbox_spec
from surogates.runtime import SlashCommandConfig
from surogates.sandbox.history import History
from surogates.sandbox.pool import SandboxPool, sandbox_session_key
from surogates.scheduled.schedule import parse_schedule
from surogates.scheduled.store import ScheduledSessionStore
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from surogates.session.store import SessionStore
from surogates.workstreams.stream import STREAM_TYPES, project_of
from tests.test_steer_loop import _final_response, _make_loop_harness

from .test_command_placements import Model, calls
from .test_command_wake_once import DiesWriting, workers  # noqa: F401  (workers is a fixture)
from .test_devices import api  # noqa: F401  (api is a fixture)
from .test_durable_landings import (  # noqa: F401  (a_short_fence is a fixture)
    a_landing_killed,
    a_short_fence,
    edited,
    ends,
    lose_the_lock,
    rows,
    rows_stand,
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
from .test_turn_sagas import a_turn, calling, stop
from .test_workstream_overview import rows as thread_rows
from .test_workstream_threads import call_tool, harness_of, queued
from .test_workstream_threads import turn_ends as a_turn_ends
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
    else:
        # A message no request had read has the turn: its skill is in it, once, and the redo after.
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


@pytest.mark.parametrize("typed", [A_SKILL, A_COMMAND], ids=["a skill", "a command"])
async def test_what_its_user_typed_while_the_clashing_turn_landed_is_taken_once_and_the_redo_read_once(workers, monkeypatch, pods, typed):
    api, store = workers.api, workers.store
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    workers.sandbox_pool = pool = SandboxPool(pods)
    expanded = skills_expanded(monkeypatch)
    await a_clash_while_its_user_types(workers, pods, pool, thread, typed)
    # Typed before the redo was written: it stands before it in the log, read by no request.
    assert [e.type for e in await store.get_events(thread.id)][-1] == EventType.HISTORY_REDO.value
    assert [e.data["content"] for e in await store.get_events(thread.id, types=[EventType.USER_MESSAGE])][-1] == typed
    for _ in range(3):
        await workers.nobody_is_queued()  # as a dispatcher takes each wake the one before queued
        await workers.wake(thread.id, SlashCommandConfig())
    [request] = workers.requests
    if typed == A_SKILL:
        # The message has the turn: its skill runs, once, and the redo is read after it, on its own.
        assert [m["content"] for m in request[-2:]] == [THE_SKILL, REDO_OF_THE_REPORT] and expanded == [A_SKILL]
    else:
        # The harness answers it, once and by name, and the redo has its turn after.
        assert [m["content"] for m in request[-3:]] == [A_COMMAND, REFUSED, REDO_OF_THE_REPORT] and expanded == []
        named = [e for e in await store.get_events(thread.id, types=[EventType.LLM_RESPONSE]) if "answers" in e.data]
        *_, said = await store.get_events(thread.id, types=[EventType.USER_MESSAGE])
        assert [(e.data["answers"], e.data["message"]["content"]) for e in named] == [(said.id, REFUSED)]
    assert [m["content"] for m in request].count(REDO_OF_THE_REPORT) == 1
    assert await workers.status(thread.id) == "completed" and not await queued(api, thread)


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
        "files": [{"ref": "notes.txt", "landing": "not_merged"}], "landed": [],
    }
    landing_module._tell(outcome, set())
    assert (outcome["redo"], outcome["saved"], outcome["files"][0]["landing"]) == ([], False, "not_merged")
    # Beside a file someone is named for, that one alone is redone, and the turn's end saved less than its work.
    outcome["overlapped"].append({"path": "Report.docx", "reason": "changed", "by": {"kind": "you"}})
    outcome["files"].append({"ref": "Report.docx", "landing": "not_merged"})
    landing_module._tell(outcome, set())
    assert outcome["redo"] == [{"path": "Report.docx", "reason": "changed", "by": {"kind": "you"}}]
    assert ([f["landing"] for f in outcome["files"]], outcome["saved"]) == (["not_merged", "redoing"], False)


async def test_a_redo_takes_the_files_held_with_a_clash_and_only_a_landing_that_completed_tells_of_one():
    held = [
        {"path": "Draft.docx", "reason": "changed", "by": {"kind": "thread", "id": "t2", "title": "Draft B"}},
        {"path": "Final.docx", "reason": "with"},
    ]
    files = [{"ref": "Draft.docx", "landing": "not_merged"}, {"ref": "Final.docx", "landing": "not_merged"}]
    # A move whose old name changed meanwhile: both names are redone, and the turn's end saved its work.
    outcome = {"state": "completed", "saved": False, "overlapped": held, "files": [dict(f) for f in files], "landed": []}
    landing_module._tell(outcome, set())
    assert outcome["redo"] == [
        {"path": "Draft.docx", "reason": "changed", "by": {"kind": "thread", "id": "t2", "title": "Draft B"}},
        {"path": "Final.docx", "reason": "with"},
    ]
    assert ([f["landing"] for f in outcome["files"]], outcome["saved"]) == (["redoing", "redoing"], True)
    # A landing put back for good applied nothing: its turn is on its branch, and lands whole with the next.
    put_back = {"state": "compensated", "saved": True, "overlapped": held, "files": [dict(f) for f in files]}
    landing_module._tell(put_back, set())
    assert "redo" not in put_back and [f["landing"] for f in put_back["files"]] == ["not_merged", "not_merged"]
    landing_module._tell(None, set())


async def test_a_projects_stream_carries_a_redo():
    assert EventType.HISTORY_REDO in STREAM_TYPES


async def test_a_projects_stream_carries_a_coordinators_follow_up():
    assert EventType.COORDINATOR_MESSAGE in STREAM_TYPES


async def test_a_follow_up_wake_reads_the_follow_up_and_runs_no_command_of_the_users_again(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await its_first_turn_was_taken(store, thread)
    command = "/report-writer Edit the report."
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": command})
    await store.emit_event(thread.id, EventType.SKILL_INVOKED, {"skill": "report-writer", "raw_message": command})
    await call_tool(api, master, "message_thread", thread_id=str(thread.id), message="Use the 2025 figures.")
    expanded: list = []

    async def a_skill(**kwargs):
        expanded.append(kwargs["text"])
        return "Follow the report-writer skill: edit the report.", "report-writer", None, "skill"

    monkeypatch.setattr(loop_module, "expand_slash_skill", a_skill)
    monkeypatch.setattr(loop_module, "expand_skill_again", a_skill)
    seen: list = []

    async def the_turn(session, messages, *_, **__):
        seen.append(messages[-1]["content"])

    harness = a_waking_thread_harness(api, monkeypatch, pool, the_turn)
    del harness._rebuild_messages  # the log replayed as it is
    await harness.wake(thread.id)
    # The follow-up is the turn's message: the command typed before it does not run again in its place.
    assert seen == ["[From the project's coordinator]\nUse the 2025 figures."]
    assert expanded == []


async def test_a_follow_up_whose_words_are_a_command_is_the_models_to_read_and_has_one_turn(workers, monkeypatch, pods):
    api, store = workers.api, workers.store
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    workers.sandbox_pool = SandboxPool(pods)
    await its_first_turn_was_taken(store, thread)
    await workers.nobody_is_queued()
    expanded: list = []

    async def a_skill(**kwargs):
        expanded.append(kwargs["text"])
        return "Follow the report-writer skill: tidy the notes.", "report-writer", None, "skill"

    monkeypatch.setattr(loop_module, "expand_slash_skill", a_skill)
    # As the log would hold one with no mark before its words: the coordinator's tool heads each with its mark.
    sent = ["/goal status", "/report-writer Tidy the notes."]
    for words in sent:
        await store.emit_event(thread.id, EventType.COORDINATOR_MESSAGE, {"content": words})
        await workers.wake(thread.id, SlashCommandConfig())
    # Each is the model's to read, as words: only the thread's user types a command or names a skill.
    assert [request[-1] for request in workers.requests] == [{"role": "user", "content": words} for words in sent]
    assert (workers.ran, expanded) == ([], [])
    assert [e for e in await store.get_events(thread.id, types=[EventType.LLM_RESPONSE]) if "answers" in e.data] == []
    # Read, each wakes nobody again.
    assert await workers.status(thread.id) == "completed" and not await queued(api, thread)
    await workers.wake(thread.id, SlashCommandConfig())
    assert len(workers.requests) == 2


async def waits_of(api, thread) -> list[InboxItem]:
    async with api.app.state.session_factory() as db:
        return list((await db.execute(select(InboxItem).where(InboxItem.session_id == thread.id))).scalars())


async def row_of(api, project, thread) -> dict:
    [row] = [r for r in await thread_rows(api, project) if r["id"] == str(thread.id)]
    return row


async def test_a_second_clash_puts_the_thread_in_waiting_on_you(api, monkeypatch, pods):
    project = await create(api)
    master = await master_of(api, project)
    thread = await a_thread(api, "Draft A", master)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v2 by you")
    # You save again before the redo lands.
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v3 by you")
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v3 by you"
    # Not told again: it waits on you.
    assert len(await store.get_events(thread.id, types=[EventType.HISTORY_REDO])) == 1
    [wait] = await waits_of(api, thread)
    assert (wait.kind, wait.title, wait.payload["target"], wait.status) == (
        "action_required", "Couldn't merge my changes to Report.docx", "Report.docx", "pending",
    )
    row = await row_of(api, project, thread)
    assert (row["group"], row["reason"], row["status_line"]) == ("waiting", "files", "Couldn't merge my changes to Report.docx")
    # Its version is in history, the landing's second parent.
    landing = git(pods.project / "_history", "rev-parse", "refs/heads/main")
    assert git(pods.project / "_history", "show", f"{landing}^2:Report.docx") == "PK\x03\x04 report v2 by you by A"
    # The thread's turn has ended, and the sweeper leaves its wait.
    await expire_inbox_items(store)
    assert (await row_of(api, project, thread))["reason"] == "files"


async def test_a_wait_over_a_second_clash_names_the_stuck_file_and_none_the_redo_turn_landed(api, monkeypatch, pods):
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    pool = SandboxPool(pods)
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v2 by you")
    await open_pod(pool, thread)
    (pods.project / "Report.docx").write_bytes(b"PK\x03\x04 report v3 by you")
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "printf ' by A' >> Report.docx && echo b > b.md"})),
        _final_response("Redid the edit, and wrote a note."),
    ], pool=pool)
    assert (pods.project / "b.md").read_text() == "b\n"
    [wait] = await waits_of(api, thread)
    assert (wait.title, wait.payload["target"]) == ("Couldn't merge my changes to Report.docx", "Report.docx")
    assert "b.md" not in wait.payload["instructions"]


async def test_a_wait_on_you_is_answered_by_your_next_message_not_by_a_wake(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v2 by you")
    await store.emit_event(thread.id, EventType.INBOX_ACTION_REQUIRED, landing_module.waiting_on_you(["Budget.xlsx"], escalated=False))

    async def a_turn_that_does_nothing(session, *_, **__):
        pass

    # The redo of another file wakes it: no answer of yours.
    await a_waking_thread_harness(api, monkeypatch, pool, a_turn_that_does_nothing).wake(thread.id)
    [wait] = await waits_of(api, thread)
    assert wait.status == "pending"
    # Nor is the coordinator's follow-up: it is the coordinator's, never the user's.
    await call_tool(api, master, "message_thread", thread_id=str(thread.id), message="Finish the report.")
    await a_waking_thread_harness(api, monkeypatch, pool, a_turn_that_does_nothing).wake(thread.id)
    [wait] = await waits_of(api, thread)
    assert wait.status == "pending"
    # Nor is a message the harness wrote for itself.
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Continue.", "synthetic": "nudge"})
    await a_waking_thread_harness(api, monkeypatch, pool, a_turn_that_does_nothing).wake(thread.id)
    [wait] = await waits_of(api, thread)
    assert wait.status == "pending"
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Keep my version, and add yours below it."})
    emit, at_wake = store.emit_event, []

    async def watched(session_id, event_type, data, *args, **kwargs):
        if event_type == EventType.HARNESS_WAKE:
            # What the project's clients read as the wake is streamed.
            at_wake.append([w.status for w in await waits_of(api, thread)])
        return await emit(session_id, event_type, data, *args, **kwargs)

    monkeypatch.setattr(store, "emit_event", watched)
    await a_waking_thread_harness(api, monkeypatch, pool, a_turn_that_does_nothing).wake(thread.id)
    [wait] = await waits_of(api, thread)
    assert wait.status == "responded" and at_wake == [["responded"]]


async def test_a_redo_turn_that_calls_no_tool_marks_the_file_not_merged_and_nothing_waits(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v2 by you")
    await a_turn(api, monkeypatch, thread, [_final_response("Your version of the report stays as it is.")], pool=pool)
    # It never used its pod, so it lands nothing; the master hears the file was left as it is.
    _, second = await reports(api, master)
    assert [(f["ref"], f["landing"], f.get("reason")) for f in second["files"]] == [("Report.docx", "not_merged", "left")]
    assert worker_note(EventType.WORKER_COMPLETE.value, second)["content"].endswith(
        "Not merged, because the thread left the newer file as it is: Report.docx"
    )
    assert await waits_of(api, thread) == []


async def test_a_redo_turn_that_leaves_the_file_alone_marks_it_not_merged_and_nothing_waits(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v2 by you")
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo more > b.md"})), _final_response("Kept your version of the report."),
    ], pool=pool)
    _, second = await reports(api, master)
    assert {f["ref"]: (f["landing"], f.get("reason")) for f in second["files"]} == {
        "b.md": ("landed", None), "Report.docx": ("not_merged", "left"),
    }
    assert await waits_of(api, thread) == []


async def test_a_landing_that_escalates_puts_its_thread_in_waiting_on_you(api, monkeypatch, pods):
    project = await create(api)
    thread = await a_thread(api, "Draft A", await master_of(api, project))
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md && echo b > b.md")
    call = landing_module._call

    async def your_save_lands_then_b_fails(sandbox_pool, owner, action, **arguments):
        if action == "apply" and arguments.get("path") == "b.md":
            (pods.project / "a.md").write_text("saved by you over the half landing")  # a.md cannot be put back
            raise landing_module.LandingStepError("the pod's step timed out")
        return await call(sandbox_pool, owner, action, **arguments)

    monkeypatch.setattr(landing_module, "_call", your_save_lands_then_b_fails)
    await ends(api, pool, thread)
    [row] = await rows(api, thread)
    assert row.saga_state == "escalated"
    [wait] = await waits_of(api, thread)
    assert (wait.title, wait.payload["action_type"]) == ("Couldn't finish landing my changes", "files")
    assert (await row_of(api, project, thread))["reason"] == "files"


async def test_a_landing_settled_as_escalated_puts_its_own_thread_in_waiting_on_you(api, monkeypatch, pods):
    project = await create(api)
    master = await master_of(api, project)
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(pods)
    # Its row written at every try, as a slow landing's is: a.md's apply is known to have run.
    rows_stand(monkeypatch, "exact")
    await edited(pool, first, "for f in a b c; do echo $f > $f.md; done")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply b.md")
    (pods.project / "a.md").write_text("saved by you over the half landing")  # its put-back finds it changed
    await edited(pool, second, "echo by B > B.md")
    await ends(api, pool, second)
    [killed] = await rows(api, first)
    assert killed.saga_state == "escalated"
    [wait] = await waits_of(api, first)
    assert wait.title == "Couldn't finish landing my changes"
    assert (await row_of(api, project, first))["reason"] == "files"


async def test_a_redo_turns_files_are_sorted_each_from_its_own_clash():
    you = {"kind": "you"}

    def landed_as(held: list[dict], *landed: str) -> dict:
        return {
            "state": "completed", "saved": False, "overlapped": held, "landed": [{"path": path} for path in landed],
            "files": [{"ref": o["path"], "landing": "not_merged"} for o in held] + [{"ref": path, "landing": "landed"} for path in landed],
        }

    # Woken for the report and the summary.  The report is held again, and the name it was moved to with it;
    # the budget clashes for the first time; the summary the turn left alone, and the notes it landed.
    outcome = landed_as([
        {"path": "Report.docx", "reason": "changed", "by": you}, {"path": "Final.docx", "reason": "with"},
        {"path": "Budget.xlsx", "reason": "changed", "by": you},
    ], "Notes.md")
    landing_module._tell(outcome, {"Report.docx", "Final.docx", "Summary.md", "Notes.md"})
    assert outcome["stuck"] == ["Final.docx", "Report.docx"]
    assert outcome["redo"] == [{"path": "Budget.xlsx", "reason": "changed", "by": you}]
    assert {f["ref"]: (f["landing"], f.get("reason")) for f in outcome["files"]} == {
        "Report.docx": ("not_merged", None), "Final.docx": ("not_merged", None), "Budget.xlsx": ("redoing", None),
        "Notes.md": ("landed", None), "Summary.md": ("not_merged", "left"),
    }
    # Its copy goes with files of its own that are being redone by no one.
    assert outcome["saved"] is False
    # A file held only with a stuck one is not redone.
    outcome = landed_as([{"path": "Report.docx", "reason": "changed", "by": you}, {"path": "Final.docx", "reason": "with"}])
    landing_module._tell(outcome, {"Report.docx"})
    assert (outcome["stuck"], outcome["redo"], outcome["saved"]) == (["Report.docx"], [], False)
    # One redone and held again only with a new clash is not stuck, and the new clash alone is redone.
    outcome = landed_as([{"path": "Budget.xlsx", "reason": "changed", "by": you}, {"path": "Final.docx", "reason": "with"}])
    landing_module._tell(outcome, {"Final.docx"})
    assert (outcome["stuck"], [f["path"] for f in outcome["redo"]], outcome["saved"]) == ([], ["Budget.xlsx"], False)
    # A file redone and held again with no one's change behind it waits on nobody, and is not redone again.
    outcome = landed_as([{"path": "Plans", "reason": "shape"}])
    landing_module._tell(outcome, {"Plans"})
    assert (outcome["stuck"], outcome["redo"], [f.get("reason") for f in outcome["files"]]) == ([], [], [None])


async def test_a_wait_on_you_names_its_first_file_and_counts_the_others():
    def title_and_target(*paths: str, escalated: bool = False) -> tuple[str, str]:
        wait = landing_module.waiting_on_you(list(paths), escalated=escalated)
        assert (wait["action_type"], wait["reason"]) == ("files", "files")
        return wait["title"], wait["target"]

    assert title_and_target("Report.docx") == ("Couldn't merge my changes to Report.docx", "Report.docx")
    assert title_and_target("A.md", "B.md") == ("Couldn't merge my changes to A.md and 1 other file", "A.md")
    assert title_and_target("A.md", "B.md", "C.md") == ("Couldn't merge my changes to A.md and 2 other files", "A.md")
    assert title_and_target("A.md", "B.md", escalated=True) == ("Couldn't finish landing my changes", "A.md")
    # A landing that knew none of its files still waits, on the thread itself.
    assert title_and_target(escalated=True) == ("Couldn't finish landing my changes", "session")
    many = landing_module.waiting_on_you([f"{n}.md" for n in range(23)], escalated=False)["instructions"]
    assert "19.md and 3 more changed again" in many and "20.md" not in many


REQUEST, COMPLETE = EventType.LLM_REQUEST, EventType.SESSION_COMPLETE


@pytest.mark.parametrize("since, the_redos", [
    ((), True),
    ((REQUEST,), True),
    ((REQUEST, COMPLETE), False),
    ((REQUEST, EventType.SESSION_FAIL), False),
    ((REQUEST, EventType.SESSION_PAUSE), False),
    ((REQUEST, EventType.SESSION_STOPPED), False),
    ((EventType.SESSION_FAIL,), True),
    ((EventType.SESSION_FAIL, REQUEST), True),
    ((EventType.SESSION_FAIL, REQUEST, COMPLETE), False),
    ((REQUEST, COMPLETE, REQUEST), False),
], ids=[
    "told", "its turn under way", "its turn landed", "its turn failed", "its turn stopped", "its turn stopped by the route",
    "a turn the limit refused, which read nothing", "the turn after that one", "that turn landed", "the turn after the redo's",
])
async def test_a_turn_is_the_redos_from_its_first_request_until_it_ends(api, since, the_redos):
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    store = api.app.state.session_store
    assert await landing_module.redo_files(store, thread.id) == set()
    # The turn whose landing clashed.
    await store.emit_event(thread.id, REQUEST, {})
    await store.emit_event(thread.id, COMPLETE, {"reason": "completed"})
    await store.emit_event(thread.id, EventType.HISTORY_REDO, {"saga": "saga:1", "files": [{"path": "Report.docx", "reason": "changed"}]})
    for kind in since:
        await store.emit_event(thread.id, kind, {})
    assert await landing_module.redo_files(store, thread.id) == ({"Report.docx"} if the_redos else set())


async def test_your_message_answers_the_waits_over_files_before_it_and_no_other(api):
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    store = api.app.state.session_store
    approval = {"title": "Send it?", "instructions": "", "context": "", "action_type": "approval", "target": "the memo"}
    await store.emit_event(thread.id, EventType.INBOX_ACTION_REQUIRED, approval)
    await store.emit_event(thread.id, EventType.INBOX_ACTION_REQUIRED, landing_module.waiting_on_you(["Report.docx"], escalated=False))
    said = await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Keep mine."})
    # Raised after your message: not what it answered.
    await store.emit_event(thread.id, EventType.INBOX_ACTION_REQUIRED, landing_module.waiting_on_you(["Budget.xlsx"], escalated=True))
    assert await store.answer_file_waits(thread.id, before=said) == 1
    # Once: a wait answered is not answered again by the next wake.
    assert await store.answer_file_waits(thread.id, before=said) == 0
    assert {wait.title: wait.status for wait in await waits_of(api, thread)} == {
        "Send it?": "pending", "Couldn't merge my changes to Report.docx": "responded",
        "Couldn't finish landing my changes": "pending",
    }
    # The sweeper leaves a finished thread's waits over files alone, and expires its other items as before.
    await store.update_session_status(thread.id, "completed")
    await expire_inbox_items(store)
    assert {wait.title: wait.status for wait in await waits_of(api, thread)} == {
        "Send it?": "expired", "Couldn't merge my changes to Report.docx": "responded",
        "Couldn't finish landing my changes": "pending",
    }


async def a_thread_at_rest(workers, pods):
    """A thread of a new project whose first turn has ended, nobody queued; its master with it."""
    api, store = workers.api, workers.store
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    workers.sandbox_pool = SandboxPool(pods)
    await its_first_turn_was_taken(store, thread)
    await a_turn_ends(api, thread)
    await workers.nobody_is_queued()
    return thread, master


async def test_a_wait_answered_by_the_inboxs_own_button_gives_the_thread_one_turn(workers, monkeypatch, pods):
    api, store = workers.api, workers.store
    thread, _ = await a_thread_at_rest(workers, pods)
    await store.emit_event(thread.id, EventType.INBOX_ACTION_REQUIRED, landing_module.waiting_on_you(["Report.docx"], escalated=False))
    [wait] = await waits_of(api, thread)
    done = await api.client.post(f"/v1/inbox/{wait.id}/respond", json={"completed": True}, headers=api.auth())
    assert done.status_code == 200, done.text
    await workers.wake(thread.id, SlashCommandConfig())
    # The button's message is the user's answer: one turn reads it, and the thread rests.
    [request] = workers.requests
    assert any(str(m.get("content")).startswith("[user action completed] files.") for m in request if m["role"] == "user")
    assert await workers.status(thread.id) == "completed" and [w.status for w in await waits_of(api, thread)] == ["responded"]
    await workers.says(thread.id, "And put a date on it.")
    await workers.wake(thread.id, SlashCommandConfig())
    assert len(workers.requests) == 2 and await workers.status(thread.id) == "completed"


def answers_of(monkeypatch) -> list[int]:
    """How many waits each look for an answer retired, from here on."""
    retired: list[int] = []
    answer = SessionStore.answer_file_waits

    async def counted(self, session_id, *, before):
        retired.append(await answer(self, session_id, before=before))
        return retired[-1]

    monkeypatch.setattr(SessionStore, "answer_file_waits", counted)
    return retired


async def settles(workers, thread) -> None:
    """The thread is woken while it is queued, as its dispatcher wakes it."""
    for _ in range(10):
        if not await queued(workers.api, thread):
            return
        await workers.api.app.state.redis.delete(SHARED_WORK_QUEUE_KEY)
        await workers.wake(thread.id, SlashCommandConfig())
    raise AssertionError("the thread is woken again and again")


async def test_an_answer_typed_while_the_thread_works_ends_the_wait(workers, monkeypatch, pods):
    api, store = workers.api, workers.store
    project = await create(api)
    master = await master_of(api, project)
    thread = await a_thread(api, "Draft A", master)
    workers.sandbox_pool = SandboxPool(pods)
    await its_first_turn_was_taken(store, thread)
    await a_turn_ends(api, thread)
    await workers.nobody_is_queued()
    model = Model()
    monkeypatch.setattr(loop_module, "call_llm_with_retry", model)
    retired = answers_of(monkeypatch)
    await store.emit_event(thread.id, EventType.INBOX_ACTION_REQUIRED, landing_module.waiting_on_you(["Report.docx"], escalated=False))
    # Its coordinator's follow-up starts a turn, which is no answer of yours.
    await call_tool(api, master, "message_thread", thread_id=str(thread.id), message="Finish the report.")
    model.script.append(calls("call_1"))

    async def you_answer() -> None:
        assert [w.status for w in await waits_of(api, thread)] == ["pending"]
        await workers.says(thread.id, "Keep my version of Report.docx.")

    workers.during_the_tool_call = you_answer
    await settles(workers, thread)
    # The turn under way read your answer, and no wake of its own follows: the wait is over all the same.
    assert ["Keep my version" in str(request[-1].get("content")) for request in model.requests] == [False, True]
    assert [w.status for w in await waits_of(api, thread)] == ["responded"]
    row = await row_of(api, project, thread)
    assert (row["group"], row["reason"]) == ("idle", None)
    assert sum(retired) == 1 and await workers.status(thread.id) == "completed"


LEFT_OUT_AGAIN = (
    "[Your changes to these files were left out again, since each changed once more while you redid it:\n"
    "- Report.docx\n"
    "The newer file was kept, and your version is in the file's history, not in the file. "
    "The user was asked what to do about it: do not put your change in again unless they tell you to.]"
)


async def test_a_thread_reads_its_wait_over_files_as_news_and_no_other_item():
    def raised(**payload) -> dict | None:
        return news(SimpleNamespace(type=EventType.INBOX_ACTION_REQUIRED.value, data=payload))

    assert raised(**landing_module.waiting_on_you(["Report.docx"], escalated=False)) == {"role": "user", "content": LEFT_OUT_AGAIN}
    assert raised(**landing_module.waiting_on_you(["a.md", "b\n.md"], escalated=True)) == {"role": "user", "content": (
        "[A landing of your changes to these files could not be finished, nor put back whole:\n"
        "- a.md\n- b .md\n"
        "Each may hold part of your change in the project. The user was asked to check them: "
        "read a file again before you change it.]"
    )}
    # An item that asks its user for anything else is theirs alone.
    assert raised(title="Log in", instructions="", context="", action_type="browser", target="site") is None


async def a_waiting_thread(workers, pods):
    """``a_thread_at_rest`` that waits on you over its report, and the wait's event."""
    thread, master = await a_thread_at_rest(workers, pods)
    wait = await workers.store.emit_event(
        thread.id, EventType.INBOX_ACTION_REQUIRED, landing_module.waiting_on_you(["Report.docx"], escalated=False),
    )
    return thread, master, wait


def notes_in(request: list[dict]) -> int:
    return sum(1 for message in request if message.get("content") == LEFT_OUT_AGAIN)


async def replayed_as_asked(workers, thread, requests: list[list[dict]]) -> bool:
    """Whether replay rebuilds each of *requests*, the thread's last ones, as it was sent."""
    events = await workers.store.get_events(thread.id)
    asked = [i for i, e in enumerate(events) if e.type == EventType.LLM_REQUEST.value][-len(requests):]
    return [workers.worker()._rebuild_messages(events[:at + 1]) for at in asked] == requests


async def test_the_turn_on_your_answer_to_a_wait_reads_what_the_thread_waits_over(workers, monkeypatch, pods):
    api = workers.api
    thread, _, _ = await a_waiting_thread(workers, pods)
    # Nobody is woken for it: it is the user's to answer.
    assert not await queued(api, thread)
    await workers.wake(thread.id, SlashCommandConfig())
    assert workers.requests == []
    await workers.says(thread.id, "Use yours.")
    await workers.wake(thread.id, SlashCommandConfig())
    [request] = workers.requests
    # The model that answers knows its change is not in the file, and that its user was asked.
    assert [m["content"] for m in request[-2:]] == ["Use yours.", LEFT_OUT_AGAIN]
    assert await replayed_as_asked(workers, thread, [request])
    # Read once: the next turn has it in its place, and not again.
    await workers.says(thread.id, "Thanks.")
    await workers.wake(thread.id, SlashCommandConfig())
    assert [notes_in(r) for r in workers.requests] == [1, 1] and workers.requests[1][-1]["content"] == "Thanks."
    assert await workers.status(thread.id) == "completed" and not await queued(api, thread)


async def test_an_answer_steered_into_a_turn_finds_the_wait_already_read_by_that_turn(workers, monkeypatch, pods):
    api = workers.api
    thread, master, _ = await a_waiting_thread(workers, pods)
    model = Model()
    monkeypatch.setattr(loop_module, "call_llm_with_retry", model)
    await call_tool(api, master, "message_thread", thread_id=str(thread.id), message="Finish the report.")
    model.script.append(calls("call_1"))

    async def you_answer() -> None:
        await workers.says(thread.id, "Keep my version of Report.docx.")

    workers.during_the_tool_call = you_answer
    await settles(workers, thread)
    first, second = model.requests
    # The follow-up's turn reads the wait at its first request; your answer joins it after the call's result.
    assert [m["content"] for m in first[-2:]] == ["[From the project's coordinator]\nFinish the report.", LEFT_OUT_AGAIN]
    assert (second[-2]["role"], second[-1]["content"]) == ("tool", "Keep my version of Report.docx.")
    assert [notes_in(r) for r in model.requests] == [1, 1]
    assert await replayed_as_asked(workers, thread, [first, second])
    assert await workers.status(thread.id) == "completed" and not await queued(api, thread)


async def test_a_command_typed_before_your_answer_is_answered_alone_and_the_wait_is_read_with_the_answer(workers, monkeypatch, pods):
    api = workers.api
    thread, _, _ = await a_waiting_thread(workers, pods)
    assert await workers.types(thread.id, A_COMMAND, SlashCommandConfig()) == REFUSED
    # The command is the harness's to answer: no request, and the thread rests with the wait's news unread.
    assert workers.requests == [] and await workers.status(thread.id) == "completed" and not await queued(api, thread)
    await workers.says(thread.id, "Use yours.")
    await workers.wake(thread.id, SlashCommandConfig())
    [request] = workers.requests
    assert [m["content"] for m in request[-4:]] == [A_COMMAND, REFUSED, "Use yours.", LEFT_OUT_AGAIN]
    assert await replayed_as_asked(workers, thread, [request])
    assert (await workers.said(thread.id)).count(REFUSED) == 1
    await workers.wake(thread.id, SlashCommandConfig())
    assert len(workers.requests) == 1


async def test_a_wait_written_while_its_thread_works_is_read_by_the_turn_under_way(workers, monkeypatch, pods):
    api, store = workers.api, workers.store
    thread, _ = await a_thread_at_rest(workers, pods)
    model = Model()
    monkeypatch.setattr(loop_module, "call_llm_with_retry", model)
    model.script.append(calls("call_1"))

    async def another_threads_settle_escalates_its_landing() -> None:
        await store.emit_event(thread.id, EventType.INBOX_ACTION_REQUIRED, landing_module.waiting_on_you(["a.md"], escalated=True))

    workers.during_the_tool_call = another_threads_settle_escalates_its_landing
    await workers.says(thread.id, "Go on.")
    await workers.wake(thread.id, SlashCommandConfig())
    first, second = model.requests
    note = news(SimpleNamespace(type=EventType.INBOX_ACTION_REQUIRED.value, data=landing_module.waiting_on_you(["a.md"], escalated=True)))
    assert note not in first and (second[-2]["role"], second[-1]) == ("tool", note)
    assert await replayed_as_asked(workers, thread, [first, second])


async def test_a_wait_over_a_file_that_has_since_landed_is_over(api, monkeypatch, pods):
    project = await create(api)
    master = await master_of(api, project)
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v2 by you")
    await a_clash(api, monkeypatch, pods, pool, thread, b"PK\x03\x04 report v3 by you")
    assert (await row_of(api, project, thread))["reason"] == "files"
    # Its coordinator reads the file was left out and follows up; the thread puts its change in again, and it lands.
    await call_tool(api, master, "message_thread", thread_id=str(thread.id), message="Put your change in again.")
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "printf ' by A' >> Report.docx"})), _final_response("Put it in again."),
    ], pool=pool)
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v3 by you by A"
    [wait] = await waits_of(api, thread)
    assert wait.status == "expired"
    row = await row_of(api, project, thread)
    assert (row["group"], row["reason"]) == ("idle", None)


async def test_a_wait_is_over_once_the_last_of_its_files_has_landed_and_an_escalations_stays(api):
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    store = api.app.state.session_store
    await store.emit_event(thread.id, EventType.INBOX_ACTION_REQUIRED, landing_module.waiting_on_you(["A.md", "B.md"], escalated=False))
    await store.emit_event(thread.id, EventType.INBOX_ACTION_REQUIRED, landing_module.waiting_on_you(["A.md"], escalated=True))

    async def stand() -> list[tuple[str, list[str]]]:
        return [(wait.status, wait.payload["files"]) for wait in sorted(await waits_of(api, thread), key=lambda w: w.id)]

    assert await store.land_file_waits(thread.id, {"C.md"}) == 0
    assert await stand() == [("pending", ["A.md", "B.md"]), ("pending", ["A.md"])]
    # One of its two: it waits over the other still.  A landing that could not be put back is the user's to check.
    assert await store.land_file_waits(thread.id, {"A.md"}) == 0
    assert await stand() == [("pending", ["B.md"]), ("pending", ["A.md"])]
    assert await store.land_file_waits(thread.id, {"B.md", "C.md"}) == 1
    assert await stand() == [("expired", ["B.md"]), ("pending", ["A.md"])]


async def a_routine_run(api, master, name: str):
    """A run of a routine of *master*'s, due in a year: no other test's claim of due runs finds it."""
    schedule = await ScheduledSessionStore(api.app.state.session_factory).create(
        org_id=master.org_id, user_id=master.user_id, agent_id=master.agent_id, name=name,
        prompt="Check the notes.", schedule=parse_schedule("1h"), source="tool", created_from_session_id=master.id,
        next_run_at=datetime.now(timezone.utc) + timedelta(days=365),
    )
    run = await create_child_session(
        store=api.app.state.session_store, parent=master, channel="scheduled",
        config={"scheduled_session_id": str(schedule.id)},
    )
    return run, schedule


async def in_its_pod(api, pool, run, command: str) -> None:
    """*command* run in the master's pod, which its routine runs work in, as a call of *run*'s turn."""
    await api.app.state.session_store.emit_event(
        run.id, EventType.TOOL_CALL, {"tool_call_id": "call_0_terminal", "name": "terminal", "arguments": {"command": command}},
    )
    await open_pod(pool, run)
    await pool.execute(sandbox_session_key(run), "terminal", json.dumps({"command": command}))


async def a_history(api, pool, master) -> None:
    """The project's history, made by a first thread's landing."""
    starter = await a_thread(api, "Starter", master)
    await edited(pool, starter, "echo s > start.md")
    await ends(api, pool, starter)


async def pickups(api, commit: str) -> list[WorkstreamHistory]:
    async with api.app.state.session_factory() as db:
        return list((await db.execute(select(WorkstreamHistory).where(
            WorkstreamHistory.kind == "pickup", WorkstreamHistory.commit == commit,
        ))).scalars())


async def pickups_of(api, master) -> list[WorkstreamHistory]:
    """Every pickup recorded alone in *master*'s project, oldest first."""
    async with api.app.state.session_factory() as db:
        return list((await db.execute(select(WorkstreamHistory).where(
            WorkstreamHistory.kind == "pickup", WorkstreamHistory.workstream_id == project_of(master.config["workspace_boundary"]),
        ).order_by(WorkstreamHistory.id))).scalars())


def main_of(pods) -> str:
    return git(pods.project / "_history", "rev-parse", "refs/heads/main")


def changed(row: WorkstreamHistory) -> list[str]:
    return [f["path"] for f in row.picked_up]


async def test_a_routine_runs_changes_are_picked_up_as_the_routines(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await open_pod(pool, thread)  # A's copy, before the routine runs
    await a_history(api, pool, master)
    run, schedule = await a_routine_run(api, master, "Health check")
    await a_turn(api, monkeypatch, run, [
        calling(("terminal", {"command": "printf ' checked' >> notes.txt"})), _final_response("Checked."),
    ], pool=pool)
    durable = pods.project / "_history"
    main = git(durable, "rev-parse", "refs/heads/main")
    assert git(durable, "log", "-1", "--format=%an <%ae>", main) == f"Health check <routine:{schedule.id}@surogate>"
    [row] = await pickups(api, main)
    assert ([f["path"] for f in row.picked_up], row.saga_state) == (["notes.txt"], "completed")
    assert row.steps[0]["arguments"]["author"]["name"] == "Health check"
    assert git(durable, "log", "-1", "--format=%(trailers:only,unfold)", main).splitlines() == [
        f"Surogate-Project: {project_of(master.config['workspace_boundary'])}", f"Surogate-Agent: {run.agent_id}",
        f"Surogate-User: {run.user_id}", f"Surogate-Saga: {row.saga_id}", "Surogate-Kind: pickup",
    ]
    # A thread whose copy predates it is told the routine changed the file.
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "printf ' by A' >> notes.txt"})), _final_response("Edited."),
    ], pool=pool)
    [redo] = await store.get_events(thread.id, types=[EventType.HISTORY_REDO])
    assert redo.data["files"][0]["by"] == {"kind": "routine", "name": "Health check"}


async def test_a_failed_routine_runs_changes_are_the_routines_too(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    pool = SandboxPool(pods)
    await a_history(api, pool, master)
    run, _ = await a_routine_run(api, master, "Tidy up")
    await in_its_pod(api, pool, run, "echo tidied > notes.txt")
    await ends(api, pool, run, failed=True)
    assert git(pods.project / "_history", "log", "-1", "--format=%an", "refs/heads/main") == "Tidy up"


async def test_a_routine_runs_pickup_first_settles_a_landing_a_killed_worker_left(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await a_history(api, pool, master)
    await edited(pool, thread, "for f in a b c; do echo $f > $f.md; done")
    await a_landing_killed(api, monkeypatch, pool, thread, after="apply b.md")
    assert pods.real_names() == ["Report.docx", "a.md", "b.md", "notes.txt", "start.md"]
    run, _ = await a_routine_run(api, master, "Tidy up")
    await in_its_pod(api, pool, run, "echo tidied > notes.txt")
    await ends(api, pool, run)
    # Put back through the master's pod, which has no copy: the half landing is no change of the routine's.
    [killed] = await rows(api, thread)
    assert killed.saga_state == "compensated"
    assert pods.real_names() == ["Report.docx", "notes.txt", "start.md"]
    [row] = await pickups(api, git(pods.project / "_history", "rev-parse", "refs/heads/main"))
    assert [f["path"] for f in row.picked_up] == ["notes.txt"]


async def test_a_routine_run_of_a_project_over_the_cap_records_nothing(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    pods = stored(api, master, tmp_path)
    pool = SandboxPool(pods)
    await a_history(api, pool, master)
    before = git(pods.project / "_history", "rev-parse", "refs/heads/main")
    run, _ = await a_routine_run(api, master, "Tidy up")
    await api.app.state.session_store.emit_event(run.id, EventType.USER_MESSAGE, {"content": "Check the notes."})
    woken: list = []

    async def its_turn(session, *_, **__):
        woken.append(session)

    harness = a_waking_thread_harness(api, monkeypatch, pool, its_turn)
    harness._storage = api.app.state.storage
    monkeypatch.setattr(rows_module, "HISTORY_CAP", 2)  # Report.docx, notes.txt and start.md are one too many
    monkeypatch.setattr(rows_module, "_COUNTED", {})
    await harness.wake(run.id)
    # Its wake marks it, as a thread's is marked: its end records nothing.
    [over] = woken
    assert over.config["history_off"] is True
    await in_its_pod(api, pool, over, "true")
    (pods.project / "notes.txt").write_text("tidied\n")  # as a call of its turn wrote it
    await ends(api, pool, over)
    assert await pickups_of(api, master) == [] and main_of(pods) == before


async def test_a_cloud_session_that_is_no_projects_wakes_with_its_bucket(api, monkeypatch, pods):
    created = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    assert created.status_code == 201, created.text
    store = api.app.state.session_store
    session = await store.get_session(created.json()["id"])
    assert session.config.get("storage_bucket") and "workspace_boundary" not in session.config
    await store.emit_event(session.id, EventType.USER_MESSAGE, {"content": "Hello."})
    ran: list = []

    async def its_turn(woken, *_, **__):
        ran.append(woken.config)

    harness = a_waking_thread_harness(api, monkeypatch, SandboxPool(pods), its_turn)
    harness._storage = api.app.state.storage  # as a worker has it
    await harness.wake(session.id)
    # Its turn runs, unmarked: no project's history is counted for it.
    assert len(ran) == 1 and "history_off" not in ran[0]
    assert await store.get_events(session.id, types=[EventType.HARNESS_CRASH]) == []


async def test_your_edit_before_a_routine_run_is_recorded_by_you_and_not_as_the_routines(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    pool = SandboxPool(pods)
    await a_history(api, pool, master)
    # Neither a run that has ended nor one that has called no tool yet is at work.
    earlier, _ = await a_routine_run(api, master, "Tidy up")
    await a_turn(api, monkeypatch, earlier, [calling(("terminal", {"command": "cat notes.txt"})), _final_response("Fine.")], pool=pool)
    await a_routine_run(api, master, "Not started")
    # Nor is another project's run this project's.
    elsewhere, _ = await a_routine_run(api, await master_of(api, await create(api)), "Elsewhere")
    await api.app.state.session_store.emit_event(
        elsewhere.id, EventType.TOOL_CALL, {"tool_call_id": "call_0_terminal", "name": "terminal", "arguments": {}},
    )
    (pods.project / "brief.pdf").write_bytes(b"%PDF uploaded before the routine ran")
    run, schedule = await a_routine_run(api, master, "Health check")
    await a_turn(api, monkeypatch, run, [
        calling(("terminal", {"command": "printf ' checked' >> notes.txt"})), _final_response("Checked."),
    ], pool=pool)
    durable = pods.project / "_history"
    # Two pickups: yours as the run began, before its first call, and then the run's own.
    yours, its = await pickups_of(api, master)
    assert (changed(yours), changed(its), its.commit) == (["brief.pdf"], ["notes.txt"], main_of(pods))
    assert git(durable, "log", "-1", "--format=%ae|%P", its.commit) == f"routine:{schedule.id}@surogate|{yours.commit}"
    assert git(durable, "log", "-1", "--format=%an <%ae>", yours.commit) == f"{run.user_id} <user:{run.user_id}@surogate>"
    assert git(durable, "log", "-1", "--format=%(trailers:key=Surogate-Kind,valueonly)", yours.commit) == "pickup"
    assert yours.steps[0]["arguments"]["author"]["email"] == f"user:{run.user_id}@surogate"


async def test_what_you_save_while_a_routine_run_is_at_work_is_recorded_with_the_runs_changes_and_never_lost(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    pool = SandboxPool(pods)
    await a_history(api, pool, master)
    run, _ = await a_routine_run(api, master, "Health check")

    async def you_save(harness) -> None:
        (pods.project / "brief.pdf").write_bytes(b"%PDF uploaded while the routine ran")

    await a_turn(api, monkeypatch, run, [
        calling(("terminal", {"command": "printf ' checked' >> notes.txt"})), calling(("memory", {"action": "add", "content": "x"})),
        _final_response("Checked."),
    ], pool=pool, during=you_save)
    # The real files are one folder, the run's and yours: what changed between its first call and its end is its pickup's.
    [its] = await pickups_of(api, master)
    assert changed(its) == ["brief.pdf", "notes.txt"]
    assert (pods.project / "brief.pdf").read_bytes() == b"%PDF uploaded while the routine ran"
    assert git(pods.project / "_history", "show", f"{its.commit}:brief.pdf") == "%PDF uploaded while the routine ran"


async def test_a_routine_run_that_starts_while_another_is_at_work_takes_none_of_its_changes_for_yours(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    pool = SandboxPool(pods)
    await a_history(api, pool, master)
    first, _ = await a_routine_run(api, master, "Tidy up")
    second, schedule = await a_routine_run(api, master, "Health check")
    await in_its_pod(api, pool, first, "echo a > a.md")
    await a_turn(api, monkeypatch, second, [
        calling(("terminal", {"command": "echo b > b.md"})), _final_response("Checked."),
    ], pool=pool)
    # What the first had written is a routine's, the one that ended first: never yours.
    [its] = await pickups_of(api, master)
    assert (changed(its), its.commit) == (["a.md", "b.md"], main_of(pods))
    assert git(pods.project / "_history", "log", "--format=%ae", "-2", its.commit).splitlines()[0] == f"routine:{schedule.id}@surogate"
    await ends(api, pool, first)
    # And recorded once: the first's own end finds nothing left.
    assert [r.id for r in await pickups_of(api, master)] == [its.id] and main_of(pods) == its.commit
    git(pods.project / "_history", "fsck", "--strict", "--no-dangling")


async def test_a_routine_runs_pickup_waits_for_a_landing_under_way_and_takes_none_of_its_files(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await a_history(api, pool, master)
    await edited(pool, thread, "for f in a b; do echo $f > $f.md; done")
    run, schedule = await a_routine_run(api, master, "Tidy up")
    before = main_of(pods)
    call = landing_module._call
    applying, go_on = asyncio.Event(), asyncio.Event()

    async def held_up(sandbox_pool, owner, action, **arguments):
        result = await call(sandbox_pool, owner, action, **arguments)
        if owner == str(thread.id) and (action, arguments.get("path")) == ("apply", "a.md"):
            applying.set()
            await go_on.wait()
        return result

    monkeypatch.setattr(landing_module, "_call", held_up)
    landing = asyncio.ensure_future(ends(api, pool, thread))
    await applying.wait()
    # The landing has a.md in the real files and not b.md, and holds the project's lock.
    await in_its_pod(api, pool, run, "echo tidied > notes.txt")
    routine = asyncio.ensure_future(ends(api, pool, run))
    await asyncio.sleep(1.5)
    assert not routine.done() and main_of(pods) == before and await pickups_of(api, master) == []
    go_on.set()
    await asyncio.gather(landing, routine)
    [landed] = await rows(api, thread)
    [its] = await pickups_of(api, master)
    durable = pods.project / "_history"
    assert (landed.saga_state, changed(its), its.commit) == ("completed", ["notes.txt"], main_of(pods))
    assert git(durable, "log", "-1", "--format=%ae|%P", its.commit) == f"routine:{schedule.id}@surogate|{landed.commit}"
    git(durable, "fsck", "--strict", "--no-dangling")


async def test_a_routine_run_taken_up_by_another_worker_records_its_changes_as_the_routines_all_the_same(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    pool = SandboxPool(pods)
    await a_history(api, pool, master)
    run, schedule = await a_routine_run(api, master, "Tidy up")
    await in_its_pod(api, pool, run, "echo tidied > notes.txt")
    # Its worker died between its work and its record.  The worker that takes its turn up holds no pod of the
    # master's, and the turn has called a tool: what the real files changed is not yours.
    await a_turn(api, monkeypatch, run, [_final_response("Tidied.")], pool=SandboxPool(pods))
    [its] = await pickups_of(api, master)
    assert (changed(its), its.commit) == (["notes.txt"], main_of(pods))
    assert git(pods.project / "_history", "log", "-1", "--format=%ae", its.commit) == f"routine:{schedule.id}@surogate"


async def test_a_routine_run_that_changed_nothing_records_nothing(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    pool = SandboxPool(pods)
    await a_history(api, pool, master)
    before = main_of(pods)
    looked, _ = await a_routine_run(api, master, "Health check")
    await a_turn(api, monkeypatch, looked, [calling(("terminal", {"command": "cat notes.txt"})), _final_response("Fine.")], pool=pool)
    assert await pickups_of(api, master) == [] and main_of(pods) == before
    # One that called no tool wrote no file: an edit of yours meanwhile is not its change, and waits for a pickup by you.
    pick_up = landing_module.pick_up_routine

    async def then_you_save(**arguments):
        picked = await pick_up(**arguments)
        (pods.project / "brief.pdf").write_bytes(b"%PDF uploaded while the routine spoke")
        return picked

    monkeypatch.setattr(loop_artifact_completion, "pick_up_routine", then_you_save)
    spoke, _ = await a_routine_run(api, master, "Greeting")
    await a_turn(api, monkeypatch, spoke, [_final_response("Good morning.")], pool=pool)
    assert await pickups_of(api, master) == [] and main_of(pods) == before


async def test_a_stopped_routine_runs_changes_are_the_routines_too(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    pool = SandboxPool(pods)
    await a_history(api, pool, master)
    run, schedule = await a_routine_run(api, master, "Tidy up")

    async def stopped(harness) -> None:
        # As the stop's route does: the turn's end is written before the turn is torn down.
        await api.app.state.session_store.emit_event(run.id, EventType.SESSION_PAUSE, {"reason": "stopped by the user"})
        await stop(harness)

    await a_turn(api, monkeypatch, run, [
        calling(("terminal", {"command": "echo tidied > notes.txt"})), calling(("memory", {"action": "add", "content": "x"})),
        _final_response("never said"),
    ], pool=pool, during=stopped)
    [its] = await pickups_of(api, master)
    assert (changed(its), its.commit) == (["notes.txt"], main_of(pods))
    assert git(pods.project / "_history", "log", "-1", "--format=%ae", its.commit) == f"routine:{schedule.id}@surogate"


async def test_a_routine_run_through_a_masters_pod_made_before_it_kept_a_history_records_nothing(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    pool = SandboxPool(pods)
    await a_history(api, pool, master)
    before = main_of(pods)
    provision = pods.provision

    async def older(spec):
        spec.env.pop("HISTORY_MAIN", None)
        return await provision(spec)

    monkeypatch.setattr(pods, "provision", older)
    run, _ = await a_routine_run(api, master, "Tidy up")
    await a_turn(api, monkeypatch, run, [
        calling(("terminal", {"command": "echo tidied > notes.txt"})), _final_response("Tidied."),
    ], pool=pool)
    # Its turn ends all the same, with nothing recorded.
    store = api.app.state.session_store
    assert len(await store.get_events(run.id, types=[EventType.SESSION_COMPLETE])) == 1
    assert await pickups_of(api, master) == [] and main_of(pods) == before
    # The next landing records the change, as yours.
    thread = await a_thread(api, "Draft A", master)
    await edited(pool, thread, "echo a > a.md")
    await ends(api, pool, thread)
    [landed] = await rows(api, thread)
    assert [f["path"] for f in landed.picked_up] == ["notes.txt"]


async def test_a_routine_runs_pickup_is_tried_once(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    pool = SandboxPool(pods)
    await a_history(api, pool, master)
    before = main_of(pods)
    run, _ = await a_routine_run(api, master, "Tidy up")
    await in_its_pod(api, pool, run, "echo tidied > notes.txt")
    call, tries = landing_module._call, []

    async def fails(sandbox_pool, owner, action, **arguments):
        if action == "pickup":
            tries.append(arguments["author"]["name"])
            raise landing_module.LandingStepError("the pod's disk is full")
        return await call(sandbox_pool, owner, action, **arguments)

    monkeypatch.setattr(landing_module, "_call", fails)
    await ends(api, pool, run, settings=SimpleNamespace(default_step_timeout=29, default_max_retries=3, retry_delay=0))
    # Its turn ends all the same; the next landing picks the change up, as yours.
    assert tries == ["Tidy up"] and await pickups_of(api, master) == [] and main_of(pods) == before
    assert len(await api.app.state.session_store.get_events(run.id, types=[EventType.SESSION_COMPLETE])) == 1


async def test_a_routine_runs_pickup_that_lost_the_projects_lock_pushes_nothing(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    pool = SandboxPool(pods)
    await a_history(api, pool, master)
    before = main_of(pods)
    run, _ = await a_routine_run(api, master, "Tidy up")
    await in_its_pod(api, pool, run, "echo tidied > notes.txt")
    settle = landing_module.settle_running

    async def then_the_lock_goes(*args, **kwargs):
        settled = await settle(*args, **kwargs)
        await lose_the_lock(api, SimpleNamespace(config={"workstream_id": project_of(master.config["workspace_boundary"])}))
        return settled

    monkeypatch.setattr(landing_module, "settle_running", then_the_lock_goes)
    await ends(api, pool, run)
    assert await pickups_of(api, master) == [] and main_of(pods) == before


async def test_a_routine_run_on_the_users_computer_asks_no_pod_for_a_pickup(api, monkeypatch, pods, caplog):
    master = await master_of(api, await create(api))
    run, _ = await a_routine_run(api, master, "Tidy up")
    await api.app.state.session_store.emit_event(
        run.id, EventType.TOOL_CALL, {"tool_call_id": "call_0_terminal", "name": "terminal", "arguments": {}},
    )
    there = run.model_copy(update={"config": {**run.config, "execution": {"kind": "device", "device_id": str(run.id)}}})
    asked: list = []

    async def pick_up(**arguments):
        asked.append(arguments)

    monkeypatch.setattr(loop_artifact_completion, "pick_up_routine", pick_up)
    monkeypatch.setattr(pods, "provision", pick_up)
    harness = harness_of(api)
    harness._sandbox_pool = SandboxPool(pods)
    await harness._pick_up_routine(there, yours=True)
    await harness._pick_up_routine(there)
    assert asked == [] and "Could not pick up" not in caplog.text


async def test_only_a_projects_masters_pod_keeps_a_history_of_its_workspace(api):
    created = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    chat = await api.app.state.session_store.get_session(created.json()["id"])
    tenant = SimpleNamespace(org_id=chat.org_id, user_id=chat.user_id)
    assert chat.config.get("storage_bucket")
    assert "HISTORY_MAIN" not in (await _build_session_sandbox_spec(chat, tenant, str(chat.id))).env


async def test_only_a_masters_scheduled_run_is_a_routine_run_over_the_real_files():
    boundary = f"{PROJECT_BOUNDARY_PREFIX}5b0c1c1e-0000-4000-8000-000000000001"
    its = {"scheduled_session_id": "r1", "workspace_boundary": boundary}

    def run(channel="scheduled", **config):
        return SimpleNamespace(channel=channel, config=config)

    assert landing_module.routine_project(run(**its)) == project_of(boundary) is not None
    # A thread's run is its helper, on a copy; a delegate of a run is no run; nor is a chat's schedule a project's.
    assert landing_module.routine_project(run(**its, history_thread="t1")) is None
    assert landing_module.routine_project(run("delegation", **its)) is None
    assert landing_module.routine_project(run(workspace_boundary=boundary)) is None
    assert landing_module.routine_project(run(scheduled_session_id="r1")) is None

