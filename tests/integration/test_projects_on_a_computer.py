"""A project made in Surogate Desktop: it works in a folder of that computer.

Its coordinator works in the folder itself, with a chat's tools there, and
every piece of work is a thread on the folder, each in a copy of its own,
which only the user starts, there.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import delete, select

from surogates.db.models import Device, DeviceOperation, Session, Workstream
from surogates.devices.binding import Binding
from surogates.devices.store import DeviceStore
from surogates.workstreams.store import WorkstreamStore
from surogates.session.events import EventType
from surogates.harness.agent_news import NEW_FORMAT
from surogates.harness.loop_context_replay import worker_news

from .test_devices import (  # noqa: F401  (api is a fixture)
    AGENT_ID,
    FOLDER,
    NONCE,
    add_user,
    api,
    binding,
    register,
)
from .test_local_threads import confirmed, held, journal
from .test_workstream_threads import children_of, events_of, queued
from .test_workstreams import create, master_of, system_prompt, turn_calling

pytestmark = pytest.mark.asyncio(loop_scope="session")

COMPUTER = "Flavius's ThinkPad"


async def on_computer(api) -> tuple[dict, dict]:
    """A project made on the user's computer, in the folder they chose there, and that computer."""
    device = await register(api)
    return await create(api, execution=confirmed(device["id"])), device


async def test_a_project_made_on_a_computer_works_in_the_folder_its_user_chose_there(api):
    project, device = await on_computer(api)
    assert project["place"] == {"kind": "device", "device_id": device["id"], "device_name": COMPUTER, "online": False, "lost": False}
    assert project["folder"] == FOLDER
    async with api.app.state.session_factory() as db:
        row = await db.scalar(select(Workstream).where(Workstream.id == UUID(project["id"])))
    assert (row.device_id, row.folder) == (UUID(device["id"]), FOLDER)
    got = await api.client.get(f"/v1/workstreams/{project['id']}", headers=api.auth())
    assert got.json() == project

    # Its coordinator works in the folder itself, never in a copy of it: its computer is asked to bind it there.
    master = await master_of(api, project)
    assert master.config["execution"] == {"kind": "device", "device_id": device["id"], "device_name": COMPUTER}
    assert master.config["workspace_path"] == FOLDER
    assert (master.config["workstream_role"], master.config["strict_coordinator"]) == ("coordinator", True)
    assert await binding(api, master.id) == Binding("pending")
    [bind] = await journal(api).pending(UUID(device["id"]), 1)
    assert (bind.kind, bind.root_session_id, bind.args) == ("bind", master.id, {"folder": FOLDER, "nonce": NONCE})


async def test_a_project_made_in_the_web_app_stays_in_the_cloud(api):
    device = await register(api)
    project = await create(api)
    assert (project["place"], project["folder"]) == ({"kind": "cloud"}, None)
    assert "execution" not in (await master_of(api, project)).config
    assert await journal(api).pending(UUID(device["id"]), 1) == []


async def test_a_projects_computer_is_said_online_while_the_agent_hears_it(api, monkeypatch):
    from surogates.devices.presence import DevicePresence

    project, device = await on_computer(api)

    async def online(self, devices):
        return set(devices)

    monkeypatch.setattr(DevicePresence, "online", online)
    got = await api.client.get(f"/v1/workstreams/{project['id']}", headers=api.auth())
    assert got.json()["place"]["online"] is True


