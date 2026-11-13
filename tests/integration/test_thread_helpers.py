"""A project thread's helpers, end to end, against the real database and thread pods.

Each way a thread starts a helper: the thread's copy is handed on first, the
helper works on a copy of its own, and its work comes back to the thread and
lands at the thread's turn end.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

import surogates.harness.loop as loop_module
from surogates.governance.policy import GovernanceGate
from surogates.harness import agent_resolver, tool_exec
from surogates.harness import landing as landing_module
from surogates.sandbox.history import PRUNE_DAYS, History, HistoryError
from surogates.sandbox.pool import SandboxPool
from surogates.session.acting_principal import ActingPrincipal
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from surogates.tools.builtin import coding_agent
from surogates.tools.builtin import delegate as delegate_module
from surogates.workstreams import thread_refusal
from tests.test_steer_loop import _final_response

from .test_devices import api  # noqa: F401  (api is a fixture)
from .test_durable_landings import FENCED, a_short_fence, edited, ends, stored  # noqa: F401  (a_short_fence is a fixture)
from .test_thread_copies import a_thread, a_waking_thread_harness, git, its_first_turn_was_taken, pods, reports  # noqa: F401  (pods is a fixture)
from .test_turn_sagas import a_looping_harness, a_turn, calling, stop
from .test_workstream_threads import call_tool
from .test_workstreams import create, master_of

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def helpers_of(api, session) -> list:
    """The sessions *session* started, oldest first."""
    async with api.app.state.session_factory() as db:
        ids = (await db.execute(text("SELECT id FROM sessions WHERE parent_id = :id ORDER BY created_at"), {"id": session.id})).scalars().all()
    return [await api.app.state.session_store.get_session(found) for found in ids]


async def a_helpers_turn(api, monkeypatch, pool, helper, *replies) -> None:
    """*helper*'s whole turn: each of *replies* a command in its pod, or a model's reply, then its turn's end."""
    with monkeypatch.context() as its_own:
        await a_turn(api, its_own, helper, [
            *(calling(("terminal", {"command": reply})) if isinstance(reply, str) else reply for reply in replies),
            _final_response("Done."),
        ], pool=pool)


async def results_of(api, session, name: str) -> list[str]:
    events = await api.app.state.session_store.get_events(session.id, types=[EventType.TOOL_RESULT])
    return [e.data["content"] for e in events if e.data["name"] == name]


def handed_on_to(api, monkeypatch, pods, thread) -> list[list[str] | None]:
    """The files on *thread*'s hand-off at the moment each session under it is made; None where it has none."""
    seen: list[list[str] | None] = []
    store = api.app.state.session_store
    create_session = store.create_session

    async def watched(**session):
        if session.get("config", {}).get("history_thread") == str(thread.id):
            listed = subprocess.run(
                ["git", f"--git-dir={pods.project / '_history'}", "ls-tree", "-r", "--name-only", f"refs/handoff/{thread.id}"],
                capture_output=True, text=True,
            )
            seen.append(listed.stdout.split() if listed.returncode == 0 else None)
        return await create_session(**session)

    monkeypatch.setattr(store, "create_session", watched)
    return seen


def refs(pods, like: str = "") -> list[str]:
    """The history's refs with *like* in their name."""
    return [ref for ref in git(pods.project / "_history", "for-each-ref", "--format=%(refname)").splitlines() if like in ref]


async def a_coordinating_thread(api, master=None):
    """A thread whose agent coordinates: it is offered the tools that start workers and tasks."""
    thread = await a_thread(api, "Draft A", master)
    store = api.app.state.session_store
    await store.update_session_config_key(thread.id, "coordinator", True)
    return await store.get_session(thread.id)


def a_worker_waking(api, monkeypatch, pool, session, turn):
    """A worker whose wake of *session* runs for real over *pool*, as its user, its turn *turn*."""
    harness = a_waking_thread_harness(api, monkeypatch, pool, turn)
    harness._tenant = dataclasses.replace(harness._tenant, org_id=session.org_id, user_id=session.user_id)
    harness._acting_principal = ActingPrincipal(user_id=session.user_id, service_account_id=None)
    return harness


def the_helper_is_on_a_copy_of_its_own(pods, thread, helper) -> None:
    assert (helper.config["history_thread"], helper.config["sandbox_root_session_id"]) == (str(thread.id), str(helper.id))
    assert pods.copies[str(helper.id)] != pods.copies[str(thread.id)]



def _goal(tool: str, goal: str) -> dict:
    """The arguments that start a helper on *goal* with *tool* ("background agent": ``agent`` in the background)."""
    if tool == "background agent":
        return {"description": "Helper", "prompt": goal, "run_in_background": True}
    return {"description": "Helper", "prompt": goal} if tool == "agent" else {"goal": goal}


def _name(tool: str) -> str:
    """The tool a parametrized *tool* calls."""
    return "agent" if tool == "background agent" else tool

async def test_a_helper_started_by_agent_works_on_a_copy_of_its_own_and_its_work_lands_with_the_thread(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    mine, theirs = SandboxPool(pods), SandboxPool(pods)  # the thread's worker, and its helper's
    handed_on = handed_on_to(api, monkeypatch, pods, thread)
    poll = delegate_module._poll_child_completion

    async def the_helper_runs(*, session_store, parent_session_id, child_id, **kwargs):
        await a_helpers_turn(
            api, monkeypatch, theirs, await session_store.get_session(child_id),
            "cat outline.md > sources.md && echo by the helper >> sources.md",
        )
        return await poll(session_store=session_store, parent_session_id=parent_session_id, child_id=child_id, **kwargs)

    monkeypatch.setattr(delegate_module, "_poll_child_completion", the_helper_runs)
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo outline > outline.md"})),
        calling(("agent", {"description": "Helper", "prompt": "Draft the sources."})),
        # The helper handed back before its turn's end: its file is in the thread's copy when the step answers.
        calling(("terminal", {"command": "cat sources.md > seen.md"})),
        _final_response("Done."),
    ], pool=mine), 120)
    [helper] = await helpers_of(api, thread)
    # The thread's copy was handed on before the helper was made, its outline with it.
    assert handed_on == [["Report.docx", "notes.txt", "outline.md"]]
    the_helper_is_on_a_copy_of_its_own(pods, thread, helper)
    # Its work came back to the thread, and landed with the thread's turn.
    assert (pods.project / "sources.md").read_text() == "outline\nby the helper\n"
    assert (pods.project / "seen.md").read_text() == "outline\nby the helper\n"
    [report] = await reports(api, master)
    assert {(f["ref"], f["landing"]) for f in report["files"]} == {
        ("outline.md", "landed"), ("sources.md", "landed"), ("seen.md", "landed"),
    }
    # The hand-off went with the landing.
    assert refs(pods, "handoff") == []


