"""office_read and office_edit through a fake runtime-office."""

from __future__ import annotations

import io
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import delete

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.devices.binding import Binding
from surogates.office import agent as office_agent
from surogates.office import files as office_files
from surogates.office import sessions as office_sessions
from surogates.office.agent import location_of
from surogates.office.runtime_office import OfficeBusy, PageGone
from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from surogates.storage.tenant import session_workspace_key
from surogates.tools.builtin import office as office_tools
from tests.device_computer import computer_io

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

OFFICE = OfficeSettings(
    public_url="https://office.example", internal_url="http://docs.internal",
    callback_base_url="https://api.example", jwt_secret="j" * 32, token_secret="t" * 32,
    runtime_office_url="http://runtime-office.test", runtime_office_token="r" * 32,
)


class FakeRuntime:
    """A runtime-office whose pages can be made to vanish once, or to be full."""

    gone_once = False
    busy = False
    calls: list[tuple[str, object]] = []

    def __init__(self, office) -> None:
        pass

    async def open(self, *, doc_key, session_id, config, document_server_url):
        if FakeRuntime.busy:
            raise OfficeBusy("full", 503)
        FakeRuntime.calls.append(("open", config["editorConfig"]["user"]["name"]))
        return f"{doc_key}.{session_id}"

    async def read(self, page, request):
        FakeRuntime.calls.append(("read", request))
        if FakeRuntime.gone_once:
            FakeRuntime.gone_once = False
            raise PageGone("closed", 410)
        return {"blocks": []}

    async def edit(self, page, ops):
        FakeRuntime.calls.append(("edit", ops))
        return {"applied": True, "results": [{"op": 0}], "flushed": True}


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    FakeRuntime.gone_once, FakeRuntime.busy, FakeRuntime.calls = False, False, []
    monkeypatch.setattr(office_tools, "RuntimeOffice", FakeRuntime)
    monkeypatch.setattr(office_tools, "load_settings", lambda: SimpleNamespace(office=OFFICE))
    office_tools._names.clear()


@pytest.fixture
def storage(tmp_path):
    return LocalBackend(str(tmp_path))


async def _kwargs(session_factory, storage, redis_client, platform_client=None, config=None):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="writer", channel="web",
                                         config={"storage_bucket": "b", **(config or {})})
    await storage.write("b", session_workspace_key(session.id, "a.docx"), b"PK-docx")
    return session, {
        "session_id": str(session.id), "agent_id": "writer", "session_store": store, "storage": storage,
        "session_factory": session_factory, "redis": redis_client, "platform_client": platform_client,
    }


async def test_read_names_who_has_it_open(session_factory, storage, redis_client):
    session, kwargs = await _kwargs(session_factory, storage, redis_client)
    document = await office_sessions.open_or_reuse(session_factory, location=location_of(session, "a.docx"), path="a.docx",
                                                   session_id=session.id, user=("user:1", "Flavius"))
    await office_sessions.record_editors(session_factory, document.id, ["user:1"])
    answer = json.loads(await office_tools._office_read({"path": "/workspace/a.docx"}, **kwargs))
    assert answer == {"document": "a.docx", "open_by": ["Flavius"], "blocks": []}
    assert FakeRuntime.calls[-1] == ("read", {"view": "outline", "start": 0})