async def test_a_projects_folder_is_only_on_the_users_own_live_computer(api, session_factory):
    _, their_token = await add_user(session_factory, api.org_id)
    theirs = await register(api, token=their_token)
    elsewhere = await DeviceStore(session_factory).create(
        org_id=api.org_id, agent_id="another-agent", user_id=api.user_id, name="Elsewhere",
    )
    revoked = await register(api)
    await api.client.delete(f"/v1/devices/{revoked['id']}", headers=api.auth())
    mine = await register(api)
    for case, (execution, status) in {
        "another user's computer": (confirmed(theirs["id"]), 404),
        "another agent's computer": (confirmed(str(elsewhere.device.id)), 404),
        "a revoked computer": (confirmed(revoked["id"]), 409),
        "a nonce that is no nonce": (confirmed(mine["id"], nonce="short"), 422),
        "a folder with a NUL": (confirmed(mine["id"], folder="/home/fl\x00avius"), 422),
        "no folder": (confirmed(mine["id"], folder=""), 422),
    }.items():
        response = await api.client.post(
            "/v1/workstreams", json={"name": "Quarterly report", "execution": execution}, headers=api.auth(),
        )
        assert response.status_code == status, case
    # Nothing was made, and no computer was asked for a folder.
    async with session_factory() as db:
        assert (await db.scalars(select(Session.id).where(Session.user_id == api.user_id))).all() == []
    for device_id in (theirs["id"], mine["id"]):
        assert await journal(api).pending(UUID(device_id), 1) == []


async def test_a_computer_projects_coordinator_has_a_chats_tools_on_its_folder_and_the_family(api, monkeypatch):
    project, _ = await on_computer(api)
    sent, _, _ = await turn_calling(monkeypatch, await master_of(api, project), {})
    # It reads, writes and changes files, and runs commands, on the computer, and starts its threads.
    assert {
        "read_file", "list_files", "search_files", "write_file", "patch", "terminal", "process",
        "agent", "send_message", "list_agents", "stop_agent",
    } <= sent
    # Every piece of work is a thread on the folder.
    assert not sent & {"web_search", "browser_navigate", "create_artifact", "run_coding_agent"}


async def a_waiting_thread(api) -> tuple[dict, dict, object, object]:
    """A computer project whose master, bound to its folder there, has just started a thread: the project, its
    computer, its master and the thread, which waits for that computer."""
    project, device = await on_computer(api)
    await answer_bind(api, device)
    master = await master_of(api, project)
    answer = await held(api, master, "agent", description="Draft A", prompt="Draft the A memo as A.docx.")
    assert (answer["status"], answer["description"]) == ("waiting", "Draft A"), answer
    assert COMPUTER in answer["note"], answer
    return project, device, master, await api.app.state.session_store.get_session(UUID(answer["agent_id"]))


async def test_a_master_starts_a_thread_on_the_projects_computer(api):
    project, device, master, thread = await a_waiting_thread(api)
    # In a copy of its own of the project's folder, as every computer thread works.
    assert (thread.parent_id, thread.config["execution"]["history"], thread.config["workspace_path"]) == (
        master.id, {"thread": str(thread.id)}, FOLDER,
    )
    # The computer is asked to set that copy up for the thread, in the master's name: no page confirmed it.
    [bind] = await journal(api).pending(UUID(device["id"]), 1)
    nonce = bind.args.pop("nonce")
    assert isinstance(nonce, str) and len(nonce) >= 32
    assert (bind.kind, bind.root_session_id, bind.args) == (
        "bind", thread.id, {"folder": FOLDER, "history": {"thread": str(thread.id)}, "master": str(master.id)},
    )
    # Not begun: no goal, no news for the master, nothing runs it, and no row lists it.
    assert await events_of(api, thread.id) == []
    assert await events_of(api, master.id, EventType.WORKER_SPAWNED) == []
    assert not await queued(api, thread)
    assert (await api.client.get(f"/v1/workstreams/{project['id']}/threads", headers=api.auth())).json() == []


