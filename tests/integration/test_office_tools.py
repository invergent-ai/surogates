"""office_read and office_edit through a fake runtime-office."""

from __future__ import annotations

import base64
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import delete

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.devices.binding import Binding
from surogates.office import agent as office_agent
from surogates.office import files as office_files
from surogates.office import guide as office_guide
from surogates.office import sessions as office_sessions
from surogates.office.agent import location_of
from surogates.office.guide import GUIDES_KEY, RETRY_AFTER_S
from surogates.office.runtime_office import OfficeBusy, PageGone, RuntimeOfficeError
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
    # An edit runtime-office refuses: (its words, its status).
    refuses: tuple[str, int] | None = None
    thread: dict | None = {"id": "1_1", "page": 1}
    outline: dict = {"blocks": []}
    calls: list[tuple[str, object]] = []

    def __init__(self, office) -> None:
        pass

    async def open(self, *, doc_key, session_id, config, document_server_url):
        if FakeRuntime.busy:
            raise OfficeBusy("full", 503)
        FakeRuntime.calls.append(("open", config["editorConfig"]["user"]["name"]))
        return f"{doc_key}.{session_id}"

    async def read(self, page, request):
        if request.get("view") == "thread":
            FakeRuntime.calls.append(("read", request))
            return {"thread": FakeRuntime.thread}
        FakeRuntime.calls.append(("read", request))
        if FakeRuntime.gone_once:
            FakeRuntime.gone_once = False
            raise PageGone("closed", 410)
        return FakeRuntime.outline

    async def edit(self, page, ops):
        FakeRuntime.calls.append(("edit", ops))
        if FakeRuntime.refuses:
            raise RuntimeOfficeError(*FakeRuntime.refuses)
        return {"applied": True, "results": [{"op": 0}], "flushed": True}


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    FakeRuntime.gone_once, FakeRuntime.busy, FakeRuntime.refuses, FakeRuntime.calls = False, False, None, []
    FakeRuntime.thread, FakeRuntime.outline = {"id": "1_1", "page": 1}, {"blocks": []}
    monkeypatch.setattr(office_tools, "RuntimeOffice", FakeRuntime)
    monkeypatch.setattr(office_tools, "load_settings", lambda: SimpleNamespace(office=OFFICE))
    office_tools._names.clear()
    office_guide._unfetchable.clear()


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