@pytest.mark.parametrize("tool", ["background agent"])
@pytest.mark.parametrize("ends_its_turn", ["while the thread's turn runs", "after the thread's turn"])
async def test_a_helper_the_thread_does_not_wait_for_works_on_a_copy_of_its_own_and_its_work_lands_with_the_thread(
    api, monkeypatch, tmp_path, tool, ends_its_turn,
):
    master = await master_of(api, await create(api))
    thread = await a_coordinating_thread(api, master)
    pods = stored(api, thread, tmp_path)
    mine, theirs = SandboxPool(pods), SandboxPool(pods)
    handed_on = handed_on_to(api, monkeypatch, pods, thread)

    async def the_helper_works(harness=None):
        [helper] = await helpers_of(api, thread)
        await a_helpers_turn(api, monkeypatch, theirs, helper, "cat outline.md > sources.md && echo by the helper >> sources.md")

    meanwhile = ends_its_turn == "while the thread's turn runs"
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo outline > outline.md"})),
        calling((_name(tool), _goal(tool, "Draft the sources."))),
        *([calling(("memory", {"action": "add", "content": "x"}))] if meanwhile else []),
        _final_response("Done."),
    ], pool=mine, during=the_helper_works), 120)
    [started] = [json.loads(result) for result in await results_of(api, thread, _name(tool))]
    assert "error" not in started, started
    [helper] = await helpers_of(api, thread)
    assert handed_on == [["Report.docx", "notes.txt", "outline.md"]]
    if meanwhile:
        # It kept its work on the hand-off; the thread's turn end took it up and landed it with its own.
        assert (pods.project / "sources.md").read_text() == "outline\nby the helper\n"
    else:
        # The thread's turn landed its own work, and the hand-off went with that.
        assert (pods.real_names(), refs(pods, "handoff")) == (["Report.docx", "notes.txt", "outline.md"], [])
        await the_helper_works()
        # Its work waits on a hand-off of its own making, in no real file, until the thread's next turn ends:
        # the turn its report wakes, which uses no tool.
        assert refs(pods, "handoff") == [f"refs/handoff-from/{thread.id}", f"refs/handoff/{thread.id}"]
        assert not (pods.project / "sources.md").exists()
        await ends(api, mine, thread)
        assert (pods.project / "sources.md").read_text() == "outline\nby the helper\n"
    the_helper_is_on_a_copy_of_its_own(pods, thread, helper)
    assert pods.real_names() == ["Report.docx", "notes.txt", "outline.md", "sources.md"]
    assert refs(pods, "handoff") == []


def an_agent(*tools: str) -> SimpleNamespace:
    """A sub-agent as the agent's bundle defines one, with *tools*."""
    return SimpleNamespace(tools=list(tools), disallowed_tools=[], model=None, max_iterations=None, preloaded_skills=[])


@pytest.mark.parametrize("command, why", [
    # Run end to end in a thread, a deep research handed its topic to a planner on a copy of its own, and
    # the planner its outline to a writer on another, started from the thread's hand-off as it was: the
    # writer's copy had none of the evidence the planner kept in .research/, which is all it writes from.
    ("/deep-research Heat pumps in cold climates", "the writer finds no evidence"),
], ids=["deep-research"])
async def test_a_threads_research_command_is_refused_and_starts_nothing(api, monkeypatch, pods, command, why):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    # Typed once the thread's first turn has read its goal: before that the command waits for that turn.
    await its_first_turn_was_taken(store, thread)
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": command})
    ran: list = []

    async def a_turn_of_it(*args, **kwargs):
        ran.append(args)

    await asyncio.wait_for(a_worker_waking(api, monkeypatch, pool, thread, a_turn_of_it).wake(thread.id), 60)
    *_, answer = [e.data["message"]["content"] for e in await store.get_events(thread.id, types=[EventType.LLM_RESPONSE])]
    name = command.split()[0]
    assert answer == f"A thread can't start {name} {NO_OTHER_WAY}", why
    # No turn ran for it, no helper or task was started, no pod, no history.
    thread = await store.get_session(thread.id)
    assert (ran, await helpers_of(api, thread), pods.pods) == ([], [], {})
    assert not {"active_mission_id", "coordinator"} & set(thread.config)
    assert not (pods.project / "_history").exists()


async def test_a_helper_that_fails_leaves_its_files_apart_and_its_thread_is_told_in_the_steps_result(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    mine, theirs = SandboxPool(pods), SandboxPool(pods)
    poll = delegate_module._poll_child_completion

    async def the_helper_fails(*, session_store, parent_session_id, child_id, **kwargs):
        helper = await session_store.get_session(child_id)
        await edited(theirs, helper, "printf ' half made' >> Report.docx && echo half > sources.md")
        await ends(api, theirs, helper, failed=True)
        return await poll(session_store=session_store, parent_session_id=parent_session_id, child_id=child_id, **kwargs)

    monkeypatch.setattr(delegate_module, "_poll_child_completion", the_helper_fails)
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo outline > outline.md"})),
        calling(("agent", {"description": "Helper", "prompt": "Draft the sources."})),
        _final_response("Done."),
    ], pool=mine), 120)
    [helper] = await helpers_of(api, thread)
    [result] = await results_of(api, thread, "agent")
    # The step's result names the files the helper left, which are not in the thread's copy.
    assert "Report.docx, sources.md" in result, result
    # Kept in the history, apart; the thread's own work landed, and none of the helper's.
    assert git(pods.project / "_history", "show", f"refs/helpers/{thread.id}/{helper.id}:sources.md") == "half"
    assert pods.real_names() == ["Report.docx", "notes.txt", "outline.md"]
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"


@pytest.mark.parametrize("pod", ["the turn's own", "one made again"])
async def test_a_stop_while_a_helper_runs_takes_the_turns_work_back_and_the_helpers_own_work_lands_later(
    api, monkeypatch, tmp_path, pod,
):
    master = await master_of(api, await create(api))
    thread = await a_coordinating_thread(api, master)
    pods = stored(api, thread, tmp_path)
    store, mine, theirs = api.app.state.session_store, SandboxPool(pods), SandboxPool(pods)

    async def stopped_while_the_helper_works(harness):
        [helper] = await helpers_of(api, thread)
        # The helper is at work in its own pod, on the copy the thread handed on.
        await edited(theirs, helper, "cat outline.md > sources.md && echo by the helper >> sources.md")
        if pod == "one made again":
            await pods.destroy(mine.sandbox_of(str(thread.id)))  # the thread's pod went under its turn
        await stop(harness)

    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo outline > outline.md"})),
        calling(("agent", {"description": "Helper", "prompt": "Draft the sources.", "run_in_background": True})),
        calling(("memory", {"action": "add", "content": "x"})),
        _final_response("Done."),
    ], pool=mine, during=stopped_while_the_helper_works), 120)
    [helper] = await helpers_of(api, thread)
    # The stop dropped the hand-off, and the branch never had the stopped turn's work: nothing of it is left to land.
    assert [ref for ref in refs(pods) if str(thread.id) in ref] == []
    assert pods.real_names() == ["Report.docx", "notes.txt"]
    assert not landing_module.handed_on(thread)
    # The helper is not stopped with it.  At its end it hands back onto no hand-off, and makes one from where it started.
    await ends(api, theirs, helper)
    assert refs(pods, "handoff") == [f"refs/handoff-from/{thread.id}", f"refs/handoff/{thread.id}"]
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Something else."})
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo other > other.md"})), _final_response("Done."),
    ], pool=SandboxPool(pods))  # with a landing's own bounds: a step of a second fails on a busy host
    # What the helper changed itself lands with the thread's next turn, made from the stopped turn's outline as it
    # is; the outline itself, the stopped turn's own file, never does.
    assert pods.real_names() == ["Report.docx", "notes.txt", "other.md", "sources.md"]
    assert (pods.project / "sources.md").read_text() == "outline\nby the helper\n"


async def the_loop_runs(api, looping, session) -> None:
    """*session*'s turn in *looping*, a harness made for it, as a worker that holds its lease runs it."""
    store = api.app.state.session_store
    lease = await store.try_acquire_lease(session.id, "worker-helpers", ttl_seconds=60)
    events = await store.get_events(session.id)
    await looping._run_loop(session, looping._rebuild_messages(events), "system", lease, all_events=events)
    await store.release_lease(session.id, lease.lease_token)


