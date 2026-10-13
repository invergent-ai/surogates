"""The editor route, the file the document server fetches, and its save callbacks."""

from __future__ import annotations

import os
import uuid
from urllib.parse import quote
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt

from surogates.office.tokens import read_url_token
from surogates.session.store import SessionStore
from surogates.storage.tenant import session_workspace_key
from surogates.tenant.auth.jwt import create_access_token

from .conftest import create_org, create_user, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")

JWT_SECRET = "j" * 32
TOKEN_SECRET = "t" * 32


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url):
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import OfficeSettings, Settings
    from surogates.runtime import agent_runtime_context_dep, build_agent_runtime_context
    from surogates.storage.backend import create_backend

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory)
    application.state.settings = Settings()
    application.state.settings.storage.bucket = f"test-agent-{uuid.uuid4()}"
    application.state.settings.office = OfficeSettings(
        public_url="https://office.example",
        internal_url="http://docs.internal",
        callback_base_url="https://api.example",
        jwt_secret=JWT_SECRET,
        token_secret=TOKEN_SECRET,
    )
    application.state.storage = create_backend(application.state.settings)
    application.dependency_overrides[agent_runtime_context_dep] = lambda: build_agent_runtime_context({
        "agent_id": "default", "org_id": "00000000-0000-0000-0000-000000000000",
        "project_id": "test-project", "enabled": True, "version": 1, "storage_key_prefix": "",
    })
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _user_chat(client, app, session_factory, *, path="docs/Plan.docx", content=b"PK-docx"):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    token = create_access_token(org_id, user_id, {"sessions:read", "sessions:write", "tools:read"})
    headers = {"Authorization": f"Bearer {token}"}
    created = await client.post("/v1/sessions", json={"model": "gpt-4o"}, headers=headers)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    session = await SessionStore(session_factory).get_session(UUID(session_id))
    await app.state.storage.write(session.config["storage_bucket"], session_workspace_key(session_id, path), content)
    return session_id, headers, user_id, session


async def test_user_gets_a_signed_config(client, app, session_factory):
    session_id, headers, user_id, _ = await _user_chat(client, app, session_factory)
    response = await client.get(f"/v1/sessions/{session_id}/workspace/editor",
                                params={"path": "docs/Plan.docx", "theme": "dark"}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document_server_url"] == "https://office.example"
    config = body["config"]
    assert config["editorConfig"]["user"]["id"] == f"user:{user_id}"
    assert config["editorConfig"]["user"]["name"] == f"Test User {user_id}"
    assert config["editorConfig"]["mode"] == "edit"
    assert config["editorConfig"]["customization"]["uiTheme"] == "theme-dark"
    assert jwt.decode(config["token"], JWT_SECRET, algorithms=["HS256"])["document"]["key"] == config["document"]["key"]


async def test_reopening_joins_the_same_session(client, app, session_factory):
    session_id, headers, _, _ = await _user_chat(client, app, session_factory)
    url = f"/v1/sessions/{session_id}/workspace/editor"
    one = (await client.get(url, params={"path": "docs/Plan.docx"}, headers=headers)).json()
    two = (await client.get(url, params={"path": "docs/Plan.docx"}, headers=headers)).json()
    assert one["config"]["document"]["key"] == two["config"]["document"]["key"]


async def test_view_param_opens_read_only(client, app, session_factory):
    session_id, headers, _, _ = await _user_chat(client, app, session_factory)
    response = await client.get(f"/v1/sessions/{session_id}/workspace/editor",
                                params={"path": "docs/Plan.docx", "view": "true"}, headers=headers)
    assert response.json()["config"]["editorConfig"]["mode"] == "view"


async def test_service_account_names_the_person(client, app, session_factory):
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(session_factory, org_id, name="ops")
    sa_headers = {"Authorization": f"Bearer {issued.token}"}
    created = await client.post("/v1/api/sessions", json={"config": {"source": "ops"}}, headers=sa_headers)
    session_id = created.json()["id"]
    session = await SessionStore(session_factory).get_session(UUID(session_id))
    await app.state.storage.write(session.config["storage_bucket"], session_workspace_key(session_id, "a.xlsx"), b"PK")
    url = f"/v1/api/sessions/{session_id}/workspace/editor"
    missing = await client.get(url, params={"path": "a.xlsx"}, headers=sa_headers)
    assert missing.status_code == 400
    named = await client.get(url, params={"path": "a.xlsx"}, headers={
        **sa_headers, "X-Surogate-Acting-User-Id": "42", "X-Surogate-Acting-User-Name": quote("Ana Popescu"),
    })
    assert named.status_code == 200, named.text
    user = named.json()["config"]["editorConfig"]["user"]
    assert user == {"id": f"sa:{issued.id}:42", "name": "Ana Popescu"}


async def test_acting_headers_are_ignored_on_user_tokens(client, app, session_factory):
    session_id, headers, user_id, _ = await _user_chat(client, app, session_factory)
    response = await client.get(f"/v1/sessions/{session_id}/workspace/editor", params={"path": "docs/Plan.docx"},
                                headers={**headers, "X-Surogate-Acting-User-Id": "someone-else"})
    assert response.json()["config"]["editorConfig"]["user"]["id"] == f"user:{user_id}"


async def test_not_office_missing_and_disabled(client, app, session_factory):
    session_id, headers, _, _ = await _user_chat(client, app, session_factory)
    url = f"/v1/sessions/{session_id}/workspace/editor"
    assert (await client.get(url, params={"path": "notes.md"}, headers=headers)).status_code == 415
    assert (await client.get(url, params={"path": "nope.docx"}, headers=headers)).status_code == 404
    assert (await client.get(url, params={"path": ".threads/x.docx"}, headers=headers)).status_code == 403
    saved = app.state.settings.office
    from surogates.config import OfficeSettings
    app.state.settings.office = OfficeSettings()
    try:
        assert (await client.get(url, params={"path": "docs/Plan.docx"}, headers=headers)).status_code == 503
    finally:
        app.state.settings.office = saved
