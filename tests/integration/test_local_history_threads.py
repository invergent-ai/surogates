"""A project's thread bound with a copy of its own on the user's computer: its bind, and where its work goes."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from surogates.devices.binding import Binding, copy_of
from surogates.devices.operations import OperationRequest
from surogates.devices.workspace import DeviceOperationError
from surogates.session.provisioning import create_child_session
from surogates.workstreams.threads import make_thread

from .test_device_sessions import is_bound
from .test_devices import FOLDER, NONCE, api, binding, eventually, link_url, register  # noqa: F401  (api and link_url are fixtures)
from .test_local_threads import begin, confirmed, device_card, journal, laptop, made_local  # noqa: F401  (laptop is a fixture)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def made_with_copy(api, device_id: str) -> tuple[dict, object, object]:
    """A project, its master and a thread made on the computer *device_id*, marked as one that works in a copy of its own.

    As the route that makes a thread will mark one: its ``execution`` names
    the thread whose copy it works in, which is itself.  Not bound yet.
    """
    project, master, proposal_id = await device_card(api)
    store = api.app.state.session_store
    thread = await make_thread(
        session_store=store, session_factory=api.app.state.session_factory, master=master, live_config=None,
        title="Check the totals", device=SimpleNamespace(id=UUID(device_id), name="Flavius's ThinkPad"), folder=FOLDER,
        card={"proposal_id": proposal_id, "key": "2"},
    )
    await store.update_session_config_key(
        thread.id, "execution", {**thread.config["execution"], "history": {"thread": str(thread.id)}},
    )
    return project, master, await store.get_session(thread.id)


async def bind_with_copy(api, thread) -> None:
    """Ask *thread*'s computer to bind it to a copy of its own, as the route that makes a thread will."""
    await journal(api).bind(
        session_id=thread.id, device_id=UUID(thread.config["execution"]["device_id"]), folder=FOLDER, nonce=NONCE,
        history=thread.id,
    )


async def test_no_bind_the_routes_send_names_a_copy(api):
    device = await register(api)
    project, _, proposal_id = await device_card(api)
    made = await api.client.post(
        f"/v1/workstreams/{project['id']}/threads",
        json={"proposal_id": proposal_id, "key": "2", "execution": confirmed(device["id"])}, headers=api.auth(),
    )
    assert made.status_code == 201, made.text
    chat = await api.client.post("/v1/sessions", json={"execution": confirmed(device["id"])}, headers=api.auth())
    assert chat.status_code == 201, chat.text
    binds = await journal(api).pending(UUID(device["id"]), 1)
    # A thread and a chat, each bound to the folder itself: no route asks a computer for a copy.
    assert sorted(str(bind.root_session_id) for bind in binds) == sorted([made.json()["thread_id"], chat.json()["id"]])
    assert [(bind.kind, bind.args) for bind in binds] == [("bind", {"folder": FOLDER, "nonce": NONCE})] * 2
    for session_id in (made.json()["thread_id"], chat.json()["id"]):
        assert copy_of((await api.app.state.session_store.get_session(UUID(session_id))).config) is None


async def test_a_bind_that_names_the_threads_copy_carries_it_and_the_thread_says_whose_copy_it_works_in(api):
    device = await register(api)
    _, _, thread = await made_with_copy(api, device["id"])
    await bind_with_copy(api, thread)
    [bind] = await journal(api).pending(UUID(device["id"]), 1)
    assert (bind.kind, bind.root_session_id, bind.args) == (
        "bind", thread.id, {"folder": FOLDER, "nonce": NONCE, "history": {"thread": str(thread.id)}},
    )
    assert await binding(api, thread.id) == Binding("pending")
    # What a worker reads: the thread works in its own copy, and so does a session made under it.
    assert copy_of(thread.config) == thread.id
    helper = await create_child_session(store=api.app.state.session_store, parent=thread, channel="worker")
    assert copy_of(helper.config) == thread.id