async def project_locks_taken(monkeypatch) -> list:
    """Each time the project's lock is asked for a keep, a hand-off or a stop's drop, from now on."""
    taken: list = []
    lock = landing_module.project_lock

    def counted(session_factory, workstream):
        taken.append(workstream)
        return lock(session_factory, workstream)

    monkeypatch.setattr(landing_module, "project_lock", counted)
    return taken


@pytest.mark.parametrize("refused_by", ["the tool itself", "governance"])
async def test_a_helper_starting_call_that_starts_none_hands_nothing_on_and_leaves_a_stop_nothing_to_drop(
    api, monkeypatch, pods, refused_by,
):
    thread = await a_thread(api)
    mine = SandboxPool(pods)
    stopped: list = []

    async def stops(harness):
        stopped.append(landing_module.handed_on(thread))
        await stop(harness)

    looping = a_looping_harness(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo outline > outline.md"})),
        # No goal: the tool refuses the call itself.  Or governance denies the tool to this agent.
        calling(("agent", {} if refused_by == "the tool itself" else {"description": "Helper", "prompt": "Draft the sources."})),
        calling(("memory", {"action": "add", "content": "x"})),
        _final_response("Done."),
    ], pool=mine, during=stops)
    if refused_by == "governance":
        looping._governance_gate = GovernanceGate(denied_tools={"agent"})
    locks = await project_locks_taken(monkeypatch)
    await asyncio.wait_for(the_loop_runs(api, looping, thread), 60)
    if refused_by == "governance":
        # A tool the policy denies is not even offered: the call never runs.
        assert await results_of(api, thread, "agent") == []
    else:
        [refusal] = await results_of(api, thread, "agent")
        assert "error" in json.loads(refusal), refusal
    # No helper, no lock asked, nothing pushed; and the stop after it found nothing to drop, and asked no lock either.
    assert await helpers_of(api, thread) == []
    assert (stopped, locks) == ([False], [])
    assert not (pods.project / "_history" / "packed-refs").exists()


NOT_TAKEN_UP = "Changed here and by a helper, so this copy keeps its own version (the helper's is in the history)"


async def test_a_step_that_starts_another_helper_takes_the_firsts_work_up_and_says_which_file_the_thread_kept_its_own_of(
    api, monkeypatch, pods,
):
    master = await master_of(api, await create(api))
    thread = await a_coordinating_thread(api, master)
    mine, theirs = SandboxPool(pods), SandboxPool(pods)
    handed_on = handed_on_to(api, monkeypatch, pods, thread)

    async def the_first_helper_ends(harness):
        [helper] = await helpers_of(api, thread)
        await a_helpers_turn(api, monkeypatch, theirs, helper, "cat outline.md > sources.md && echo by the helper > notes.txt")

    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo outline > outline.md"})),
        calling(("agent", {"description": "Helper", "prompt": "Draft the sources.", "run_in_background": True})),
        calling(("terminal", {"command": "echo by the thread > notes.txt"})),
        calling(("memory", {"action": "add", "content": "x"})),
        calling(("agent", {"description": "Helper", "prompt": "Check them.", "run_in_background": True})),
        _final_response("Done."),
    ], pool=mine, during=the_first_helper_ends), 120)
    first, second = [json.loads(result) for result in await results_of(api, thread, "agent")]
    # The second step took the first helper's work up before it handed the copy on: its sources, not its notes,
    # which the thread changed too.  The result stays the JSON its tool returns, the note a key of its own.
    # A background agent's result carries its own note; the step's note joins it.
    assert NOT_TAKEN_UP not in first["note"] and "error" not in second
    assert second["note"].endswith(f"{NOT_TAKEN_UP}: notes.txt")
    assert handed_on == [["Report.docx", "notes.txt", "outline.md"], ["Report.docx", "notes.txt", "outline.md", "sources.md"]]
    assert pods.real_names() == ["Report.docx", "notes.txt", "outline.md", "sources.md"]
    assert (pods.project / "notes.txt").read_text() == "by the thread\n"
    assert (pods.project / "sources.md").read_text() == "outline\n"


async def test_a_steps_note_is_a_key_of_its_own_in_a_json_result_and_a_line_after_any_other():
    assert json.loads(tool_exec._noted('{"status": "started"}', "kept: a.md")) == {"status": "started", "note": "kept: a.md"}
    assert tool_exec._noted("Done.", "kept: a.md") == "Done.\n\n[kept: a.md]"
    assert tool_exec._noted("[1, 2]", "kept: a.md") == "[1, 2]\n\n[kept: a.md]"
    # A second note joins the first, and the result's own text stays as it was written.
    assert tool_exec._noted('{"output": "café", "note": "kept: a.md"}', "deck.pptx changed") == (
        '{"output": "café", "note": "kept: a.md deck.pptx changed"}'
    )


#: A model's reply the provider failed: with no retry left, the turn fails.
THE_PROVIDER_FAILS = (
    {"role": "assistant", "content": ""},
    {"model": "test-model", "finish_reason": "error", "input_tokens": 1, "output_tokens": 0},
)


async def test_a_helper_that_fails_after_starting_a_helper_of_its_own_lands_none_of_its_own_files(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    mine, theirs = SandboxPool(pods), SandboxPool(pods)
    monkeypatch.setattr(agent_resolver, "resolve_agent_by_name", AsyncMock(return_value=an_agent("terminal", "agent")))
    monkeypatch.setattr(loop_module, "_MAX_PROVIDER_ERROR_RETRIES", 0)
    handed_on = handed_on_to(api, monkeypatch, pods, thread)
    poll = delegate_module._poll_child_completion

    async def each_runs(*, session_store, parent_session_id, child_id, **kwargs):
        helper = await session_store.get_session(child_id)
        if parent_session_id == thread.id:
            # It writes a file, starts a helper of its own, and then its turn fails.
            with monkeypatch.context() as its_own:
                await a_turn(api, its_own, helper, [
                    calling(("terminal", {"command": "echo half made > first.md"})),
                    calling(("agent", {"description": "Helper", "prompt": "Check it."})),
                    THE_PROVIDER_FAILS,
                ], pool=theirs)
        else:
            await a_helpers_turn(api, monkeypatch, theirs, helper, "ls -A > seen.txt && echo checked > second.md")
        return await poll(session_store=session_store, parent_session_id=parent_session_id, child_id=child_id, **kwargs)

    monkeypatch.setattr(delegate_module, "_poll_child_completion", each_runs)
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo outline > outline.md"})),
        calling(("agent", {"description": "Helper", "prompt": "Draft the sources.", "agent_type": "lead"})),
        _final_response("Done."),
    ], pool=mine), 120)
    [helper] = await helpers_of(api, thread)
    # Its step that started a helper put nothing of its own on the thread's hand-off: its helper started from
    # the thread's work as the thread handed it on.
    assert handed_on == [["Report.docx", "notes.txt", "outline.md"]] * 2
    assert "first.md" not in (pods.project / "seen.txt").read_text().split()
    # So its failed turn's file, written before that step, is kept apart whole with what it wrote after, and the
    # thread is told; it never lands.  Its own helper's finished work does.
    [result] = await results_of(api, thread, "agent")
    assert "first.md" in result, result
    assert git(pods.project / "_history", "show", f"refs/helpers/{thread.id}/{helper.id}:first.md") == "half made"
    assert pods.real_names() == ["Report.docx", "notes.txt", "outline.md", "second.md", "seen.txt"]


