"""Who may change a skill that lives in the org-shared layer.

A skill found by name resolves to the caller's own layer first and to the
org-shared layer second.  Changing an org-shared skill needs a service account
or an ``admin`` token; an end-user changes only the skills in their own layer.
The expert lifecycle routes (activate, retire, collect) also refuse
channel-session callers.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import NamedTuple
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient, Response

from surogates.storage.backend import LocalBackend
from surogates.storage.tenant import TenantStorage
from surogates.tenant.credentials import CredentialVault

from .conftest import OrgPrincipals, org_principals

pytestmark = pytest.mark.asyncio(loop_scope="session")


EXPERT = (
    "---\n"
    "name: {name}\n"
    "description: Answers billing questions\n"
    "type: expert\n"
    "expert_status: draft\n"
    "expert_endpoint: https://old.example/v1\n"
    "---\n"
    "Original body\n"
)
REWRITTEN = EXPERT.replace("Original body", "Rewritten body")
NOTES = "references/notes.md"


class Route(NamedTuple):
    id: str
    call: Callable[[AsyncClient, str, dict[str, str]], Awaitable[Response]]
    #: What the route answers when it is allowed.
    ok_status: int
    #: Whether an allowed call leaves the skill's files different from before.
    changes_the_skill: bool = True


ROUTES = [
    Route(
        "put",
        lambda c, n, h: c.put(
            f"/v1/skills/{n}", json={"content": REWRITTEN.format(name=n)}, headers=h,
        ),
        200,
    ),
    Route(
        "patch",
        lambda c, n, h: c.patch(
            f"/v1/skills/{n}",
            json={"old_string": "Original body", "new_string": "Patched body"},
            headers=h,
        ),
        200,
    ),
    Route("delete", lambda c, n, h: c.delete(f"/v1/skills/{n}", headers=h), 204),
    Route(
        "write-file",
        lambda c, n, h: c.post(
            f"/v1/skills/{n}/files",
            json={"file_path": "references/extra.md", "file_content": "extra"},
            headers=h,
        ),
        201,
    ),
    Route(
        "remove-file",
        lambda c, n, h: c.delete(
            f"/v1/skills/{n}/files", params={"path": NOTES}, headers=h,
        ),
        204,
    ),
    Route(
        "activate",
        lambda c, n, h: c.post(
            f"/v1/skills/{n}/activate",
            json={"endpoint": "https://new.example/v1"},
            headers=h,
        ),
        200,
    ),
    Route("retire", lambda c, n, h: c.post(f"/v1/skills/{n}/retire", headers=h), 200),
    # No finished session mentions the expert, so there is nothing to export
    # and the skill's files stay as they are.
    Route(
        "collect",
        lambda c, n, h: c.post(f"/v1/skills/{n}/collect", headers=h),
        200,
        changes_the_skill=False,
    ),
]
EXPERT_ROUTES = [r for r in ROUTES if r.id in {"activate", "retire", "collect"}]
route_ids = [r.id for r in ROUTES]


class _NoFinishedSessions:
    """A session store with no finished session for the collector to read."""

    async def list_sessions(self, **_filters):
        return []


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url, tmp_path_factory):
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = _NoFinishedSessions()
    application.state.settings = Settings()
    application.state.settings.storage.bucket = "test-skills-shared-layer"
    application.state.storage = LocalBackend(
        base_path=str(tmp_path_factory.mktemp("skills-shared-layer-storage")),
    )
    application.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key(),
    )
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def principals(session_factory) -> OrgPrincipals:
    return await org_principals(session_factory)


def _storage(app, org_id: UUID, user_id: UUID | None) -> TenantStorage:
    return TenantStorage(
        backend=app.state.storage,
        org_id=org_id,
        user_id=user_id,
        bucket=app.state.settings.storage.bucket,
    )


async def _seed(ts: TenantStorage, name: str) -> None:
    key_prefix = await ts.write_skill(name, EXPERT.format(name=name))
    await ts.write_skill_file(key_prefix, NOTES, "notes")


async def _files(ts: TenantStorage, name: str) -> dict[str, str] | None:
    """Every file of the skill the storage finds under *name*, or ``None`` when there is none."""
    found = await ts.skill_exists(name)
    if found is None:
        return None
    prefix = found["key_prefix"]
    return {
        path: await ts.read_skill_file(prefix, path)
        for path in await ts.list_skill_files(prefix)
    }


@pytest.mark.parametrize("route", ROUTES, ids=route_ids)
async def test_an_end_user_cannot_change_an_org_shared_skill(
    app, client, principals, route,
):
    shared = _storage(app, principals.org_id, None)
    name = f"shared-{route.id}"
    await _seed(shared, name)
    before = await _files(shared, name)

    response = await route.call(client, name, principals.auth("user"))

    assert response.status_code == 403, response.text
    assert await _files(shared, name) == before


@pytest.mark.parametrize("route", ROUTES, ids=route_ids)
@pytest.mark.parametrize("principal", ["service_account_session", "admin"])
async def test_a_service_account_or_an_admin_can_change_an_org_shared_skill(
    app, client, principals, route, principal,
):
    shared = _storage(app, principals.org_id, None)
    name = f"shared-{route.id}-{principal}".replace("_", "-")
    await _seed(shared, name)
    before = await _files(shared, name)

    response = await route.call(client, name, principals.auth(principal))

    assert response.status_code == route.ok_status, response.text
    assert (await _files(shared, name) != before) is route.changes_the_skill


@pytest.mark.parametrize("route", ROUTES, ids=route_ids)
async def test_an_end_user_can_change_a_skill_in_their_own_layer(
    app, client, principals, route,
):
    own = _storage(app, principals.org_id, principals.user_id)
    name = f"own-{route.id}"
    await _seed(own, name)
    before = await _files(own, name)

    response = await route.call(client, name, principals.auth("user"))

    assert response.status_code == route.ok_status, response.text
    assert (await _files(own, name) != before) is route.changes_the_skill


@pytest.mark.parametrize("route", EXPERT_ROUTES, ids=[r.id for r in EXPERT_ROUTES])
async def test_a_channel_session_cannot_use_the_expert_routes(
    app, client, principals, route,
):
    shared = _storage(app, principals.org_id, None)
    name = f"channel-{route.id}"
    await _seed(shared, name)
    before = await _files(shared, name)

    response = await route.call(client, name, principals.auth("channel_session"))

    assert response.status_code == 403, response.text
    assert await _files(shared, name) == before