async def test_a_root_is_bound_with_a_copy_only_as_the_server_marked_it(api):
    device = await register(api)
    _, _, marked = await made_with_copy(api, device["id"])
    _, _, _, plain_id = await plain_thread(api, device["id"])
    ops = journal(api)
    bound = {"device_id": UUID(device["id"]), "folder": FOLDER, "nonce": NONCE}
    for case, (session_id, history) in {
        "a thread marked for a copy, bound to the folder itself": (marked.id, None),
        "a thread marked for a copy, bound to another's": (marked.id, uuid4()),
        "a thread bound to its folder, asked for a copy": (UUID(plain_id), UUID(plain_id)),
    }.items():
        with pytest.raises(ValueError, match="A root is bound with a copy only where the server made it a thread that works in one"):
            await ops.bind(session_id=session_id, history=history, **bound)
            pytest.fail(case)
    # The route's own bind of the plain thread stands as it was, and nothing was recorded for the marked one.
    [bind] = await ops.pending(UUID(device["id"]), 1)
    assert (bind.root_session_id, bind.args) == (UUID(plain_id), {"folder": FOLDER, "nonce": NONCE})
    assert await binding(api, marked.id) == Binding("failed", "The computer was never asked to set it up")


async def answered_bind(api, thread, outcome: dict) -> None:
    """*thread*'s computer answers its bind with *outcome*."""
    ops = journal(api)
    device_id = UUID(thread.config["execution"]["device_id"])
    [bind] = await ops.pending(device_id, 1)
    assert await ops.complete(device_id, 1, bind.id, bind.digest, outcome) == "completed"


KEEPS_NO_COPY = (
    "This chat's folder could not be set up: This computer keeps no copy of the folder for a thread: "
    "update Surogate Desktop. Start a new chat."
)


@pytest.mark.parametrize("case, outcome", [
    # As an app older than copies answers: it bound the thread to the folder itself.
    ("the folder itself", {"ok": None}),
    ("another thread's copy", {"ok": {"history": {"thread": "0f6d1c5e-7a3b-4c2d-9e1f-0a1b2c3d4e5f"}}}),
    ("something else", {"ok": {"history": {"thread": None}, "folder": FOLDER}}),
])
async def test_a_thread_whose_computer_does_not_say_it_keeps_the_copy_works_nowhere_and_says_so(api, case, outcome):
    device = await register(api)
    project, _, thread = await made_with_copy(api, device["id"])
    await bind_with_copy(api, thread)
    await answered_bind(api, thread, outcome)
    assert await binding(api, thread.id) == Binding(
        "failed", "This computer keeps no copy of the folder for a thread: update Surogate Desktop",
    ), case
    # Its user is told, and it never begins.
    refused = await begin(api, project, str(thread.id))
    assert (refused.status_code, refused.json()["detail"]) == (409, KEEPS_NO_COPY)
    # Nothing of the thread's reaches its computer: no file operation, no command, and none of its own kinds.
    ops = journal(api)
    for kind, args in [("write", {"key": f"{FOLDER}/a.txt", "data": ""}), ("run", {"command": "ls"}), ("history", {"action": "open"})]:
        with pytest.raises(DeviceOperationError, match="This session's folder is not set up on this computer yet"):
            await ops.run(OperationRequest(
                device_id=UUID(device["id"]), root_session_id=thread.id, calling_session_id=thread.id,
                invocation_id="open:0" if kind == "history" else "1", ordinal=1, kind=kind, args=args,
            ))
    assert await ops.pending(UUID(device["id"]), 1) == []


async def test_a_thread_whose_computer_says_it_keeps_the_copy_is_bound(api):
    device = await register(api)
    _, _, thread = await made_with_copy(api, device["id"])
    await bind_with_copy(api, thread)
    await answered_bind(api, thread, {"ok": {"history": {"thread": str(thread.id)}}})
    assert await binding(api, thread.id) == Binding("bound")


async def test_a_chat_on_the_folder_itself_is_bound_whatever_its_computer_says_beside_yes(api):
    device = await register(api)
    _, _, _, thread_id = await plain_thread(api, device["id"])
    thread = await api.app.state.session_store.get_session(UUID(thread_id))
    # As on master: a binding is its computer's yes, and nothing it says beside it is read.
    await answered_bind(api, thread, {"ok": {"history": {"thread": thread_id}}})
    assert await binding(api, thread.id) == Binding("bound")


async def plain_thread(api, device_id: str) -> tuple[dict, object, str, str]:
    """A thread made by the route on the computer *device_id*: bound to its folder itself."""
    return await made_local(api, SimpleNamespace(device_id=device_id))