async def test_a_step_that_hands_the_copy_on_waits_out_the_fence_its_harness_runs_with(api, monkeypatch, pods):
    thread = await a_coordinating_thread(api)
    fences: list[float] = []
    keep = landing_module.keep_copy

    async def watched(**kept):
        fences.append(landing_module._fence(kept["saga_settings"]))
        return await keep(**kept)

    monkeypatch.setattr(landing_module, "keep_copy", watched)
    # A worker whose steps have fifteen minutes and a retry: a landing that lost its lock unseen is taken
    # for dead only after that, never after the default five minutes.
    slow = SimpleNamespace(default_step_timeout=900, default_max_retries=1, retry_delay=2)
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("agent", {"description": "Helper", "prompt": "Draft the sources.", "run_in_background": True})), _final_response("Done."),
    ], pool=SandboxPool(pods), saga_settings=slow), 120)
    assert fences == [903.0]


async def test_a_stop_of_a_turn_that_used_no_pod_opens_none(api, monkeypatch, pods):
    thread = await a_thread(api)

    def replies():
        looping.interrupt("stopped by the user")  # while the model is asked for the turn's first step
        yield _final_response("Done.")

    looping = a_looping_harness(api, monkeypatch, thread, replies(), pool=SandboxPool(pods))
    await asyncio.wait_for(the_loop_runs(api, looping, thread), 60)
    assert await api.app.state.session_store.get_events(thread.id, types=[EventType.SESSION_COMPLETE]) == []
    assert (pods.pods, pods.copies) == ({}, {})


async def a_thread_whose_helper_finished_after_its_turn(api, monkeypatch, tmp_path):
    """A coordinating thread that landed a turn, and whose helper then kept ``sources.md`` and reported; with its master, pods and pool."""
    master = await master_of(api, await create(api))
    thread = await a_coordinating_thread(api, master)
    pods = stored(api, thread, tmp_path)
    mine = SandboxPool(pods)
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo outline > outline.md"})),
        calling(("agent", {"description": "Helper", "prompt": "Draft the sources.", "run_in_background": True})),
        _final_response("Started."),
    ], pool=mine), 120)
    [first] = await helpers_of(api, thread)
    await a_helpers_turn(api, monkeypatch, SandboxPool(pods), first, "echo an hour of work > sources.md")
    await api.app.state.session_store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Now check them."})
    return master, thread, pods, mine


async def one_more_turn(api, monkeypatch, pods, thread, command: str) -> None:
    await api.app.state.session_store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Something else."})
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": command})), _final_response("Done."),
    ], pool=SandboxPool(pods)), 120)


async def test_a_stop_of_a_turn_that_took_a_helpers_finished_work_up_and_handed_on_loses_none_of_that_work(api, monkeypatch, tmp_path):
    master, thread, pods, mine = await a_thread_whose_helper_finished_after_its_turn(api, monkeypatch, tmp_path)
    # The next turn's copy takes the helper's work up, the turn starts a second helper, and is stopped.
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "ls > seen.txt"})),
        calling(("agent", {"description": "Helper", "prompt": "Check the sources.", "run_in_background": True})),
        calling(("memory", {"action": "add", "content": "x"})),
        _final_response("Done."),
    ], pool=mine, during=stop), 120)
    assert not (pods.project / "sources.md").exists()
    # The hand-off is back as the turn found it: the first helper's work, for the next turn to take up.
    assert refs(pods, "handoff") == [f"refs/handoff-from/{thread.id}", f"refs/handoff/{thread.id}"]
    await one_more_turn(api, monkeypatch, pods, thread, "ls > seen-later.txt")
    assert "sources.md" in (pods.project / "seen-later.txt").read_text().split()
    assert (pods.project / "sources.md").read_text() == "an hour of work\n"
    # And the stopped turn's own file is nowhere.
    assert pods.real_names() == ["Report.docx", "notes.txt", "outline.md", "seen-later.txt", "sources.md"]
    assert ("sources.md", "landed") in {(f["ref"], f["landing"]) for f in (await reports(api, master))[-1]["files"]}


async def test_a_thread_whose_pod_goes_after_its_turn_took_a_helpers_work_up_and_handed_on_lands_that_work(api, monkeypatch, tmp_path):
    master, thread, pods, mine = await a_thread_whose_helper_finished_after_its_turn(api, monkeypatch, tmp_path)

    async def the_pod_goes(harness):
        await pods.destroy(mine.sandbox_of(str(thread.id)))  # evicted, under the turn

    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "ls > seen.txt"})),
        calling(("agent", {"description": "Helper", "prompt": "Check the sources.", "run_in_background": True})),
        calling(("memory", {"action": "add", "content": "x"})),
        calling(("terminal", {"command": "ls > seen-after.txt"})),
        _final_response("Done."),
    ], pool=mine, during=the_pod_goes), 120)
    # The copy made in the pod's place took up what was handed on: the helper's work lands with this very turn.
    assert "sources.md" in (pods.project / "seen-after.txt").read_text().split()
    assert (pods.project / "sources.md").read_text() == "an hour of work\n"
    assert ("sources.md", "landed") in {(f["ref"], f["landing"]) for f in (await reports(api, master))[-1]["files"]}
    assert refs(pods, "handoff") == []


COMMANDS_A_THREAD_REFUSES = {
    "loop": "/loop 5m Add a line to notes.txt.",
    "code": "/code claude Add a line to notes.txt.",
    "deep-research": "/deep-research Heat pumps in cold climates",
}


async def woken_with_nothing_started(api, monkeypatch, pool, helper) -> list[str]:
    """*helper* woken for real; what it answered, having run no turn, no coding agent and no schedule."""
    ran: list = []

    async def a_turn_of_it(*args, **kwargs):
        ran.append("a turn")

    harness = a_worker_waking(api, monkeypatch, pool, helper, a_turn_of_it)
    harness._run_code_agent = AsyncMock(side_effect=lambda *args, **kwargs: ran.append("the coding agent"))
    await asyncio.wait_for(harness.wake(helper.id), 60)
    store = api.app.state.session_store
    async with api.app.state.session_factory() as db:
        schedules = (await db.execute(
            text("SELECT count(*) FROM scheduled_sessions WHERE created_from_session_id = :id"), {"id": helper.id},
        )).scalar()
    made = set((await store.get_session(helper.id)).config) & {"active_mission_id", "coordinator"}
    assert (ran, schedules, made, await helpers_of(api, helper)) == ([], 0, set(), [])
    return [e.data["message"]["content"] for e in await store.get_events(helper.id, types=[EventType.LLM_RESPONSE])]


@pytest.mark.parametrize("command", sorted(COMMANDS_A_THREAD_REFUSES))
@pytest.mark.parametrize("tool", ["agent", "background agent"])
async def test_a_threads_helper_whose_goal_is_a_command_its_thread_may_not_run_is_refused_it_too(api, monkeypatch, pods, command, tool):
    thread = await a_coordinating_thread(api)
    mine, theirs = SandboxPool(pods), SandboxPool(pods)
    # A helper that is waited for: its outcome, without its turn.  It is woken below, as a worker wakes it.
    monkeypatch.setattr(delegate_module, "_poll_child_completion", AsyncMock(return_value={"status": "failed", "reason": "not run here"}))
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling((_name(tool), _goal(tool, COMMANDS_A_THREAD_REFUSES[command]))), _final_response("Done."),
    ], pool=mine), 120)
    # Its goal is its first message, word for word: read as a command, it gets the answer its thread gets.
    [helper] = await helpers_of(api, thread)
    assert await woken_with_nothing_started(api, monkeypatch, theirs, helper) == [thread_refusal(f"/{command}")]