async def test_a_slide_svg_from_the_workspace_goes_on_as_it_is_and_a_file_that_is_no_svg_is_refused(session_factory, storage, redis_client):
    session, kwargs = await _kwargs(session_factory, storage, redis_client)
    # matplotlib's own: runtime-office checks what an SVG holds, and drops the DOCTYPE.
    svg = (b'<?xml version="1.0" encoding="utf-8" standalone="no"?>\n'
           b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'
           b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><text>online = 34%</text></svg>')
    await storage.write("b", session_workspace_key(session.id, "chart.svg"), svg)
    answer = json.loads(await office_tools._office_edit(
        {"path": "deck.pptx", "ops": [{"op": "slides", "slides": [{"html": '<section><img src="chart.svg" alt="x"></section>'}]}]}, **kwargs))
    assert answer["applied"] is True
    sent = FakeRuntime.calls[-1][1][0]["slides"][0]["html"]
    assert sent == f'<section><img src="data:image/svg+xml;base64,{base64.b64encode(svg).decode()}" alt="x"></section>'
    await storage.write("b", session_workspace_key(session.id, "fake.svg"), b"\x89PNG\r\n\x1a\n not an svg")
    answer = json.loads(await office_tools._office_edit(
        {"path": "deck.pptx", "ops": [{"op": "slides", "slides": [{"html": '<section><img src="fake.svg" alt="x"></section>'}]}]}, **kwargs))
    assert "fake.svg is not a PNG, JPEG or SVG" in answer["error"]


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


async def test_broken_ops_string_is_read_and_said(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    # As a model server's tool parser passes it on: JSON text, the op's own '}' missing right before the ']'.
    broken = ('[{"op": "insert", "target": {"end": true}, "content": {"markdown": "# A\\n<?block why?>", '
              '"blocks": {"why": {"type": "pending", "intent": "Why"}}}]')
    answer = json.loads(await office_tools._office_edit({"path": "a.docx", "ops": broken}, **kwargs))
    assert answer["applied"] is True
    assert FakeRuntime.calls[-1] == ("edit", [{"op": "insert", "target": {"end": True}, "content": {
        "markdown": "# A\n<?block why?>", "blocks": {"why": {"type": "pending", "intent": "Why"}}}}])
    assert len(answer["read_as"]) == 1 and "missing '}'" in answer["read_as"][0]
    # Ops as sent leave nothing to say.
    answer = json.loads(await office_tools._office_edit({"path": "a.docx", "ops": [{"op": "delete"}]}, **kwargs))
    assert "read_as" not in answer


async def test_ops_that_cannot_be_read_open_and_make_nothing(session_factory, storage, redis_client):
    session, kwargs = await _kwargs(session_factory, storage, redis_client)
    answer = json.loads(await office_tools._office_edit({"path": "new.docx", "ops": '[{"op": "delete" "target": {}}]'}, **kwargs))
    assert "not valid JSON" in answer["error"] and "character 17" in answer["error"]
    answer = json.loads(await office_tools._office_edit({"path": "new.docx", "ops": [{"op": "rewrite"}]}, **kwargs))
    assert "insert, replace, delete, move, comment, reply or resolve" in answer["error"]
    assert FakeRuntime.calls == []
    assert not await storage.exists("b", session_workspace_key(session.id, "new.docx"))


async def test_what_was_read_differently_is_said_when_the_call_fails(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    ops = {"op": "insert", "target": {"end": True}, "content": {"markdown": "<?block p?>", "blocks": {"p": {"type": "image", "path": "gone.png"}}}}
    answer = json.loads(await office_tools._office_edit({"path": "a.docx", "ops": ops}, **kwargs))
    assert answer["error"] == "No picture at gone.png."
    assert answer["read_as"] == ['"ops" was one op, not a list: read as a list of one.']


async def test_what_was_read_differently_is_said_when_an_op_is_refused(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    answer = json.loads(await office_tools._office_edit({"path": "a.docx", "ops": '[{"op": "rewrite", "target": {"end": true}'}, **kwargs))
    assert "insert, replace, delete, move, comment, reply or resolve" in answer["error"]
    assert answer["read_as"] == ["\"ops\" was JSON text, not a list: read as the JSON it holds, adding the missing '}]' at the end."]
    answer = json.loads(await office_tools._office_edit({"path": "a.docx", "ops": [{"op": "rewrite"}]}, **kwargs))
    assert "read_as" not in answer
    assert FakeRuntime.calls == []


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


MENTION = {"path": "a.docx", "thread": "15689582", "kind": "docx", "author": "Ana", "author_id": "user:1", "comment": "@Writer check"}


async def test_a_mention_is_acknowledged_in_its_thread(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    answer = json.loads(await office_tools.acknowledge_mention(MENTION, **kwargs))
    assert answer["applied"] is True
    assert FakeRuntime.calls[-1] == ("edit", [{"op": "reply", "thread": "15689582", "body": "On it, I'll reply here when done."}])


async def test_a_pdfs_mention_finds_its_thread_by_the_comments_words(session_factory, storage, redis_client):
    session, kwargs = await _kwargs(session_factory, storage, redis_client)
    await storage.write("b", session_workspace_key(session.id, "c.pdf"), b"%PDF")
    mention = {**MENTION, "path": "c.pdf", "thread": "5885824a", "kind": "pdf", "comment": "@Writer is March right?"}
    await office_tools.acknowledge_mention(mention, **kwargs)
    assert FakeRuntime.calls[-2:] == [
        ("read", {"view": "thread", "query": "@Writer is March right?", "author": "user:1"}),
        ("edit", [{"op": "reply", "thread": "1_1", "body": "On it, I'll reply here when done."}]),
    ]
    FakeRuntime.thread = None
    assert "not in c.pdf any more" in json.loads(await office_tools.acknowledge_mention(mention, **kwargs))["error"]


OFFICE_SKILL = Path(__file__).parents[2] / "skills/productivity/office"


class SkillsAPI:
    """The API as the worker's client reaches it: the office skill's files as it stores them, one at a time; *failing*,
    a 404, as the client raises it."""

    def __init__(self, *, failing: bool = False) -> None:
        self.failing = failing
        self.asked: list[tuple[str, str]] = []

    async def skill_file_bytes(self, name, path):
        self.asked.append((name, path))
        if self.failing:
            request = httpx.Request("GET", f"http://api.test/v1/skills/{name}/file")
            raise httpx.HTTPStatusError(f"the skill's file {path} could not be fetched (HTTP 404)", request=request,
                                        response=httpx.Response(404, request=request))
        return (OFFICE_SKILL / path).read_bytes()


def _by_the_model(kwargs, api):
    """*kwargs* as the worker passes them for a model's tool call."""
    return {**kwargs, "tool_call_id": "call_1", "api_client": api}


async def _guides(kwargs):
    session = await kwargs["session_store"].get_session(UUID(kwargs["session_id"]))
    return session.config.get(GUIDES_KEY)


async def test_first_call_of_a_type_carries_its_guide(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    api = SkillsAPI()
    answer = json.loads(await office_tools._office_read({"path": "a.docx"}, **_by_the_model(kwargs, api)))
    assert "error" not in answer and answer["blocks"] == []
    assert answer["guide"]["reference"].startswith("# office_edit for Word documents")
    assert answer["guide"]["rules"].startswith("# Working on documents live with the user")
    assert "---" not in answer["guide"]["rules"]
    # Through the skill's file route: the whole skill's view stages its files into the workspace.
    assert sorted(api.asked) == [("office", "SKILL.md"), ("office", "references/docx.md")]
    assert await _guides(kwargs) == ["docx"]


async def test_second_call_does_not(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    api = SkillsAPI()
    await office_tools._office_read({"path": "a.docx"}, **_by_the_model(kwargs, api))
    answer = json.loads(await office_tools._office_edit({"path": "a.docx", "ops": [{"op": "delete"}]}, **_by_the_model(kwargs, api)))
    assert answer["applied"] is True and "guide" not in answer
    assert len(api.asked) == 2
    assert await _guides(kwargs) == ["docx"]


async def test_each_type_once(session_factory, storage, redis_client):
    session, kwargs = await _kwargs(session_factory, storage, redis_client)
    await storage.write("b", session_workspace_key(session.id, "b.xlsx"), b"PK-xlsx")
    api = SkillsAPI()
    await office_tools._office_read({"path": "a.docx"}, **_by_the_model(kwargs, api))
    answer = json.loads(await office_tools._office_read({"path": "b.xlsx"}, **_by_the_model(kwargs, api)))
    assert answer["guide"]["reference"].startswith("# Spreadsheets (.xlsx)")
    assert await _guides(kwargs) == ["docx", "xlsx"]


async def test_unfetchable_guide_is_left_out(session_factory, storage, redis_client, monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(office_guide, "monotonic", lambda: now[0])
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    answer = json.loads(await office_tools._office_read({"path": "a.docx"}, **_by_the_model(kwargs, SkillsAPI(failing=True))))
    assert answer == {"document": "a.docx", "open_by": [], "blocks": []}
    assert await _guides(kwargs) is None
    # Not asked for again for a while: the calls in between go without it.
    api = SkillsAPI()
    assert "guide" not in json.loads(await office_tools._office_read({"path": "a.docx"}, **_by_the_model(kwargs, api)))
    assert api.asked == [] and await _guides(kwargs) is None
    # The next call after that carries it.
    now[0] += RETRY_AFTER_S
    assert "guide" in json.loads(await office_tools._office_read({"path": "a.docx"}, **_by_the_model(kwargs, api)))
    assert await _guides(kwargs) == ["docx"]


async def test_a_result_too_long_for_the_guide_leaves_it_for_the_next(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    FakeRuntime.outline = {"blocks": [{"id": "1", "text": "x" * 40_000}]}
    text = await office_tools._office_read({"path": "a.docx"}, **_by_the_model(kwargs, SkillsAPI()))
    answer = json.loads(text)
    assert "guide" not in answer and answer["blocks"] == FakeRuntime.outline["blocks"]
    assert await _guides(kwargs) is None
    FakeRuntime.outline = {"blocks": []}
    text = await office_tools._office_read({"path": "a.docx"}, **_by_the_model(kwargs, SkillsAPI()))
    assert "guide" in json.loads(text) and len(text) < 45_000
    assert await _guides(kwargs) == ["docx"]


async def test_a_failed_call_leaves_the_guide_for_the_next(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    api = SkillsAPI()
    FakeRuntime.busy = True
    answer = json.loads(await office_tools._office_read({"path": "a.docx"}, **_by_the_model(kwargs, api)))
    assert "error" in answer and "guide" not in answer
    FakeRuntime.busy, FakeRuntime.refuses = False, ("the editor did not answer", 502)
    answer = json.loads(await office_tools._office_edit({"path": "a.docx", "ops": [{"op": "delete"}]}, **_by_the_model(kwargs, api)))
    assert "error" in answer and "guide" not in answer
    answer = json.loads(await office_tools._office_read({"path": "gone.docx"}, **_by_the_model(kwargs, api)))
    assert "error" in answer and "guide" not in answer
    assert api.asked == [] and await _guides(kwargs) is None


async def test_a_call_refused_for_what_it_says_carries_the_guide(session_factory, storage, redis_client):
    # A small model's first deck edit is most often refused for its slide: the moment it needs the reference most.
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    api = SkillsAPI()
    FakeRuntime.refuses = ("op 0: slide 1: the title overflows its box by 120 px: split the slide.", 400)
    ops = [{"op": "slides", "slides": [{"html": "<section><h1>Q3</h1></section>"}]}]
    answer = json.loads(await office_tools._office_edit({"path": "d.pptx", "ops": ops}, **_by_the_model(kwargs, api)))
    assert answer["error"] == FakeRuntime.refuses[0]
    assert answer["guide"]["reference"].startswith("# Slide decks (.pptx)")
    assert answer["guide"]["rules"].startswith("# Working on documents live with the user")
    assert await _guides(kwargs) == ["pptx"]
    # Given once: the next refusal on the type goes without it.
    answer = json.loads(await office_tools._office_edit({"path": "d.pptx", "ops": ops}, **_by_the_model(kwargs, api)))
    assert "error" in answer and "guide" not in answer
    assert len(api.asked) == 2


async def test_ops_that_cannot_be_read_carry_the_guide_beside_what_was_read(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    answer = json.loads(await office_tools._office_edit(
        {"path": "a.docx", "ops": '[{"op": "rewrite", "target": {"end": true}'}, **_by_the_model(kwargs, SkillsAPI())))
    assert "insert, replace, delete, move, comment, reply or resolve" in answer["error"]
    assert answer["read_as"] and answer["guide"]["reference"].startswith("# office_edit for Word documents")
    assert await _guides(kwargs) == ["docx"]
    assert FakeRuntime.calls == []


async def test_acknowledgement_never_takes_the_guide(session_factory, storage, redis_client):
    _, kwargs = await _kwargs(session_factory, storage, redis_client)
    api = SkillsAPI()
    # As the loop acknowledges a mention: no tool call of the model's.
    answer = json.loads(await office_tools.acknowledge_mention(MENTION, **{**kwargs, "api_client": api}))
    assert answer["applied"] is True and "guide" not in answer
    assert api.asked == [] and await _guides(kwargs) is None