async def test_the_thread_begins_once_its_computer_binds_it(api):
    project, device, master, thread = await a_waiting_thread(api)
    ops = journal(api)
    [bind] = await ops.pending(UUID(device["id"]), 1)
    bound = {"ok": {"history": {"thread": str(thread.id)}}}
    # A reconnect sends the answer again: the thread begins once.
    assert [await ops.complete(UUID(device["id"]), 1, bind.id, bind.digest, bound) for _ in range(2)] == [
        "completed", "duplicate",
    ]
    [goal] = await events_of(api, thread.id, EventType.USER_MESSAGE)
    assert goal.data == {"content": "Draft the A memo as A.docx."}
    [spawned] = await events_of(api, master.id, EventType.WORKER_SPAWNED)
    assert spawned.data == {
        "worker_id": str(thread.id), "title": "Draft A", "goal": "Draft the A memo as A.docx.", "bound": True,
        "format": NEW_FORMAT,
    }
    # The master hears it started, at its next model request.
    heard = worker_news(spawned.type, spawned.data)["content"]
    assert '<status>started</status>' in heard and '"Draft A"' in heard, heard
    assert await queued(api, thread)
    listed = (await api.client.get(f"/v1/workstreams/{project['id']}/threads", headers=api.auth())).json()
    assert [row["id"] for row in listed] == [str(thread.id)]


async def test_two_threads_started_while_the_computer_is_away_each_begin_once_it_answers(api):
    project, device, master, one = await a_waiting_thread(api)
    second = await held(api, master, "agent", description="Check the totals", prompt="Check the totals in Budget.xlsx.")
    two = await api.app.state.session_store.get_session(UUID(second["agent_id"]))
    # The computer is back: it answers both binds, in whichever order it took them.
    ops = journal(api)
    binds = await ops.pending(UUID(device["id"]), 1)
    assert sorted(bind.root_session_id for bind in binds) == sorted([one.id, two.id])
    for bind in reversed(binds):
        bound = {"ok": {"history": {"thread": str(bind.root_session_id)}}}
        assert await ops.complete(UUID(device["id"]), 1, bind.id, bind.digest, bound) == "completed"
    goals = {thread.id: (await events_of(api, thread.id, EventType.USER_MESSAGE)) for thread in (one, two)}
    assert {key: [e.data["content"] for e in events] for key, events in goals.items()} == {
        one.id: ["Draft the A memo as A.docx."], two.id: ["Check the totals in Budget.xlsx."],
    }
    spawned = await events_of(api, master.id, EventType.WORKER_SPAWNED)
    assert sorted(e.data["worker_id"] for e in spawned) == sorted([str(one.id), str(two.id)])
    assert all(e.data["bound"] for e in spawned)
    assert await queued(api, one) and await queued(api, two)


async def test_a_refused_bind_tells_the_master_the_thread_failed(api):
    _, device, master, thread = await a_waiting_thread(api)
    refusal = "This folder was not confirmed on this computer"
    await answer_bind(api, device, {"error": {"type": "binding", "message": refusal}})
    [failed] = await events_of(api, master.id, EventType.WORKER_FAILED)
    assert (failed.data["worker_id"], failed.data["title"], failed.data["format"]) == (str(thread.id), "Draft A", NEW_FORMAT)
    assert COMPUTER in failed.data["error"] and refusal in failed.data["error"], failed.data
    assert "update Surogate Desktop" in failed.data["error"], failed.data
    # Nothing begins, and the master is woken to hear it.
    assert (await api.app.state.session_store.get_session(thread.id)).status == "archived"
    assert await events_of(api, thread.id) == []
    assert not await queued(api, thread) and await queued(api, master)


async def test_a_thread_its_computer_binds_to_the_folder_itself_never_begins(api):
    # An app older than copies binds the folder itself: the thread would work in the project's folder, not a copy.
    _, device, master, thread = await a_waiting_thread(api)
    await answer_bind(api, device, {"ok": None})
    [failed] = await events_of(api, master.id, EventType.WORKER_FAILED)
    assert "This computer keeps no copy of the folder for a thread: update Surogate Desktop" in failed.data["error"]
    assert (await api.app.state.session_store.get_session(thread.id)).status == "archived"
    assert await events_of(api, thread.id) == [] and await events_of(api, master.id, EventType.WORKER_SPAWNED) == []


async def test_a_master_whose_projects_folder_is_lost_starts_no_thread(api):
    project, device = await on_computer(api)
    await answer_bind(api, device, {"error": {"type": "binding", "message": "The folder is gone"}})
    master = await master_of(api, project)
    refused = await held(api, master, "agent", description="Draft A", prompt="Draft the A memo as A.docx.")
    assert "Use this computer for this project" in refused["error"], refused
    assert await children_of(api, master) == []