@pytest.mark.parametrize("command", sorted(COMMANDS_A_THREAD_REFUSES))
async def test_a_helper_of_a_threads_helper_is_refused_the_commands_too(api, monkeypatch, pods, command):
    thread = await a_thread(api)
    store = api.app.state.session_store
    helper = await create_child_session(store=store, parent=thread, channel="delegation")
    its_own = await create_child_session(store=store, parent=helper, channel="worker")
    await store.emit_event(its_own.id, EventType.USER_MESSAGE, {"content": COMMANDS_A_THREAD_REFUSES[command]})
    assert await woken_with_nothing_started(api, monkeypatch, SandboxPool(pods), its_own) == [thread_refusal(f"/{command}")]


async def test_a_threads_helper_and_its_own_are_refused_the_tools_their_thread_is(api):
    tool = "cron_create"
    thread = await a_thread(api)
    store = api.app.state.session_store
    helper = await create_child_session(store=store, parent=thread, channel="delegation")
    its_own = await create_child_session(store=store, parent=helper, channel="worker")
    for session in (helper, its_own):
        assert await call_tool(api, session, tool) == {"error": thread_refusal(tool)}


NO_OTHER_WAY = "yet, and has no other way to do it: tell the user it cannot be done in a project's thread."


@pytest.mark.parametrize("tool, arguments, kind", [
    ("agent", {"description": "Research", "prompt": "Heat pumps in cold climates", "agent_type": "deep-research"}, "deep-research"),
    ("agent", {"description": "Write", "prompt": "Write the report.", "agent_type": "research-writer"}, "research-writer"),
    ("agent", {"description": "Research", "prompt": "Heat pumps in cold climates", "agent_type": "deep-research",
               "run_in_background": True}, "deep-research"),
], ids=["agent", "the writer alone", "background agent"])
async def test_a_thread_and_its_helpers_start_no_research_sub_agent_by_its_type(api, monkeypatch, pods, tool, arguments, kind):
    thread = await a_coordinating_thread(api)
    # The agent has the sub-agents: nothing but the thread's rule stands between the call and them.
    monkeypatch.setattr(agent_resolver, "resolve_agent_by_name", AsyncMock(return_value=an_agent("terminal", "agent")))
    monkeypatch.setattr(delegate_module, "_poll_child_completion", AsyncMock(return_value={"status": "failed", "reason": "not run here"}))
    locks = await project_locks_taken(monkeypatch)
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling((tool, arguments)), _final_response("Done."),
    ], pool=SandboxPool(pods)), 120)
    # Deep research run in a thread, with or without its command, starts a planner and a writer on copies of
    # their own, where the writer finds no evidence; and the planner's .research/ folder lands among the files.
    [answer] = [json.loads(result) for result in await results_of(api, thread, tool)]
    assert answer == {"error": f'A thread can\'t start the "{kind}" sub-agent {NO_OTHER_WAY}'}
    [call] = await api.app.state.session_store.get_events(thread.id, types=[EventType.TOOL_CALL])
    assert "checkpoint_hash" not in call.data
    assert (await helpers_of(api, thread), pods.pods, locks) == ([], {}, [])
    # A helper of the thread, and its own, get the same answer.
    store = api.app.state.session_store
    helper = await create_child_session(store=store, parent=thread, channel="delegation")
    for session in (helper, await create_child_session(store=store, parent=helper, channel="worker")):
        assert await call_tool(api, session, tool, **arguments) == answer


async def test_what_a_thread_has_no_other_way_to_do_is_refused_in_words_that_send_it_nowhere_else():
    # "Do this step in the thread itself" would, for research, be the very delegation that is refused.
    for name in ("/deep-research", 'the "deep-research" sub-agent'):
        assert thread_refusal(name) == f"A thread can't start {name} {NO_OTHER_WAY}"
    for name in ("/loop", "/code", "cron_create"):
        assert thread_refusal(name) == f"A thread can't start {name} yet: do this step in the thread itself."


@pytest.mark.parametrize("tool", ["agent", "background agent"])
async def test_a_step_whose_hand_off_fails_starts_no_helper_on_old_files_and_says_so(api, monkeypatch, pods, tool):
    thread = await a_coordinating_thread(api)
    monkeypatch.setattr(delegate_module, "_poll_child_completion", AsyncMock(return_value={"status": "failed", "reason": "not run here"}))

    def fails(self, **_):
        raise HistoryError("the bucket did not answer")

    monkeypatch.setattr(History, "hand_off", fails)
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo outline > outline.md"})),
        calling((_name(tool), _goal(tool, "Draft the sources from outline.md."))),
        _final_response("Done."),
    ], pool=SandboxPool(pods)), 120)
    # A helper started now would work without this turn's outline, and nobody would know: none is started.
    [result] = await results_of(api, thread, _name(tool))
    assert "error" in json.loads(result) and "could not be handed to a helper, so none was started" in result, result
    assert await helpers_of(api, thread) == []
    # The turn's own work lands as ever.
    assert pods.real_names() == ["Report.docx", "notes.txt", "outline.md"]


async def a_thread_whose_worker_outlives_its_turn(api, monkeypatch, tmp_path):
    """A coordinating thread whose first turn started a worker, now at work in its pod, and landed.

    With its master, its pods and pool, the worker and the worker's pool."""
    master = await master_of(api, await create(api))
    thread = await a_coordinating_thread(api, master)
    pods = stored(api, thread, tmp_path)
    mine, theirs = SandboxPool(pods), SandboxPool(pods)

    async def the_worker_starts_work(harness):
        [helper] = await helpers_of(api, thread)
        await edited(theirs, helper, "echo started > started.md")  # its pod opens on the first turn's hand-off

    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo outline > outline.md"})),
        calling(("agent", {"description": "Helper", "prompt": "Draft the sources.", "run_in_background": True})),
        calling(("memory", {"action": "add", "content": "x"})),
        _final_response("Started."),
    ], pool=mine, during=the_worker_starts_work), 120)
    [worker] = await helpers_of(api, thread)
    await api.app.state.session_store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Meanwhile, draft the summary."})
    return master, thread, pods, mine, worker, theirs


@pytest.mark.slow
async def test_a_stop_of_a_turn_during_which_an_earlier_turns_worker_handed_back_is_carried_out(api, monkeypatch, tmp_path, caplog):
    master, thread, pods, mine, worker, theirs = await a_thread_whose_worker_outlives_its_turn(api, monkeypatch, tmp_path)

    async def the_worker_ends_then_the_turn_is_stopped(harness):
        await ends(api, theirs, worker, settings=None)
        await stop(harness)

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
            calling(("terminal", {"command": "echo the stopped turn > stopped.md"})),
            calling(("agent", {"description": "Helper", "prompt": "Check something.", "run_in_background": True})),
            calling(("memory", {"action": "add", "content": "x"})),
            _final_response("Done."),
        ], pool=mine, during=the_worker_ends_then_the_turn_is_stopped), 180)
    assert "Could not take back" not in caplog.text
    await one_more_turn(api, monkeypatch, pods, thread, "ls > seen-later.txt")
    # The worker's file lands with the next turn; the stopped turn's does not.
    assert pods.real_names() == ["Report.docx", "notes.txt", "outline.md", "seen-later.txt", "started.md"]


#: The ways a stop of a turn that handed on is not carried out on the hand-off, or only by a pod that did not
#: make all of it.
WAYS_A_STOP_FALLS_SHORT = [
    "its pod fails it", "the turn was cut off and taken up again", "the project's lock is not had in time",
    "a pod in place of the turn's is not open in time", "a pod made again handed on again",
]