async def test_a_vanished_page_is_opened_again_once(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    FakeRuntime.gone_once = True
    answer = json.loads(await office_tools._office_read({"path": "a.docx", "view": "search", "query": "x"}, **kwargs))
    assert answer["blocks"] == []
    assert [call[0] for call in FakeRuntime.calls] == ["open", "read", "open", "read"]


async def test_busy_and_bad_requests_are_said(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    assert "view" in json.loads(await office_tools._office_read({"path": "a.docx", "view": "all"}, **kwargs))["error"]
    assert "ops" in json.loads(await office_tools._office_edit({"path": "a.docx", "ops": []}, **kwargs))["error"]
    assert ".docx" in json.loads(await office_tools._office_edit({"path": "a.xlsx", "ops": [{"op": "delete"}]}, **kwargs))["error"]
    FakeRuntime.busy = True
    assert "try again" in json.loads(await office_tools._office_read({"path": "a.docx"}, **kwargs))["error"]


async def test_edit_creates_a_missing_document_and_reads_pictures_for_the_editor(session_factory, storage, redis_client):
    session, kwargs = await _kwargs(session_factory, storage, redis_client)
    picture = io.BytesIO()
    Image.new("RGB", (200, 100)).save(picture, format="PNG")
    await storage.write("b", session_workspace_key(session.id, "chart.png"), picture.getvalue())
    ops = [{"op": "insert", "target": {"end": True},
            "content": {"markdown": "<?block p?>", "blocks": {"p": {"type": "image", "path": "chart.png", "width": 100}}}}]
    answer = json.loads(await office_tools._office_edit({"path": "new.docx", "ops": ops}, **kwargs))
    assert answer["created"] is True and answer["applied"] is True
    sent = FakeRuntime.calls[-1][1][0]["content"]["blocks"]["p"]
    assert {key: sent[key] for key in ("type", "mime", "width", "height")} == {"type": "image", "mime": "image/png", "width": 100, "height": 50}
    assert sent["data"]


async def test_a_picture_is_only_ever_read_from_the_workspace(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    ops = [{"op": "insert", "target": {"end": True},
            "content": {"markdown": "<?block p?>", "blocks": {"p": {"type": "image", "data": "R0lGODlh", "mime": "image/gif"}}}}]
    assert "No picture" in json.loads(await office_tools._office_edit({"path": "a.docx", "ops": ops}, **kwargs))["error"]
    assert "edit" not in [call[0] for call in FakeRuntime.calls]


async def test_the_agents_name_comes_from_its_runtime_config(session_factory, storage, redis_client):
    class Platform:
        async def get_runtime_config(self, agent_id):
            return {"display_name": "Writer"}

    _, kwargs = await _kwargs(session_factory, storage, redis_client, platform_client=Platform())
    await office_tools._office_read({"path": "a.docx"}, **kwargs)
    assert FakeRuntime.calls[0] == ("open", "Writer")


FOLDER = "/home/me/notes"


async def _on_a_computer(session_factory, storage, redis_client, monkeypatch, files):
    """A chat on a folder of a fake computer: the computer, and the tools' kwargs."""
    device_id = str(uuid4())
    computer = computer_io(FOLDER, files, device_id=device_id)

    @asynccontextmanager
    async def on_it(session, **kwargs):
        yield computer

    async def bound(_db, _root):
        return Binding("bound")

    for module in (office_tools, office_agent, office_files):
        monkeypatch.setattr(module, "session_files", on_it)
    monkeypatch.setattr(office_files, "binding_of", bound)
    _, kwargs = await _kwargs(session_factory, storage, redis_client, config={
        "workspace_path": FOLDER, "execution": {"kind": "device", "device_id": device_id},
    })
    return computer, kwargs


async def test_on_a_computer_a_document_through_home_is_refused_and_not_made(session_factory, storage, redis_client, monkeypatch):
    computer, kwargs = await _on_a_computer(session_factory, storage, redis_client, monkeypatch, {})
    answer = json.loads(await office_tools._office_edit({"path": "~/Documents/notes/a.docx", "ops": [{"op": "delete"}]}, **kwargs))
    assert answer["error"] == "Name the document by its path in the chat's folder, like docs/report.docx."
    assert "write" not in [kind for kind, _ in computer.runner.calls]


async def test_on_a_computer_a_picture_it_cannot_read_is_said(session_factory, storage, redis_client, monkeypatch):
    computer, kwargs = await _on_a_computer(session_factory, storage, redis_client, monkeypatch, {"a.docx": b"PK"})
    run = computer.runner.run

    async def unavailable(kind, args, payload=None):
        if kind == "read":
            return {"error": {"type": "unavailable", "message": "The computer is busy"}}
        return await run(kind, args, payload)

    computer.runner.run = unavailable
    ops = [{"op": "insert", "target": {"end": True},
            "content": {"markdown": "<?block p?>", "blocks": {"p": {"type": "image", "path": "chart.png"}}}}]
    answer = json.loads(await office_tools._office_edit({"path": "a.docx", "ops": ops}, **kwargs))
    assert answer["error"] == "Could not read chart.png: The computer is busy"
