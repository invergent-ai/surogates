"""session_files for a chat on a folder of the user's computer: its files are reached only through that computer."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import func, select

from surogates.api.session_guards import require_device_access
from surogates.db.models import DeviceOperation, DeviceTransfer
from surogates.devices.operations import REQUEST_PREFIX, OperationConflict
from surogates.devices.presence import DevicePresence
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.session.files import ComputerAway, DeviceAccess, session_files
from surogates.session.provisioning import create_child_session
from surogates.session.store import SessionStore

from .test_devices import (  # noqa: F401  (fixtures)
    api,
    eventually,
    has_pending,
    laptop_rig,
    link_url,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def files_of(api, session, **kwargs):
    state = api.app.state
    return session_files(
        session, storage=state.storage, session_factory=state.session_factory, redis=state.redis, **kwargs,
    )


async def access_of(api, session) -> DeviceAccess:
    """What the routes' one check gives the chat's own user, before any change of theirs is claimed."""
    caller = SimpleNamespace(user_id=session.user_id, service_account_id=None)
    return await require_device_access(SimpleNamespace(app=api.app), session, caller)


async def is_online(api, device_id: UUID) -> bool:
    return device_id in await DevicePresence(api.app.state.redis).online([device_id])


async def test_a_chats_files_are_its_computers_folder(api, laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    session = await SessionStore(api.app.state.session_factory).get_session(rig.root)
    (rig.folder / "notes.md").write_text("hello")
    async with files_of(api, session, request_id="look-0001") as files:
        assert isinstance(files, DeviceWorkspaceIO)
        assert await asyncio.wait_for(files.read(await files.resolve("notes.md")), 5.0) == b"hello"
    async with api.app.state.session_factory() as db:
        rows = (await db.execute(
            select(DeviceOperation.invocation_id, DeviceOperation.ordinal, DeviceOperation.kind)
            .where(DeviceOperation.calling_session_id == rig.root, DeviceOperation.root_session_id == rig.root)
            .where(DeviceOperation.invocation_id.startswith(REQUEST_PREFIX))
            .order_by(DeviceOperation.ordinal)
        )).all()
    # One request, outside any tool call, its operations numbered in order.
    assert rows == [(f"{REQUEST_PREFIX}look-0001", 1, "resolve"), (f"{REQUEST_PREFIX}look-0001", 2, "read")]


async def test_what_a_request_read_in_a_transfer_is_consumed_once_it_ends(api, laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    session = await SessionStore(api.app.state.session_factory).get_session(rig.root)
    data = bytes(range(256)) * 8192  # 2 MiB: a transfer, not one frame
    (rig.folder / "big.bin").write_bytes(data)
    async with files_of(api, session, request_id="read-big-0001") as files:
        assert await asyncio.wait_for(files.read(await files.resolve("big.bin")), 10.0) == data
    async with api.app.state.session_factory() as db:
        consumed = (await db.execute(
            select(DeviceTransfer.consumed_at)
            .join(DeviceOperation, DeviceOperation.id == DeviceTransfer.operation_id)
            .where(DeviceOperation.invocation_id == f"{REQUEST_PREFIX}read-big-0001")
        )).scalars().all()
    assert len(consumed) == 1 and consumed[0] is not None


async def test_a_sub_agents_files_are_its_roots_folder(api, laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    store = SessionStore(api.app.state.session_factory)
    child = await create_child_session(store=store, parent=await store.get_session(rig.root), channel="worker")
    (rig.folder / "notes.md").write_text("from the root's folder")
    async with files_of(api, child) as files:
        assert await asyncio.wait_for(files.read(await files.resolve("notes.md")), 5.0) == b"from the root's folder"


async def test_an_offline_computer_is_said_by_name(api, laptop_rig):
    session = await SessionStore(api.app.state.session_factory).get_session(laptop_rig.root)
    with pytest.raises(ComputerAway) as away:
        async with files_of(api, session):
            pass
    assert (away.value.revoked, str(away.value)) == (False, "The files are on Flavius's ThinkPad, which is offline")


async def test_a_revoked_computer_is_said_by_name(api, laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    revoked = await api.client.delete(f"/v1/devices/{rig.device_id}", headers=api.auth())
    assert revoked.status_code == 204, revoked.text
    session = await SessionStore(api.app.state.session_factory).get_session(rig.root)
    with pytest.raises(ComputerAway) as away:
        async with files_of(api, session):
            pass
    assert (away.value.revoked, str(away.value)) == (True, "Local access to Flavius's ThinkPad was revoked")


async def test_a_change_is_claimed_by_what_it_does(api, laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    session = await SessionStore(api.app.state.session_factory).get_session(rig.root)
    access = await access_of(api, session)
    async with files_of(api, session, request_id="change-0002", change="upload a.txt one", access=access):
        pass
    with pytest.raises(OperationConflict):
        async with files_of(api, session, request_id="change-0002", change="upload a.txt two", access=access):
            pass


async def test_a_change_is_claimed_only_once_its_caller_was_let_in(api, laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    store = SessionStore(api.app.state.session_factory)
    session = await store.get_session(rig.root)
    child = await create_child_session(store=store, parent=session, channel="worker")
    # No access, or another session's: refused before anything is recorded.
    for access in (None, await access_of(api, child)):
        with pytest.raises(RuntimeError, match="require_device_access"):
            async with files_of(api, session, request_id="change-0003", change="upload a.txt", access=access):
                pass
    async with api.app.state.session_factory() as db:
        recorded = await db.scalar(
            select(func.count()).select_from(DeviceOperation)
            .where(DeviceOperation.invocation_id == f"{REQUEST_PREFIX}change-0003")
        )
    assert recorded == 0
    async with files_of(
        api, session, request_id="change-0003", change="upload a.txt", access=await access_of(api, session),
    ):
        pass


async def test_a_change_its_computer_left_waiting_is_cancelled_once_it_is_found_offline(api, laptop_rig):
    rig = laptop_rig
    await rig.laptop.connect()
    rig.laptop.hold = True  # it receives the change, and does not answer it yet
    session = await SessionStore(api.app.state.session_factory).get_session(rig.root)
    access = await access_of(api, session)
    with pytest.raises(TimeoutError):
        async with files_of(api, session, request_id="change-0001", change="upload notes.md", access=access) as files:
            async with asyncio.timeout(1.0):
                await files.resolve("notes.md")
    assert await has_pending(rig.ops, rig.device_id)
    await rig.laptop.disconnect()

    async def offline() -> bool:
        return not await is_online(api, rig.device_id)

    await eventually(offline, timeout=10.0)
    with pytest.raises(ComputerAway):
        async with files_of(api, session, request_id="change-0001", change="upload notes.md", access=access):
            pass
    # Told it failed, its caller must never see it land later.
    assert not await has_pending(rig.ops, rig.device_id)