async def a_stop_that_falls_short(api, monkeypatch, tmp_path, caplog, way: str):
    """A thread's turn takes up a helper's finished ``sources.md``, writes ``stopped.md``, starts a helper and is
    stopped, the stop falling short in *way*; then that helper ends, and the thread's next turn lands.

    The project's files after it, and what the log said of the stop."""
    master, thread, pods, mine = await a_thread_whose_helper_finished_after_its_turn(api, monkeypatch, tmp_path)
    theirs, steps = SandboxPool(pods), []
    monkeypatch.setattr(loop_module, "_STOP_HAND_OFF_BOUND", 1.0)

    async def never(*args, **kwargs):
        await asyncio.Event().wait()

    async def stopped(harness):
        steps.append(way)
        if way == "a pod made again handed on again" and len(steps) == 1:
            await pods.destroy(mine.sandbox_of(str(thread.id)))  # the turn goes on in a pod made again
            return
        if way == "its pod fails it":
            call = landing_module._call

            async def fails(sandbox_pool, owner, action, **arguments):
                if action == "drop_hand_off":
                    raise landing_module.LandingStepError("the pod's step timed out")
                return await call(sandbox_pool, owner, action, **arguments)

            monkeypatch.setattr(landing_module, "_call", fails)
        elif way == "the turn was cut off and taken up again":
            landing_module.turn_ended(thread)  # as its worker forgets it, here or on the worker that takes it up
        elif way == "the project's lock is not had in time":
            monkeypatch.setattr(landing_module, "_guarded", a_lock_never_had)
        elif way == "a pod in place of the turn's is not open in time":
            compensate, ensure = harness._compensate_sagas, mine.ensure

            async def then_the_pod_goes(*args, **kwargs):
                await compensate(*args, **kwargs)
                await pods.destroy(mine.sandbox_of(str(thread.id)))
                monkeypatch.setattr(mine, "ensure", never)

            monkeypatch.setattr(harness, "_compensate_sagas", then_the_pod_goes)
            steps.append(ensure)
        await stop(harness)

    again = way == "a pod made again handed on again"
    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
            calling(("terminal", {"command": "echo the stopped turn > stopped.md"})),
            calling(("agent", {"description": "Helper", "prompt": "Check the sources.", "run_in_background": True})),
            calling(("memory", {"action": "add", "content": "x"})),
            *([
                # In the pod made again, whose copy took up what the turn had handed on, its draft with it.
                calling(("agent", {"description": "Helper", "prompt": "Check them again.", "run_in_background": True})),
                calling(("memory", {"action": "add", "content": "x"})),
            ] if again else []),
            _final_response("Done."),
        ], pool=mine, during=stopped), 180)
    said = "\n".join(r.getMessage() for r in caplog.records if "stop of thread" in r.getMessage())
    # What made the stop fall short is over.
    monkeypatch.setattr(landing_module, "_guarded", GUARDED)
    monkeypatch.setattr(landing_module, "_call", CALL)
    # The helper the stopped turn started ends after the stop, with a file of its own.
    checker = (await helpers_of(api, thread))[1]
    await asyncio.wait_for(a_helpers_turn(api, monkeypatch, theirs, checker, "echo checked > checked.md"), 120)
    await one_more_turn(api, monkeypatch, pods, thread, "ls > seen-later.txt")
    return pods, said


GUARDED, CALL = landing_module._guarded, landing_module._call


@contextlib.asynccontextmanager
async def a_lock_never_had(*args, **kwargs):
    await asyncio.Event().wait()
    yield


@pytest.mark.slow
@pytest.mark.parametrize("way", WAYS_A_STOP_FALLS_SHORT)
async def test_a_stopped_turns_own_files_never_land_however_its_stop_falls_short(api, monkeypatch, tmp_path, caplog, way):
    pods, said = await a_stop_that_falls_short(api, monkeypatch, tmp_path, caplog, way)
    assert not (pods.project / "stopped.md").exists()
    assert "stopped.md" not in (pods.project / "seen-later.txt").read_text().split()
    if way != "a pod made again handed on again":
        # A stop not carried out on the hand-off says so, and why.
        assert "was not carried out on its hand-off" in said and "leaves the stopped turn's own files out" in said, said


@pytest.mark.slow
@pytest.mark.parametrize("way", WAYS_A_STOP_FALLS_SHORT)
async def test_no_helpers_finished_work_is_lost_however_a_stop_falls_short(api, monkeypatch, tmp_path, caplog, way):
    pods, _ = await a_stop_that_falls_short(api, monkeypatch, tmp_path, caplog, way)
    # The helper that finished before the turn, and the one the turn started, which ended after the stop.
    assert (pods.project / "sources.md").read_text() == "an hour of work\n"
    assert (pods.project / "checked.md").read_text() == "checked\n"


async def test_a_thread_whose_copy_was_made_again_after_it_handed_on_is_told_what_is_in_it_and_what_is_not(api, monkeypatch, pods):
    thread = await a_coordinating_thread(api)
    mine = SandboxPool(pods)

    async def the_pod_goes(harness):
        await mine.execute(str(thread.id), "terminal", json.dumps({"command": "echo after > after.md"}))  # after the hand-on
        await pods.destroy(mine.sandbox_of(str(thread.id)))

    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo draft > draft.md"})),
        calling(("agent", {"description": "Helper", "prompt": "Check the draft.", "run_in_background": True})),
        calling(("memory", {"action": "add", "content": "x"})),
        calling(("terminal", {"command": "ls"})),
        _final_response("Done."),
    ], pool=mine, during=the_pod_goes), 120)
    seen = json.loads((await results_of(api, thread, "terminal"))[-1].split("]\n\n", 1)[-1])["output"].split()
    # What it handed to its helper is in the copy made again; what it wrote after is not.  The line says that.
    assert "draft.md" in seen and "after.md" not in seen
    assert (await results_of(api, thread, "terminal"))[-1].startswith(
        "[This thread's copy of the project's files was made again. What this thread handed to a helper in this "
        "turn is in it, with what helpers kept since. Changes it made after that are not in it. "
        "Check the files before making any of those changes again.]\n\n"
    )


async def paused_as_the_route_pauses(api, thread) -> None:
    """The user presses Stop: the route writes the status and the event before it signals any worker."""
    store = api.app.state.session_store
    await store.update_session_status(thread.id, "paused")
    await store.emit_event(thread.id, EventType.SESSION_PAUSE, {"reason": "user"})


async def a_worker_gone_under_a_turn_that_handed_on(api, monkeypatch, tmp_path, *, dies_at="its step"):
    """A thread's turn takes up a helper's finished ``sources.md``, writes ``stopped.md`` and hands on; its worker
    then dies, with its pod and all it knew: under the step, or inside the stop the user has just pressed."""
    master, thread, pods, mine = await a_thread_whose_helper_finished_after_its_turn(api, monkeypatch, tmp_path)

    async def the_worker_dies(harness):
        if dies_at == "the stop":
            await paused_as_the_route_pauses(api, thread)

            async def killed(session):
                raise asyncio.CancelledError("the worker died inside the stop")

            monkeypatch.setattr(harness, "_take_back_what_the_turn_handed_on", killed)
            await stop(harness)
            return
        # The worker dies under the turn: its task is cancelled from outside, as a shutdown cancels it, while the
        # step still runs.
        turn.cancel()
        await asyncio.Event().wait()

    turn = asyncio.ensure_future(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo the stopped turn > stopped.md"})),
        calling(("agent", {"description": "Helper", "prompt": "Check the sources.", "run_in_background": True})),
        calling(("memory", {"action": "add", "content": "x"})),
        _final_response("Done."),
    ], pool=mine, during=the_worker_dies))
    with pytest.raises(BaseException):
        await asyncio.wait_for(turn, 120)
    async with api.app.state.session_factory() as db:
        await db.execute(text("DELETE FROM session_leases WHERE session_id = :id"), {"id": thread.id})
        await db.commit()
    await pods.destroy(mine.sandbox_of(str(thread.id)))
    landing_module.turn_ended(thread)
    return master, thread, pods


