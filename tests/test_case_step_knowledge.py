"""The knowledge a case's type grants its sessions, and the pages they read.

A case's scope names the knowledge bases its sessions may read.  Each one of
the case's project is theirs to read, the step's and its children's, though
their agent has it not attached: the worker names it in the prompt and offers
the knowledge tools for it, the tools read it, and every call to the
management plane's knowledge routes carries the grant.  A grant of another
project's knowledge base reads nothing.

Each page a session of a case reads is written to the case root's log, with
the SHA-256 of the content read, so the step's report carries every page its
sessions read.  An ordinary session records nothing.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from surogates.db import ops_engine
from surogates.db.ops_models import OpsKBWikiPage, OpsKnowledgeBase, agent_knowledge_bases
from surogates.harness.prompt import PromptBuilder
from surogates.orchestrator.worker import _filter_effective_tools, _load_session_kbs
from surogates.runtime.platform_client import PlatformClient
from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.session.provisioning import create_agent_session, create_child_session
from surogates.tools.builtin import kb_tools
from tests.test_case_step_isolation import CONTRACT, _SessionStore
from tests.test_case_tool_gate import KB_TOOLS, _registry, _sent_on_a_turn, _tenant
from tests.test_eval_session_seeding import _route_fixtures

# The case's project, and another.
P = str(uuid4())
Q = str(uuid4())
AGENT = "support-bot"
INCOME = b"# Income\n\nCount the net pay of the current employer only."
PAGE_PATH = "policies/income.md"
SOURCE_PATH = "sources/handbook.md"
DIGEST = hashlib.sha256(INCOME).hexdigest()
NOT_ATTACHED = "Error: knowledge base 'kb2' is not attached to this agent."


@pytest.fixture
async def ops(monkeypatch):
    """The management plane's database: ``kb1`` and ``kb2`` of project P, ``kb1`` attached to the agent, and ``kb3`` of Q."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (OpsKnowledgeBase.__table__, OpsKBWikiPage.__table__, agent_knowledge_bases):
            await conn.run_sync(table.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        for kb_id, project in (("kb1", P), ("kb2", P), ("kb3", Q)):
            db.add(OpsKnowledgeBase(
                id=kb_id, project_id=project, name=kb_id, display_name=kb_id.upper(),
                description="", status="active", hub_ref=f"test/{kb_id}",
            ))
            db.add(OpsKBWikiPage(
                id=f"{kb_id}-income", kb_id=kb_id, path=PAGE_PATH, title="Income",
                page_type="concept", size_bytes=len(INCOME), content_sha256=DIGEST,
            ))
        db.add(OpsKBWikiPage(
            id="kb2-handbook", kb_id="kb2", path=SOURCE_PATH, title="Handbook", page_type="source",
            source_file_id="handbook-file", size_bytes=len(INCOME), content_sha256=DIGEST,
        ))
        await db.execute(agent_knowledge_bases.insert().values(agent_id=AGENT, kb_id="kb1", mode="grounding"))
        await db.commit()
    monkeypatch.setattr(ops_engine, "_session_factory", factory)
    yield factory
    await engine.dispose()


@pytest.fixture
def hub(monkeypatch):
    """Hub, which holds every page's content."""
    monkeypatch.setenv("SUROGATES_KB_HUB_ENDPOINT_URL", "https://hub.invalid")
    fetch = AsyncMock(return_value=INCOME)
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", fetch)
    return fetch


class _Log:
    """The event logs the tools write to."""

    def __init__(self) -> None:
        self.events: list[SimpleNamespace] = []

    async def emit_event(self, session_id, event_type, data, *, lease_token=None) -> int:
        self.events.append(SimpleNamespace(session_id=session_id, type=event_type, data=data))
        return len(self.events)

    def pages_read(self, session_id) -> list[dict]:
        return [e.data for e in self.events if e.session_id == session_id and e.type is EventType.KB_PAGE_READ]


class _Ops:
    """The management plane's knowledge routes, which answer every request with nothing found."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/kb/search"):
            return httpx.Response(200, json=[])
        if path.endswith("/links"):
            return httpx.Response(200, json={"status": "unavailable", "links": [], "truncated": False})
        return httpx.Response(200, json={"resolution": "not_found", "documents": [], "truncated": False})


async def _step(store, *, knowledge: list[str], agent_id: str = AGENT) -> Session:
    """A step's own session in project P, made as the Keel link makes one, its case granting *knowledge*."""
    _, request, _, _ = _route_fixtures("/v1/api/sessions", service_account=True)
    return await create_agent_session(
        store=store,
        storage=request.app.state.storage,
        settings=request.app.state.settings,
        org_id=UUID(P),
        user_id=None,
        agent_id=agent_id,
        channel="api",
        config={"memory_boundary": "case:C1", "workspace_boundary": "case:C1"},
        service_account_id=uuid4(),
        case_step={**CONTRACT, "knowledge": knowledge},
        case_scope={"tools": None, "knowledge": knowledge},
    )


def _chat() -> Session:
    """An ordinary chat with the agent, in project P."""
    now = datetime.now(UTC)
    return Session(
        id=uuid4(), user_id=uuid4(), org_id=UUID(P), agent_id=AGENT, channel="web",
        status="active", config={}, created_at=now, updated_at=now,
    )


async def _call(name: str, arguments: dict, session: Session, log: _Log, platform_client=None) -> str:
    """Run a tool of *session* as the harness runs it."""
    return await _registry().dispatch(
        name, arguments,
        session_id=str(session.id), agent_id=session.agent_id, tenant=_tenant(session),
        session_store=log, session_config=session.config, platform_client=platform_client,
    )


async def _read(session: Session, log: _Log, kb_id: str, path: str = PAGE_PATH) -> str:
    return await _call("kb_read_page", {"kb_id": kb_id, "path": path}, session, log)


async def test_a_granted_kb_of_the_project_is_loaded(ops):
    store = _SessionStore()
    # Its own knowledge base too, and one of another project.
    step = await _step(store, knowledge=["kb2", "kb3", "kb1"])
    child = await create_child_session(store=store, parent=step, channel="delegation")

    for session in (step, child):
        kbs = await _load_session_kbs(session, ops_db_url="postgresql://ops")
        assert sorted(kb["id"] for kb in kbs) == ["kb1", "kb2"]
        [granted] = [kb for kb in kbs if kb["id"] == "kb2"]
        assert granted["mode"] == "grounding"
        assert granted["display_name"] == "KB2"
        assert PAGE_PATH in granted["pages_tree"]
    # A session of no case has its agent's own.
    assert [kb["id"] for kb in await _load_session_kbs(_chat(), ops_db_url="postgresql://ops")] == ["kb1"]


@pytest.mark.parametrize("knowledge", [["kb2"], []], ids=["granted", "not-granted"])
async def test_a_step_whose_agent_has_no_kb_still_gets_the_kb_tools(ops, monkeypatch, knowledge):
    registry = _registry()
    step = await _step(_SessionStore(), knowledge=knowledge, agent_id="bare-bot")

    kbs = await _load_session_kbs(step, ops_db_url="postgresql://ops")
    # The worker drops the knowledge tools when the session has no knowledge base to read.
    prompt = _filter_effective_tools(
        tools=set(registry.tool_names) - (set() if kbs else KB_TOOLS), tenant=_tenant(step), session=step,
        use_api_for_harness_tools=True,
    )
    builder = PromptBuilder(_tenant(step), session=step, available_kbs=kbs)
    sent = await _sent_on_a_turn(monkeypatch, step, registry, prompt=builder)

    for offered in (prompt, sent):
        if knowledge:
            assert KB_TOOLS <= offered
        else:
            assert offered.isdisjoint(KB_TOOLS)


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned-digest", "no-digest"])
async def test_a_step_reads_a_granted_kb_and_records_the_page(ops, hub, pinned):
    if not pinned:
        # A page published before digests were kept is recorded with the digest of what was read.
        async with ops() as db:
            await db.execute(sa.update(OpsKBWikiPage).values(content_sha256=None))
            await db.commit()
    log = _Log()
    step = await _step(_SessionStore(), knowledge=["kb2"])

    listing = await _call("kb_list_pages", {"kb_id": "kb2"}, step, log)
    out = await _read(step, log, "kb2")

    assert PAGE_PATH in listing
    assert INCOME.decode() in out
    assert log.pages_read(step.id) == [{"kb_id": "kb2", "path": PAGE_PATH, "sha256": DIGEST}]
    assert len(log.events) == 1


async def test_a_child_reads_its_steps_granted_kb(ops, hub):
    store, log = _SessionStore(), _Log()
    step = await _step(store, knowledge=["kb2"])
    child = await create_child_session(store=store, parent=step, channel="delegation")

    out = await _read(child, log, "kb2")

    assert INCOME.decode() in out
    # On the step's log, which its report reads.
    assert log.pages_read(step.id) == [{"kb_id": "kb2", "path": PAGE_PATH, "sha256": DIGEST}]
    assert log.pages_read(child.id) == []


async def test_a_granted_kb_of_another_project_is_refused(ops, hub):
    log = _Log()
    step = await _step(_SessionStore(), knowledge=["kb2", "kb3"])

    for name, arguments in (
        ("kb_read_page", {"kb_id": "kb3", "path": PAGE_PATH}),
        ("kb_list_pages", {"kb_id": "kb3"}),
    ):
        out = await _call(name, arguments, step, log)
        assert out == NOT_ATTACHED.replace("kb2", "kb3")
    assert log.events == []
    hub.assert_not_called()


async def test_an_ordinary_session_records_no_pages(ops, hub):
    log = _Log()
    chat = _chat()

    out = await _read(chat, log, "kb1")

    assert INCOME.decode() in out
    assert log.events == []
    # Nor does it read a knowledge base of its project its agent has not attached.
    assert await _read(chat, log, "kb2") == NOT_ATTACHED


async def test_search_sends_the_grants_to_ops(ops, hub):
    fake = _Ops()
    client = PlatformClient(base_url="http://ops.test", token="runtime", transport=httpx.MockTransport(fake.handle))
    step = await _step(_SessionStore(), knowledge=["kb2"])
    try:
        await _call("kb_search_pages", {"query": "net pay"}, step, _Log(), client)
        await _call("kb_search_pages", {"query": "HB-1", "mode": "documents"}, step, _Log(), client)
        # Reading an original source looks up its document links.
        await _call("kb_read_page", {"kb_id": "kb2", "path": SOURCE_PATH}, step, _Log(), client)
        await _call("kb_search_pages", {"query": "net pay"}, _chat(), _Log(), client)
    finally:
        await client.aclose()

    *case, chat = fake.requests
    assert [r.url.path for r in case] == [
        f"/api/agents/agents/{AGENT}/kb/search",
        f"/api/agents/agents/{AGENT}/kb/documents",
        f"/api/agents/agents/{AGENT}/kb/documents/handbook-file/links",
    ]
    for request in case:
        assert request.url.params.get_list("granted_kb_ids") == ["kb2"]
    assert "granted_kb_ids" not in chat.url.params
