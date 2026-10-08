"""A project's threads that work in a folder on the user's computer."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from surogates.devices.binding import Binding
from surogates.devices.operations import DeviceOperations
from surogates.session.store import SessionStore

from .test_devices import (  # noqa: F401  (api is a fixture)
    AGENT_ID,
    FOLDER,
    NONCE,
    api,
    binding,
    register,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def journal(api) -> DeviceOperations:
    return DeviceOperations(api.app.state.session_factory, api.app.state.redis)


async def own_root(api, device_id: UUID) -> UUID:
    """A session that is its own sandbox root, as a project's thread is, on *device_id*'s folder, not bound yet."""
    session_id = uuid4()
    await SessionStore(api.app.state.session_factory).create_session(
        session_id=session_id, user_id=api.user_id, org_id=api.org_id, agent_id=AGENT_ID, channel="worker",
        config={
            "execution": {"kind": "device", "device_id": str(device_id), "device_name": "Flavius's ThinkPad"},
            "workspace_path": FOLDER,
            "storage_bucket": "test-bucket",
            "storage_key_prefix": "",
            "sandbox_root_session_id": str(session_id),
        },
    )
    return session_id


async def bound_own_root(api) -> tuple[UUID, UUID]:
    device_id = UUID((await register(api))["id"])
    root = await own_root(api, device_id)
    ops = journal(api)
    await ops.bind(session_id=root, device_id=device_id, folder=FOLDER, nonce=NONCE)
    [bind] = await ops.pending(device_id, 1)
    assert await ops.complete(device_id, 1, bind.id, bind.digest, {"ok": None}) == "completed"
    return device_id, root


async def test_a_session_that_is_its_own_sandbox_root_is_bound_to_a_folder(api):
    _, root = await bound_own_root(api)
    assert await binding(api, root) == Binding("bound")


async def test_deleting_a_session_that_is_its_own_sandbox_root_has_its_computer_forget_the_folder(api):
    device_id, root = await bound_own_root(api)
    deleted = await api.client.delete(f"/v1/sessions/{root}", headers=api.auth())
    assert deleted.status_code == 204, deleted.text
    [retire] = await journal(api).pending(device_id, 1)
    assert (retire.kind, retire.root_session_id, retire.args) == ("retire", root, {})