@pytest.mark.slow
@pytest.mark.parametrize("stopped", ["while no worker runs the turn", "and its worker dies inside the stop"])
async def test_a_stop_no_worker_carries_out_still_keeps_the_stopped_turns_files_out(api, monkeypatch, tmp_path, stopped):
    master, thread, pods = await a_worker_gone_under_a_turn_that_handed_on(
        api, monkeypatch, tmp_path, dies_at="its step" if stopped == "while no worker runs the turn" else "the stop",
    )
    if stopped == "while no worker runs the turn":
        await paused_as_the_route_pauses(api, thread)
    # Later the user writes again: the route makes the thread active, and a worker takes it.
    await api.app.state.session_store.update_session_status(thread.id, "active")
    await one_more_turn(api, monkeypatch, pods, thread, "ls > seen-later.txt")
    # Nothing was written for the stop but what the route wrote, and that is enough.
    assert pods.real_names() == ["Report.docx", "notes.txt", "outline.md", "seen-later.txt", "sources.md"]


async def test_a_turn_cut_off_and_taken_up_by_another_worker_is_the_same_turn_and_has_all_it_handed_on(api, monkeypatch, tmp_path):
    master, thread, pods = await a_worker_gone_under_a_turn_that_handed_on(api, monkeypatch, tmp_path)
    # No stop: the turn is taken up again, by another worker, with a pod of its own.
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "ls > seen-again.txt"})), _final_response("Done."),
    ], pool=SandboxPool(pods)), 120)
    assert {"stopped.md", "sources.md"} <= set((pods.project / "seen-again.txt").read_text().split())
    assert pods.real_names() == ["Report.docx", "notes.txt", "outline.md", "seen-again.txt", "sources.md", "stopped.md"]


async def test_a_threads_pod_is_told_the_threads_last_turn_end_and_none_at_its_first_turn(api, monkeypatch, pods):
    thread = await a_thread(api)
    store, mine, told = api.app.state.session_store, SandboxPool(pods), []
    ensure = mine.ensure

    async def watched(owner, spec):
        told.append(spec.env["HISTORY_TURN"])
        return await ensure(owner, spec)

    monkeypatch.setattr(mine, "ensure", watched)
    for n in range(2):
        await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Go on."})
        await a_turn(api, monkeypatch, thread, [
            calling(("terminal", {"command": f"echo {n} > {n}.md"})), _final_response("Done."),
        ], pool=mine)
    [first_end, _] = await store.get_events(thread.id, types=[EventType.SESSION_COMPLETE])
    # A thread with no turn end yet, new or made before turns were named, is at turn 0; then at its last end's id.
    assert set(told) == {"0", str(first_end.id)} and told[0] == "0" and told[-1] == str(first_end.id)


async def test_a_thread_whose_turn_cannot_be_named_makes_no_pod(api, monkeypatch, pods):
    thread = await a_thread(api)
    store = api.app.state.session_store

    async def the_store_does_not_answer(*args, **kwargs):
        raise ConnectionError("the database did not answer")

    looping = a_looping_harness(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo draft > draft.md"})), _final_response("Done."),
    ], pool=SandboxPool(pods))
    monkeypatch.setattr(store, "last_event", the_store_does_not_answer)
    # The turn's start fails, for its wake to be tried again: no pod is made under a name that is not known.
    with pytest.raises(ConnectionError):
        await the_loop_runs(api, looping, thread)
    assert (pods.pods, pods.copies) == ({}, {})


@pytest.mark.slow
async def test_a_stop_this_worker_could_not_write_down_is_written_before_the_threads_next_turn_here(api, monkeypatch, tmp_path, caplog):
    master, thread, pods, mine = await a_thread_whose_helper_finished_after_its_turn(api, monkeypatch, tmp_path)
    store = api.app.state.session_store
    emit = store.emit_event

    async def no_stop_is_written(session_id, type, data, *args, **kwargs):
        if type == EventType.SESSION_PAUSE:
            raise ConnectionError("the database did not answer")
        return await emit(session_id, type, data, *args, **kwargs)

    async def stopped_by_no_route(harness):
        landing_module.turn_ended(thread)  # nor is the stop carried out on the hand-off
        monkeypatch.setattr(store, "emit_event", no_stop_is_written)
        await stop(harness)

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
            calling(("terminal", {"command": "echo the stopped turn > stopped.md"})),
            calling(("agent", {"description": "Helper", "prompt": "Check the sources.", "run_in_background": True})),
            calling(("memory", {"action": "add", "content": "x"})),
            _final_response("Done."),
        ], pool=mine, during=stopped_by_no_route), 120)
    monkeypatch.setattr(store, "emit_event", emit)
    assert "Could not write that the turn of thread" in caplog.text
    assert await store.get_events(thread.id, types=[EventType.SESSION_PAUSE]) == []
    # The thread's next turn on this worker writes it first, and so is another turn: the stopped file stays out.
    await one_more_turn(api, monkeypatch, pods, thread, "ls > seen-later.txt")
    assert len(await store.get_events(thread.id, types=[EventType.SESSION_PAUSE])) == 1
    assert pods.real_names() == ["Report.docx", "notes.txt", "outline.md", "seen-later.txt", "sources.md"]


@pytest.mark.slow
@pytest.mark.parametrize("read", ["at once", "after a read that failed"])
async def test_a_stop_its_worker_could_not_write_down_is_written_before_the_threads_next_turn_on_another_worker(api, monkeypatch, tmp_path, read):
    master, thread, pods, mine = await a_thread_whose_helper_finished_after_its_turn(api, monkeypatch, tmp_path)
    store = api.app.state.session_store
    emit = store.emit_event

    async def no_stop_is_written(session_id, type, data, *args, **kwargs):
        if type == EventType.SESSION_PAUSE:
            raise ConnectionError("the database did not answer")
        return await emit(session_id, type, data, *args, **kwargs)

    async def stopped_by_no_route(harness):
        landing_module.turn_ended(thread)  # nor is the stop carried out on the hand-off
        monkeypatch.setattr(store, "emit_event", no_stop_is_written)
        await stop(harness)

    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo the stopped turn > stopped.md"})),
        calling(("agent", {"description": "Helper", "prompt": "Check the sources.", "run_in_background": True})),
        calling(("memory", {"action": "add", "content": "x"})),
        _final_response("Done."),
    ], pool=mine, during=stopped_by_no_route), 120)
    monkeypatch.setattr(store, "emit_event", emit)
    assert await store.get_events(thread.id, types=[EventType.SESSION_PAUSE]) == []
    # Kept where every worker reads it, in Redis, through which the interrupt came: its worker goes, and all it knew with it.
    redis = api.app.state.redis
    kept = f"{loop_module._STOP_NOT_WRITTEN_PREFIX}{thread.id}"
    assert await redis.get(kept) is not None
    if read == "after a read that failed":
        get, made = redis.get, dict(pods.pods)

        async def not_read(name, *args, **kwargs):
            if name == kept:
                raise ConnectionError("Redis did not answer")
            return await get(name, *args, **kwargs)

        with monkeypatch.context() as away:
            away.setattr(redis, "get", not_read)
            looping = a_looping_harness(api, away, thread, [], pool=SandboxPool(pods))
            # The turn's start fails, for its wake to be tried again: no pod is made, and nothing is written for it.
            with pytest.raises(ConnectionError):
                await the_loop_runs(api, looping, thread)
        assert (pods.pods, await store.get_events(thread.id, types=[EventType.SESSION_PAUSE])) == (made, [])
        assert await redis.get(kept) is not None
        async with api.app.state.session_factory() as db:
            await db.execute(text("DELETE FROM session_leases WHERE session_id = :id"), {"id": thread.id})
            await db.commit()
    # The thread's next turn, on another worker, writes it first, and so is another turn: the stopped file stays out.
    await one_more_turn(api, monkeypatch, pods, thread, "ls > seen-later.txt")
    assert len(await store.get_events(thread.id, types=[EventType.SESSION_PAUSE])) == 1
    assert pods.real_names() == ["Report.docx", "notes.txt", "outline.md", "seen-later.txt", "sources.md"]
    assert await redis.get(kept) is None


