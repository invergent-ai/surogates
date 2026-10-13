"""The editor route, the file the document server fetches, and its save callbacks."""

from __future__ import annotations

import asyncio
import os
import uuid
from urllib.parse import quote
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import delete

from surogates.db.models import OfficeSession
from surogates.devices.binding import Binding
from surogates.office import sessions as office_sessions
from surogates.office.tokens import read_url_token, sign
from surogates.session.store import SessionStore
from surogates.storage.tenant import session_workspace_key
from surogates.tenant.auth.jwt import create_access_token

from .conftest import create_org, create_user, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")

JWT_SECRET = "j" * 32
TOKEN_SECRET = "t" * 32


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    """Each test sees only its own rows: the save worker claims pending rows across the whole table."""
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


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
    ambiguous = await client.get(url, params={"path": "a.xlsx"}, headers={**sa_headers, "X-Surogate-Acting-User-Id": "42:7"})
    assert ambiguous.status_code == 400
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


def _row_id(config) -> UUID:
    return read_url_token(TOKEN_SECRET, config["document"]["url"].rsplit("/", 1)[1], "file")


async def test_a_closing_document_answers_409_after_the_wait(client, app, session_factory, monkeypatch):
    from surogates.api.routes import office as office_route

    monkeypatch.setattr(office_route, "CLOSING_WAIT_S", 0.2)
    monkeypatch.setattr(office_route, "CLOSING_POLL_S", 0.05)
    session_id, headers, _, _ = await _user_chat(client, app, session_factory)
    url = f"/v1/sessions/{session_id}/workspace/editor"
    first = (await client.get(url, params={"path": "docs/Plan.docx"}, headers=headers)).json()
    await office_sessions.set_pending(session_factory, _row_id(first["config"]), bucket="b", key="k", close=True)
    response = await client.get(url, params={"path": "docs/Plan.docx"}, headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "The document is still being saved; try again in a moment."


async def test_a_closing_document_that_lands_during_the_wait_opens_fresh(client, app, session_factory, monkeypatch):
    from surogates.api.routes import office as office_route

    monkeypatch.setattr(office_route, "CLOSING_WAIT_S", 3.0)
    monkeypatch.setattr(office_route, "CLOSING_POLL_S", 0.05)
    session_id, headers, _, _ = await _user_chat(client, app, session_factory)
    url = f"/v1/sessions/{session_id}/workspace/editor"
    first = (await client.get(url, params={"path": "docs/Plan.docx"}, headers=headers)).json()
    row_id = _row_id(first["config"])
    await office_sessions.set_pending(session_factory, row_id, bucket="b", key="k", close=True)

    async def land_later():
        await asyncio.sleep(0.2)
        await office_sessions.close(session_factory, row_id)

    landing = asyncio.create_task(land_later())
    response = await client.get(url, params={"path": "docs/Plan.docx"}, headers=headers)
    await landing
    assert response.status_code == 200, response.text
    assert response.json()["config"]["document"]["key"] != first["config"]["document"]["key"]


async def test_computer_folder_chats(client, app, session_factory, monkeypatch):
    """Hidden from the org's other members, as every workspace route hides them; refused to their own user."""
    _, headers, user_id, chat = await _user_chat(client, app, session_factory)
    store = SessionStore(session_factory)
    device_chat = await store.create_session(
        user_id=user_id, org_id=chat.org_id, agent_id=chat.agent_id,
        config={"storage_bucket": chat.config["storage_bucket"],
                "execution": {"kind": "device", "device_id": str(uuid.uuid4())}},
    )
    url = f"/v1/sessions/{device_chat.id}/workspace/editor"
    other = await create_user(session_factory, chat.org_id)
    other_token = create_access_token(chat.org_id, other, {"sessions:read", "sessions:write", "tools:read"})
    hidden = await client.get(url, params={"path": "a.docx"}, headers={"Authorization": f"Bearer {other_token}"})
    assert hidden.status_code == 404

    async def bound(_db, _root):
        return Binding("bound")

    monkeypatch.setattr("surogates.api.session_guards.binding_of", bound)
    refused = await client.get(url, params={"path": "a.docx"}, headers=headers)
    assert refused.status_code == 409
    assert "folder on a computer" in refused.json()["detail"]


async def test_scheduled_run_opens_read_only(client, app, session_factory):
    _, headers, user_id, chat = await _user_chat(client, app, session_factory)
    run = await SessionStore(session_factory).create_session(
        user_id=user_id, org_id=chat.org_id, agent_id=chat.agent_id, channel="scheduled",
        config={"storage_bucket": chat.config["storage_bucket"], "scheduled_session_id": str(uuid.uuid4())},
    )
    await app.state.storage.write(run.config["storage_bucket"], session_workspace_key(str(run.id), "r.docx"), b"PK")
    response = await client.get(f"/v1/sessions/{run.id}/workspace/editor", params={"path": "r.docx"}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["config"]["editorConfig"]["mode"] == "view"


def _tokens(config):
    file_token = config["document"]["url"].rsplit("/", 1)[1]
    callback_token = config["editorConfig"]["callbackUrl"].rsplit("/", 1)[1]
    return file_token, callback_token


async def _opened(client, app, session_factory, **kwargs):
    session_id, headers, user_id, session = await _user_chat(client, app, session_factory, **kwargs)
    config = (await client.get(f"/v1/sessions/{session_id}/workspace/editor",
                               params={"path": kwargs.get("path", "docs/Plan.docx")}, headers=headers)).json()["config"]
    row_id = read_url_token(TOKEN_SECRET, _tokens(config)[0], "file")
    return session_id, headers, session, config, row_id


async def test_document_server_fetches_the_file(client, app, session_factory):
    _, _, _, config, row_id = await _opened(client, app, session_factory, content=b"PK-original")
    file_token, _ = _tokens(config)
    response = await client.get(f"/v1/office/files/{file_token}")
    assert response.status_code == 200
    assert response.content == b"PK-original"
    assert (await office_sessions.get_open(session_factory, row_id)).base_version


async def test_bad_file_token_is_refused(client):
    assert (await client.get("/v1/office/files/not-a-token")).status_code == 403


async def test_status_1_records_editors(client, app, session_factory):
    _, _, _, config, row_id = await _opened(client, app, session_factory)
    _, callback_token = _tokens(config)
    body = {"key": config["document"]["key"], "status": 1, "users": ["user:1"]}
    response = await client.post(f"/v1/office/callback/{callback_token}", json={"token": sign(JWT_SECRET, body)})
    assert response.json() == {"error": 0}
    assert (await office_sessions.get_open(session_factory, row_id)).editors == ("user:1",)


async def test_unsigned_callback_is_refused(client, app, session_factory):
    _, _, _, config, _ = await _opened(client, app, session_factory)
    _, callback_token = _tokens(config)
    response = await client.post(f"/v1/office/callback/{callback_token}", json={"status": 1, "users": []})
    assert response.status_code == 403


async def test_callback_for_another_document_is_refused(client, app, session_factory, monkeypatch):
    """A body the document server signed for one document cannot act on another's row."""
    from surogates.api.routes import office as office_route
    downloads: list[str] = []

    async def fake_download(url: str) -> bytes:
        downloads.append(url)
        return b"PK-other"

    monkeypatch.setattr(office_route, "_download", fake_download)
    _, _, _, config, row_id = await _opened(client, app, session_factory)
    _, _, _, other, _ = await _opened(client, app, session_factory)
    _, callback_token = _tokens(config)
    for body in (
        {"key": other["document"]["key"], "status": 1, "users": ["user:1"]},
        {"key": other["document"]["key"], "status": 2, "url": "https://office.example/cache/files/y/output.docx"},
    ):
        response = await client.post(f"/v1/office/callback/{callback_token}", json={"token": sign(JWT_SECRET, body)})
        assert response.status_code == 403
    document = await office_sessions.get_open(session_factory, row_id)
    assert document.editors == ()
    assert document.pending_save is None
    assert downloads == []


async def test_save_is_taken_and_kept_pending(client, app, session_factory, monkeypatch):
    from surogates.api.routes import office as office_route
    downloads: list[str] = []

    async def fake_download(url: str) -> bytes:
        downloads.append(url)
        return b"PK-edited"

    monkeypatch.setattr(office_route, "_download", fake_download)
    _, _, session, config, row_id = await _opened(client, app, session_factory)
    _, callback_token = _tokens(config)
    body = {"key": config["document"]["key"], "status": 2, "url": "https://office.example/cache/files/x/output.docx?md5=1"}
    response = await client.post(f"/v1/office/callback/{callback_token}", json={"token": sign(JWT_SECRET, body)})
    assert response.json() == {"error": 0}
    assert downloads == ["http://docs.internal/cache/files/x/output.docx?md5=1"]
    pending = (await office_sessions.get_open(session_factory, row_id)).pending_save
    assert pending["close"] is True
    assert pending["key"].startswith(f"_office/pending/{config['document']['key']}/")
    assert await app.state.storage.read(pending["bucket"], pending["key"]) == b"PK-edited"


async def test_newer_save_removes_the_older_pending_object(client, app, session_factory, monkeypatch):
    from surogates.api.routes import office as office_route
    contents = iter([b"first", b"second"])

    async def fake_download(url: str) -> bytes:
        return next(contents)

    monkeypatch.setattr(office_route, "_download", fake_download)
    _, _, _, config, row_id = await _opened(client, app, session_factory)
    _, callback_token = _tokens(config)
    url = "https://office.example/cache/files/x/output.docx"
    for status in (6, 6):
        body = {"key": config["document"]["key"], "status": status, "url": url}
        await client.post(f"/v1/office/callback/{callback_token}", json={"token": sign(JWT_SECRET, body)})
    pending = (await office_sessions.get_open(session_factory, row_id)).pending_save
    assert await app.state.storage.read(pending["bucket"], pending["key"]) == b"second"
    keys = [e["key"] for e in await app.state.storage.list_entries(pending["bucket"], prefix=f"_office/pending/{config['document']['key']}/")]
    assert keys == [pending["key"]]


async def test_closed_document_refuses_callbacks(client, app, session_factory):
    _, _, _, config, row_id = await _opened(client, app, session_factory)
    _, callback_token = _tokens(config)
    await office_sessions.close(session_factory, row_id)
    body = {"key": config["document"]["key"], "status": 1, "users": []}
    response = await client.post(f"/v1/office/callback/{callback_token}", json={"token": sign(JWT_SECRET, body)})
    assert response.status_code == 404
