"""office_read and office_edit through a fake runtime-office."""

from __future__ import annotations

import base64
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
    assert "slide decks" in json.loads(await office_tools._office_edit({"path": "a.odt", "ops": [{"op": "delete"}]}, **kwargs))["error"]
    FakeRuntime.busy = True
    assert "try again" in json.loads(await office_tools._office_read({"path": "a.docx"}, **kwargs))["error"]


async def test_each_view_and_its_target_reach_runtime_office(session_factory, storage, redis_client):
    session, kwargs = await _kwargs(session_factory, storage, redis_client)
    await storage.write("b", session_workspace_key(session.id, "a.xlsx"), b"PK-xlsx")
    await office_tools._office_read({"path": "a.xlsx", "view": "range", "target": "Sales!A1:B2"}, **kwargs)
    assert FakeRuntime.calls[-1] == ("read", {"view": "range", "target": "Sales!A1:B2"})
    await office_tools._office_read({"path": "a.xlsx", "view": "search", "query": "North"}, **kwargs)
    assert FakeRuntime.calls[-1] == ("read", {"view": "search", "query": "North"})
    assert "target" in json.loads(await office_tools._office_read({"path": "a.xlsx", "view": "slide"}, **kwargs))["error"]


async def test_a_slides_pictures_are_read_from_the_workspace(session_factory, storage, redis_client):
    session, kwargs = await _kwargs(session_factory, storage, redis_client)
    picture = io.BytesIO()
    Image.new("RGB", (40, 20)).save(picture, format="PNG")
    await storage.write("b", session_workspace_key(session.id, "img/chart.png"), picture.getvalue())
    await storage.write("b", session_workspace_key(session.id, "logo.svg"), b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"/>')
    html = '<section><img src="img/chart.png" alt="a"><img src=\'/workspace/logo.svg\' alt="b"><img src="data:image/png;base64,AA==" alt="c"></section>'
    answer = json.loads(await office_tools._office_edit({"path": "deck.pptx", "ops": [{"op": "slides", "slides": [{"html": html}]}]}, **kwargs))
    assert answer["applied"] is True
    sent = FakeRuntime.calls[-1][1][0]["slides"][0]["html"]
    assert 'src="data:image/png;base64,' in sent and "src='data:image/svg+xml;base64," in sent and 'src="data:image/png;base64,AA=="' in sent
    await storage.write("b", session_workspace_key(session.id, "notes.txt"), b"hello")
    bad = '<section><img src="notes.txt" alt="x"></section>'
    assert "not a PNG, JPEG or SVG" in json.loads(await office_tools._office_edit({"path": "deck.pptx", "ops": [{"op": "slides", "slides": [{"html": bad}]}]}, **kwargs))["error"]


SVG = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="8" height="8">{}</svg>'


@pytest.mark.parametrize("inside", [
    '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><text>&x;</text>',
    "<script>alert(1)</script>",
    "<svg:script>alert(1)</svg:script>",
    "<foreignObject><div>x</div></foreignObject>",
    '<rect width="8" height="8" onload="alert(1)"/>',
    '<image href="http://169.254.169.254/latest" width="8" height="8"/>',
    "<use xlink:href='https://example.com/a.svg#b'/>",
    '<rect width="8" height="8" style="fill:url(http://example.com/x)"/>',
    '<a href="javascript:alert(1)"><rect width="8" height="8"/></a>',
    '<set attributeName="href" to="javascript:alert(1)"/>',
])
async def test_a_slide_svg_that_is_more_than_plain_shapes_is_refused(session_factory, storage, redis_client, inside):
    session, kwargs = await _kwargs(session_factory, storage, redis_client)
    svg = SVG.format(inside).encode()
    await storage.write("b", session_workspace_key(session.id, "bad.svg"), svg)
    inline = "data:image/svg+xml;base64," + base64.b64encode(svg).decode()
    for src in ("bad.svg", inline):
        html = f'<section><img src="{src}" alt="x"></section>'
        answer = json.loads(await office_tools._office_edit({"path": "deck.pptx", "ops": [{"op": "slides", "slides": [{"html": html}]}]}, **kwargs))
        assert "is plain shapes" in answer["error"], src[:40]
    assert "edit" not in [call[0] for call in FakeRuntime.calls]


async def test_a_slide_svg_of_shapes_linking_only_inside_itself_goes_in(session_factory, storage, redis_client):
    session, kwargs = await _kwargs(session_factory, storage, redis_client)
    inside = ('<defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/></linearGradient></defs>'
              '<rect width="8" height="8" fill="url(#g)"/><use href="#g"/><image href="data:image/png;base64,AA==" width="1" height="1"/>'
              '<text>Hand on heart</text>')
    await storage.write("b", session_workspace_key(session.id, "ok.svg"), ('<?xml version="1.0" encoding="UTF-8"?>' + SVG.format(inside)).encode())
    html = '<section><img src="ok.svg" alt="x"></section>'
    answer = json.loads(await office_tools._office_edit({"path": "deck.pptx", "ops": [{"op": "slides", "slides": [{"html": html}]}]}, **kwargs))
    assert answer["applied"] is True


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


async def _on_a_computer(session_factory, storage, redis_client, monkeypatch, files, *, ignores_case=False):
    """A chat on a folder of a fake computer: the computer, and the tools' kwargs."""
    device_id = str(uuid4())
    computer = computer_io(FOLDER, files, device_id=device_id, ignores_case=ignores_case)

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


async def test_on_a_computer_the_answer_names_the_document_as_the_computer_spells_it(session_factory, storage, redis_client, monkeypatch):
    _, kwargs = await _on_a_computer(session_factory, storage, redis_client, monkeypatch, {"Plan.docx": b"PK"}, ignores_case=True)
    answer = json.loads(await office_tools._office_edit({"path": "plan.docx", "ops": [{"op": "delete"}]}, **kwargs))
    assert answer["document"] == "Plan.docx"
