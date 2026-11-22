"""Deleting a session removes a workspace only with the last session that uses it.

Through the real delete route, on local storage and, where
``SUROGATES_TEST_S3_ENDPOINT`` names one, on an S3-compatible store.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio

from surogates.api.routes import sessions as sessions_route
from surogates.jobs.platform_cleanup import sweep_orphan_workspaces
from surogates.session.provisioning import create_child_session
from surogates.storage.backend import S3Backend

from .conftest import create_org
from .test_devices import AGENT_ID, api  # noqa: F401  (api is a fixture)

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session", params=["local", "s3"])
async def chats(api, request):  # noqa: F811
    if request.param == "s3":
        endpoint = os.environ.get("SUROGATES_TEST_S3_ENDPOINT")
        if not endpoint:
            pytest.skip("no S3-compatible store named")
        api.app.state.storage = S3Backend(
            endpoint, os.environ["SUROGATES_TEST_S3_KEY"], os.environ["SUROGATES_TEST_S3_SECRET"], "garage",
        )
    return SimpleNamespace(api=api, store=api.app.state.session_store, storage=api.app.state.storage)


async def a_chat(chats):
    created = await chats.api.client.post("/v1/sessions", json={}, headers=chats.api.auth())
    assert created.status_code == 201, created.text
    return await chats.store.get_session(UUID(created.json()["id"]))


async def a_child(chats, parent):
    return await create_child_session(store=chats.store, parent=parent, channel=parent.channel)


async def deleted(chats, session) -> None:
    answer = await chats.api.client.delete(f"/v1/sessions/{session.id}", headers=chats.api.auth())
    assert answer.status_code == 204, answer.text


async def a_file(chats, root) -> tuple[str, str]:
    bucket, key = root.config["storage_bucket"], f"{root.id}/report.txt"
    await chats.storage.write(bucket, key, b"the user's report")
    return bucket, key


async def there(chats, place: tuple[str, str]) -> bool:
    return await chats.storage.exists(*place)


async def test_deleting_a_child_keeps_its_roots_files(chats):
    root = await a_chat(chats)
    child = await a_child(chats, root)
    place = await a_file(chats, root)
    await deleted(chats, child)
    assert await there(chats, place)


async def test_deleting_a_grandchild_keeps_the_roots_files(chats):
    root = await a_chat(chats)
    child = await a_child(chats, root)
    grandchild = await a_child(chats, child)
    place = await a_file(chats, root)
    await deleted(chats, grandchild)
    assert await there(chats, place)
    await deleted(chats, child)
    assert await there(chats, place)


async def test_deleting_a_root_keeps_files_a_session_outside_its_tree_still_uses(chats):
    root = await a_chat(chats)
    place = await a_file(chats, root)
    other = await chats.store.create_session(
        user_id=chats.api.user_id, org_id=chats.api.org_id, agent_id=AGENT_ID, channel=root.channel,
        config={"storage_bucket": place[0], "sandbox_root_session_id": str(root.id)},
    )
    await deleted(chats, root)
    assert await there(chats, place)
    # And they go with the last session that uses them.
    await deleted(chats, other)
    assert not await there(chats, place)


async def test_deleting_a_whole_tree_removes_its_files(chats):
    root = await a_chat(chats)
    child = await a_child(chats, root)
    await a_child(chats, child)
    place = await a_file(chats, root)
    await deleted(chats, root)
    assert not await there(chats, place)


async def test_the_root_deleted_after_its_children_removes_the_files(chats):
    root = await a_chat(chats)
    child = await a_child(chats, root)
    place = await a_file(chats, root)
    await deleted(chats, child)
    assert await there(chats, place)
    await deleted(chats, root)
    assert not await there(chats, place)


async def test_deleting_a_session_with_no_children_removes_its_files(chats):
    root = await a_chat(chats)
    place = await a_file(chats, root)
    await deleted(chats, root)
    assert not await there(chats, place)


async def test_children_deleted_at_once_keep_their_roots_files(chats):
    root = await a_chat(chats)
    family = [await a_child(chats, root) for _ in range(4)]
    place = await a_file(chats, root)
    await asyncio.gather(*(deleted(chats, child) for child in family))
    assert await there(chats, place)
    await deleted(chats, root)
    assert not await there(chats, place)


async def test_another_orgs_session_naming_the_same_root_neither_holds_nor_loses_anything(chats):
    root = await a_chat(chats)
    place = await a_file(chats, root)
    theirs = f"{uuid.uuid4()}/theirs.txt"
    await chats.storage.write(place[0], theirs, b"another org's file")
    other_org = await create_org(chats.api.app.state.session_factory)
    await chats.store.create_session(
        user_id=None, org_id=other_org, agent_id=AGENT_ID, channel="api",
        config={"storage_bucket": place[0], "sandbox_root_session_id": str(root.id)},
    )
    await deleted(chats, root)
    assert not await there(chats, place)
    assert await there(chats, (place[0], theirs))


async def test_a_row_of_another_org_naming_a_root_cannot_delete_its_files(chats):
    root = await a_chat(chats)
    place = await a_file(chats, root)
    other_org = await create_org(chats.api.app.state.session_factory)
    stray = await chats.store.create_session(
        user_id=None, org_id=other_org, agent_id=AGENT_ID, channel="api",
        config={"storage_bucket": place[0], "sandbox_root_session_id": str(root.id)},
    )
    await sessions_route._cleanup_archived_workspaces(chats.storage, [stray], chats.api.app.state.session_factory)
    assert await there(chats, place)


async def test_an_archived_session_with_a_boundary_still_holds_the_workspace_it_names(chats):
    root = await a_chat(chats)
    place = await a_file(chats, root)
    kept = await chats.store.create_session(
        user_id=chats.api.user_id, org_id=chats.api.org_id, agent_id=AGENT_ID, channel=root.channel,
        config={
            "storage_bucket": place[0], "sandbox_root_session_id": str(root.id), "workspace_boundary": "workstream:p1",
        },
    )
    await chats.store.update_session_status(kept.id, "archived")
    await deleted(chats, root)
    assert await there(chats, place)


async def test_a_workspace_stays_when_the_check_cannot_be_made(chats):
    root = await a_chat(chats)
    place = await a_file(chats, root)

    def unreachable():
        raise ConnectionError("the database is away")

    await sessions_route._cleanup_archived_workspaces(chats.storage, [root], unreachable)
    assert await there(chats, place)


async def test_the_cleanup_job_keeps_a_deleted_roots_files_while_a_session_still_uses_them(chats):
    root = await a_chat(chats)
    place = await a_file(chats, root)
    other = await chats.store.create_session(
        user_id=chats.api.user_id, org_id=chats.api.org_id, agent_id=AGENT_ID, channel=root.channel,
        config={"storage_bucket": place[0], "sandbox_root_session_id": str(root.id)},
    )
    await deleted(chats, root)
    factory = chats.api.app.state.session_factory
    await sweep_orphan_workspaces(storage=chats.storage, session_factory=factory, bucket=place[0])
    assert await there(chats, place)
    await chats.store.update_session_status(other.id, "archived")
    await sweep_orphan_workspaces(storage=chats.storage, session_factory=factory, bucket=place[0])
    assert not await there(chats, place)