async def test_a_computer_projects_prompt_has_it_work_in_its_folder_and_start_its_threads_there(api):
    project, _ = await on_computer(api)
    prompt = await system_prompt(api, await master_of(api, project))
    said = " ".join(prompt.split())
    assert "# Running a project" in said
    assert (
        "Do a simple thing yourself, here: a quick answer, a plain text file whose whole content the user gave you, "
        "writing a few lines, a small edit, one quick command."
    ) in said
    assert "## The project's folder" in said
    assert "Your tools read, write and run commands in the folder itself, on that computer." in said
    assert "tell the user you cannot reach the folder until the computer is back." in said
    assert (
        "Should they say its access was revoked or the folder is no longer there, tell the user to open the project in "
        "Surogate Desktop on the computer it should work on and choose Use this computer for this project."
    ) in said
    assert (
        "Give a thread's work to a thread on the project's folder with `agent`: the `description` is the thread's "
        "title, the `prompt` its goal."
    ) in said
    assert "A thread starts once that computer sets up its copy: at once while the computer is online, or when it is back." in said
    # It is never told of a thread in the cloud, or of proposing one.
    assert "## Starting threads" not in said
    assert "propose" not in said.lower()


# -- Using this computer for a project whose own can no longer reach its folder ------------------------------------

MOVED_TO = "/home/flavius/notes"


def online(monkeypatch, *devices: dict) -> None:
    """The agent hears *devices* now, and no other computer."""
    from surogates.devices.presence import DevicePresence

    heard = {UUID(device["id"]) for device in devices}

    async def answer(self, asked):
        return {device for device in asked if device in heard}

    monkeypatch.setattr(DevicePresence, "online", answer)


async def use_here(api, project: dict, execution: dict, token: str | None = None):
    return await api.client.post(
        f"/v1/workstreams/{project['id']}/computer", json={"execution": execution}, headers=api.auth(token),
    )


async def answer_bind(api, device: dict, outcome: dict | None = None) -> None:
    """The computer answers the bind it was sent, as its app does: bound, unless *outcome* says otherwise."""
    ops = journal(api)
    [bind] = await ops.pending(UUID(device["id"]), 1)
    assert await ops.complete(UUID(device["id"]), 1, bind.id, bind.digest, outcome or {"ok": None}) == "completed"


async def revoke(api, device: dict) -> None:
    assert (await api.client.delete(f"/v1/devices/{device['id']}", headers=api.auth())).status_code == 204


async def project_of(api, project: dict) -> dict:
    return (await api.client.get(f"/v1/workstreams/{project['id']}", headers=api.auth())).json()