async def test_a_stop_that_can_be_neither_written_down_nor_kept_still_ends_its_turn_and_says_so(api, monkeypatch, pods, caplog):
    thread = await a_thread(api)
    store, redis = api.app.state.session_store, api.app.state.redis
    emit, keep = store.emit_event, redis.set

    async def no_stop_is_written(session_id, type, data, *args, **kwargs):
        if type == EventType.SESSION_PAUSE:
            raise ConnectionError("the database did not answer")
        return await emit(session_id, type, data, *args, **kwargs)

    async def nor_kept(name, *args, **kwargs):
        if str(name).startswith(loop_module._STOP_NOT_WRITTEN_PREFIX):
            raise ConnectionError("Redis did not answer")
        return await keep(name, *args, **kwargs)

    async def stopped_by_no_route(harness):
        monkeypatch.setattr(store, "emit_event", no_stop_is_written)
        monkeypatch.setattr(redis, "set", nor_kept)
        await stop(harness)

    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Draft the outline."})
    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
            calling(("terminal", {"command": "echo the stopped turn > stopped.md"})),
            calling(("memory", {"action": "add", "content": "x"})),
            _final_response("Done."),
        ], pool=SandboxPool(pods), during=stopped_by_no_route), 120)
    # The stop goes on all the same: its turn lands nothing, and its pod goes.
    assert "Nor could the stop of thread" in caplog.text
    assert (pods.real_names(), pods.pods) == (["Report.docx", "notes.txt"], {})


async def test_a_threads_coding_agent_run_has_its_pod_made_for_the_threads_turn(api, monkeypatch, pods):
    thread = await a_thread(api)
    store, pool, told = api.app.state.session_store, SandboxPool(pods), []
    ensure = pool.ensure

    async def watched(owner, spec):
        told.append(spec.env["HISTORY_TURN"])
        return await ensure(owner, spec)

    async def as_far_as_its_pod(*, ensure_sandbox, **run):
        await ensure_sandbox()  # the run's first act; no coding agent is run here
        return SimpleNamespace(status="not_connected")

    monkeypatch.setattr(pool, "ensure", watched)
    monkeypatch.setattr(coding_agent, "execute_coding_run", as_far_as_its_pod)
    monkeypatch.setattr(coding_agent, "resolve_git_pat", AsyncMock(return_value="a token"))
    # The tool is handed the turn's own config, as the harness hands it every tool: the thread's as its
    # row has it, the turn's name, and the repositories the wake put there.
    turn = thread.model_copy(update={"config": dict(thread.config)})
    await landing_module.name_turn(store, turn)
    config = {**turn.config, "repos": [{"url": "https://github.com/acme/api", "default_branch": "main"}]}
    answer = json.loads(await coding_agent._run_coding_agent_handler(
        {"agent": "claude", "prompt": "Add a line to the readme."},
        tenant=SimpleNamespace(org_id=thread.org_id, user_id=thread.user_id, service_account_id=None),
        session_id=str(thread.id), session_store=store, sandbox_pool=pool, credential_vault=object(), session_config=config,
    ))
    assert "is not connected" in answer["error"], answer
    # Its pod is the thread's, over the thread's copy, told the turn it was made in.
    assert told == [str(config["turn_after"])] and pool.holds_copy(str(thread.id))


async def test_a_threads_pod_asked_for_with_no_turns_name_is_refused_in_words(api):
    thread = await a_thread(api)
    with pytest.raises(RuntimeError, match="made for a turn of its thread, and this session carries no turn's name"):
        await tool_exec._build_session_sandbox_spec(thread, SimpleNamespace(org_id=thread.org_id, user_id=thread.user_id), str(thread.id))


async def test_a_pause_is_its_status_and_its_event_together_or_neither(api, monkeypatch):
    thread = await a_thread(api)
    store = api.app.state.session_store
    emit = store.emit_event

    async def the_route_dies_here(*args, **kwargs):
        raise ConnectionError("the route died at its write of the event")

    # A thread left paused with no turn-ending event would have its next turn named as the stopped one.
    with monkeypatch.context() as dying:
        dying.setattr(store, "emit_event", the_route_dies_here)
        with pytest.raises(ConnectionError):
            await api.client.post(f"/v1/sessions/{thread.id}/pause", headers=api.auth())
    assert (await store.get_session(thread.id)).status == "active"
    # The one write is one transaction: with its commit lost, neither is there.
    sessions = store._sf

    def a_database_that_goes_before_the_commit():
        db = sessions()

        async def gone():
            raise ConnectionError("the database went away")

        db.commit = gone
        return db

    with monkeypatch.context() as down:
        down.setattr(store, "_sf", a_database_that_goes_before_the_commit)
        with pytest.raises(ConnectionError):
            await emit(thread.id, EventType.SESSION_PAUSE, {}, status="paused")
    assert (await store.get_session(thread.id)).status == "active"
    assert await store.get_events(thread.id, types=[EventType.SESSION_PAUSE]) == []
    # And whole, through the route.
    paused = await api.client.post(f"/v1/sessions/{thread.id}/pause", headers=api.auth())
    assert paused.status_code == 200, paused.text
    assert (await store.get_session(thread.id)).status == "paused"
    assert len(await store.get_events(thread.id, types=[EventType.SESSION_PAUSE])) == 1
    # An archived session stays archived: the event's status is not written over a deletion.
    await store.update_session_status(thread.id, "archived")
    await emit(thread.id, EventType.SESSION_PAUSE, {}, status="paused")
    assert (await store.get_session(thread.id)).status == "archived"


async def test_a_stop_kept_to_be_written_is_let_go_once_the_thread_has_a_later_turn_end_and_is_kept_no_longer_than_a_hand_off(api, monkeypatch, pods):
    thread = await a_thread(api)
    store, redis = api.app.state.session_store, api.app.state.redis
    looping = a_looping_harness(api, monkeypatch, thread, [_final_response("Done.")], pool=SandboxPool(pods))
    # A stop of the thread's first turn that its worker could not write down.
    await loop_module._keep_to_write(redis, thread.id, 0, {"reason": "interrupted"})
    kept = f"{loop_module._STOP_NOT_WRITTEN_PREFIX}{thread.id}"
    # Kept no longer than the history keeps a resolved thread's hand-off, which it protects.
    assert 0 < await redis.ttl(kept) <= PRUNE_DAYS * 86_400
    # The thread's turn ends otherwise meanwhile, written by another worker or a route.
    await store.emit_event(thread.id, EventType.SESSION_FAIL, {"reason": "provider_error"})
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Go on."})
    await the_loop_runs(api, looping, thread)
    # The thread has a later turn end than the stop: no late pause is added to its log.
    assert await redis.get(kept) is None
    assert await store.get_events(thread.id, types=[EventType.SESSION_PAUSE]) == []