async def test_a_project_whose_computer_was_revoked_moves_to_the_users_computer_here_with_the_folder_chosen_again(
    api, monkeypatch,
):
    project, laptop = await on_computer(api)
    await answer_bind(api, laptop)
    master = await master_of(api, project)
    old = await held(api, master, "agent", description="Draft A", prompt="Draft the A memo as A.docx.")
    await answer_bind(api, laptop, {"ok": {"history": {"thread": old["agent_id"]}}})
    # Logged out there: its access ends, and this computer joins the agent again under another id, by the same name.
    await revoke(api, laptop)
    here = await register(api)
    online(monkeypatch, here)
    assert (await project_of(api, project))["place"]["lost"] is True

    moved = await use_here(api, project, confirmed(here["id"], folder=MOVED_TO))
    assert moved.status_code == 200, moved.text
    place = {"kind": "device", "device_id": here["id"], "device_name": COMPUTER, "online": True, "lost": False}
    assert (moved.json()["place"], moved.json()["folder"]) == (place, MOVED_TO)
    assert await project_of(api, project) == moved.json()
    async with api.app.state.session_factory() as db:
        row = await db.scalar(select(Workstream).where(Workstream.id == UUID(project["id"])))
    assert (row.device_id, row.folder) == (UUID(here["id"]), MOVED_TO)
    # Its coordinator works in the folder chosen here, which this computer is asked to bind afresh.
    master = await master_of(api, project)
    assert master.config["execution"] == {"kind": "device", "device_id": here["id"], "device_name": COMPUTER}
    assert master.config["workspace_path"] == MOVED_TO
    assert await binding(api, master.id) == Binding("pending")
    [bind] = await journal(api).pending(UUID(here["id"]), 1)
    assert (bind.kind, bind.root_session_id, bind.args) == ("bind", master.id, {"folder": MOVED_TO, "nonce": NONCE})
    await answer_bind(api, here)
    assert await binding(api, master.id) == Binding("bound")

    # The thread started before stays with its copy on the old computer, and can be resolved.
    old_thread = await api.app.state.session_store.get_session(UUID(old["agent_id"]))
    assert old_thread.config["execution"]["device_id"] == laptop["id"]
    resolved = await api.client.post(
        f"/v1/workstreams/{project['id']}/threads/{old_thread.id}/resolve", headers=api.auth(),
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["group"] == "resolved"
    # A new one starts in the project's folder here, and nowhere else.
    made = await held(api, master, "agent", description="Check the totals", prompt="Check the totals in Budget.xlsx.")
    thread = await api.app.state.session_store.get_session(UUID(made["agent_id"]))
    assert (thread.config["execution"]["device_id"], thread.config["workspace_path"]) == (here["id"], MOVED_TO)
    [bind] = await journal(api).pending(UUID(here["id"]), 1)
    assert (bind.root_session_id, bind.args["folder"], bind.args["master"]) == (thread.id, MOVED_TO, str(master.id))


async def test_a_project_moves_to_this_computer_once_its_own_is_gone_or_could_not_set_its_folder_up_or_is_this_one(
    api, monkeypatch, session_factory,
):
    for case in ("gone", "could not set it up", "never asked to", "this computer"):
        project, laptop = await on_computer(api)
        if case == "never asked to":
            # As a binding whose recording failed: no computer will ever answer it.
            async with session_factory() as db:
                await db.execute(delete(DeviceOperation).where(
                    DeviceOperation.calling_session_id == UUID(project["master_session_id"]),
                ))
                await db.commit()
        if case == "gone":
            await answer_bind(api, laptop)
            async with session_factory() as db:
                await db.execute(delete(Device).where(Device.id == UUID(laptop["id"])))
                await db.commit()
        if case == "could not set it up":
            await answer_bind(api, laptop, {"error": {"type": "binding", "message": "This computer could not record the folder"}})
        if case != "this computer":
            assert (await project_of(api, project))["place"]["lost"] is True, case
        here = laptop if case == "this computer" else await register(api)
        online(monkeypatch, here)
        moved = await use_here(api, project, confirmed(here["id"], folder=MOVED_TO))
        assert moved.status_code == 200, (case, moved.text)
        assert (moved.json()["place"]["device_id"], moved.json()["folder"]) == (here["id"], MOVED_TO), case
        assert await binding(api, UUID(project["master_session_id"])) == Binding("pending"), case


async def test_a_project_moves_only_for_its_own_user_from_their_own_live_computer_here_and_never_from_one_that_reaches_it(
    api, monkeypatch, session_factory,
):
    project, laptop = await on_computer(api)
    await answer_bind(api, laptop)
    _, their_token = await add_user(session_factory, api.org_id)
    theirs = await register(api, token=their_token)
    revoked = await register(api)
    await revoke(api, revoked)
    asleep = await register(api, name="Flavius's desktop")
    desktop = await register(api, name="Flavius's desktop")
    cloud = await create(api)
    online(monkeypatch, theirs, revoked, desktop)
    for case, (asked, execution, token, said) in {
        "by another user": (project, confirmed(theirs["id"]), their_token, (404, "No such project.")),
        "to another user's computer": (project, confirmed(theirs["id"]), None, (404, "Unknown device.")),
        "to a revoked computer": (project, confirmed(revoked["id"]), None, (409, "Local access to this computer was revoked.")),
        "to a computer the agent does not hear": (
            project, confirmed(asleep["id"]), None,
            (409, "Flavius's desktop is not connected to the agent right now: use it for this project once it is."),
        ),
        "away from a live computer that reaches its folder": (
            project, confirmed(desktop["id"]), None,
            (409, f"This project works in a folder on {COMPUTER}, which still reaches it: start its threads there, "
                  f"or remove {COMPUTER} from your computers first."),
        ),
        "a project in the cloud": (cloud, confirmed(desktop["id"]), None, (409, "This project works in the cloud.")),
        "with a nonce that is no nonce": (project, confirmed(desktop["id"], nonce="short"), None, (422, None)),
    }.items():
        response = await use_here(api, asked, execution, token)
        detail = response.json()["detail"] if said[1] is not None else None
        assert (response.status_code, detail) == said, case
    # Nothing moved, and no computer was asked for a folder.
    after = await project_of(api, project)
    assert (after["place"]["device_id"], after["folder"]) == (laptop["id"], FOLDER)
    assert (await master_of(api, project)).config["execution"]["device_id"] == laptop["id"]
    assert await binding(api, UUID(project["master_session_id"])) == Binding("bound")
    for device in (theirs, revoked, asleep, desktop):
        assert await journal(api).pending(UUID(device["id"]), 1) == [], device["name"]


async def test_a_project_never_moves_away_from_a_computer_that_is_not_its_users_own(api, monkeypatch, session_factory):
    project, _ = await on_computer(api)
    _, their_token = await add_user(session_factory, api.org_id)
    theirs = await register(api, token=their_token)
    await api.client.delete(f"/v1/devices/{theirs['id']}", headers=api.auth(their_token))
    # Its row names another user's computer, revoked: not the user's to move away from.
    async with session_factory() as db:
        row = await db.get(Workstream, UUID(project["id"]))
        row.device_id = UUID(theirs["id"])
        await db.commit()
    here = await register(api)
    online(monkeypatch, here)
    response = await use_here(api, project, confirmed(here["id"], folder=MOVED_TO))
    assert response.status_code == 409, response.text
    assert await journal(api).pending(UUID(here["id"]), 1) == []


async def test_a_project_moved_meanwhile_is_not_moved_again(api, session_factory):
    project, laptop = await on_computer(api)
    here = await register(api)
    store = WorkstreamStore(session_factory)
    owner = {"org_id": api.org_id, "agent_id": AGENT_ID, "user_id": api.user_id}
    moved = await store.move(
        UUID(project["id"]), away_from=UUID(here["id"]), device_id=UUID(here["id"]), device_name=COMPUTER, folder=MOVED_TO,
        **owner,
    )
    assert moved is None
    after = await project_of(api, project)
    assert (after["place"]["device_id"], after["folder"]) == (laptop["id"], FOLDER)
    assert await binding(api, UUID(project["master_session_id"])) == Binding("pending")


async def test_a_master_whose_computer_was_revoked_starts_no_thread(api):
    project, laptop = await on_computer(api)
    await answer_bind(api, laptop)
    master = await master_of(api, project)
    await revoke(api, laptop)
    refused = await held(api, master, "agent", description="Draft A", prompt="Draft the A memo as A.docx.")
    assert refused == {"error": (
        f"This project's folder can no longer be reached on {COMPUTER}: tell the user to open the project in "
        "Surogate Desktop on the computer it should work on and choose Use this computer for this project."
    )}
    assert await children_of(api, master) == []


async def test_the_projects_list_says_where_each_project_works(api):
    project, laptop = await on_computer(api)
    cloud = await create(api, name="Budget")
    listed = {row["id"]: row["place"] for row in (await api.client.get("/v1/workstreams", headers=api.auth())).json()}
    assert listed == {
        project["id"]: {"kind": "device", "device_id": laptop["id"], "device_name": COMPUTER, "online": False, "lost": False},
        cloud["id"]: {"kind": "cloud"},
    }
