"""Read exact published passage evidence through the real DB authorization lookup."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from surogates.db.ops_models import (
    OpsKnowledgeBase,
    OpsKBWikiPage,
    agent_knowledge_bases,
)
from surogates.tools.builtin import kb_tools


@pytest.fixture
async def evidence_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (
            OpsKnowledgeBase.__table__,
            OpsKBWikiPage.__table__,
            agent_knowledge_bases,
        ):
            await conn.run_sync(table.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            OpsKnowledgeBase(
                id="kb",
                project_id="project",
                name="kb",
                display_name="KB",
                description="",
                status="active",
                hub_ref="test/kb",
            )
        )
        await s.execute(
            agent_knowledge_bases.insert().values(
                agent_id="agent", kb_id="kb", mode="authoritative"
            )
        )
        await s.commit()
    monkeypatch.setattr(kb_tools, "ensure_ops_session_factory", lambda: factory)
    monkeypatch.setattr(
        "surogates.config.Settings",
        lambda: SimpleNamespace(
            kb_hub=SimpleNamespace(
                endpoint_url="https://unused.invalid",
                access_key_id="test",
                secret_access_key="test",
            )
        ),
    )
    yield factory
    await engine.dispose()


async def add_artifact(factory, raw, *, pdf=False, kb_id="kb", file_id=None):
    path = "sources/manual.json" if pdf else "sources/manual.md"
    digest = hashlib.sha256(raw).hexdigest()
    async with factory() as s:
        s.add(
            OpsKBWikiPage(
                id="parent",
                kb_id=kb_id,
                path=path,
                title="Manual",
                page_type="source",
                source_file_id=file_id,
                size_bytes=len(raw),
                hub_object_path="wiki/.versions/published/manual",
                content_sha256=digest,
            )
        )
        s.add(
            OpsKBWikiPage(
                id="passage",
                kb_id=kb_id,
                path="_passages/manual.md",
                parent_path=path,
                title="Manual",
                page_type="source",
                source_file_id=file_id,
                size_bytes=12,
                hub_object_path="wiki/.versions/published/manual",
                content_sha256=digest,
                source_start=6,
                source_end=18,
                page_number=27 if pdf else None,
            )
        )
        await s.commit()
    return path


async def test_markdown_passage_uses_pinned_object_and_exact_offsets(
    evidence_db, monkeypatch
):
    raw = b"prefixExact clause suffix"
    path = await add_artifact(evidence_db, raw)
    fetch = AsyncMock(return_value=raw)
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", fetch)
    out = await kb_tools._kb_read_page_handler(
        {"kb_id": "kb", "path": path, "passage_id": "passage"}, agent_id="agent"
    )
    assert out.endswith("Exact clause")
    assert "characters=6-18" in out and "sha256=" in out
    assert fetch.call_args.kwargs["path"] == "wiki/.versions/published/manual"


async def test_pdf_passage_and_page_range_are_readable(evidence_db, monkeypatch):
    raw = json.dumps([{"page": 27, "content": "prefixExact clause suffix"}]).encode()
    path = await add_artifact(evidence_db, raw, pdf=True)
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", AsyncMock(return_value=raw))
    out = await kb_tools._kb_read_page_handler(
        {"kb_id": "kb", "path": path, "passage_id": "passage"}, agent_id="agent"
    )
    assert out.endswith("Exact clause") and "page=27" in out
    out = await kb_tools._kb_read_page_handler(
        {"kb_id": "kb", "path": path, "pages": "27"}, agent_id="agent"
    )
    assert "Exact clause" in out and "Page 27" in out


async def test_pdf_range_obeys_character_budget(evidence_db, monkeypatch):
    raw = json.dumps([{"page": 27, "content": "x" * 200000}]).encode()
    path = await add_artifact(evidence_db, raw, pdf=True)
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", AsyncMock(return_value=raw))
    out = await kb_tools._kb_read_page_handler(
        {"kb_id": "kb", "path": path, "pages": "27", "limit": 1000}, agent_id="agent"
    )
    assert len(out) < 1500 and "Continue with offset=1000" in out


@pytest.mark.parametrize("context", ["exact", "surrounding"])
async def test_hash_mismatch_fails_closed(evidence_db, monkeypatch, context):
    path = await add_artifact(evidence_db, b"expected evidence")
    monkeypatch.setattr(
        kb_tools, "fetch_wiki_object", AsyncMock(return_value=b"wrong version")
    )
    out = await kb_tools._kb_read_page_handler(
        {"kb_id": "kb", "path": path, "passage_id": "passage", "context": context},
        agent_id="agent",
    )
    assert out.startswith("Error: published artifact hash mismatch")


@pytest.mark.parametrize("context", ["exact", "surrounding"])
async def test_passage_id_cannot_cross_kb_boundary(evidence_db, monkeypatch, context):
    path = await add_artifact(evidence_db, b"private evidence", kb_id="private")
    fetch = AsyncMock()
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", fetch)
    out = await kb_tools._kb_read_page_handler(
        {"kb_id": "kb", "path": path, "passage_id": "passage", "context": context},
        agent_id="agent",
    )
    assert "not found" in out
    fetch.assert_not_called()


@pytest.mark.parametrize("context", ["exact", "surrounding"])
async def test_detached_agent_cannot_read_passage(evidence_db, monkeypatch, context):
    path = await add_artifact(evidence_db, b"prefixExact clause")
    fetch = AsyncMock()
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", fetch)
    out = await kb_tools._kb_read_page_handler(
        {"kb_id": "kb", "path": path, "passage_id": "passage", "context": context},
        agent_id="other",
    )
    assert "not attached" in out
    fetch.assert_not_called()


@pytest.mark.parametrize("is_pdf", [True, False])
async def test_surrounding_uses_same_verified_artifact(
    evidence_db, monkeypatch, is_pdf
):
    body = "prefixExact clause suffix"
    raw = (
        json.dumps(
            [
                {"page": 26, "content": "Governing heading"},
                {"page": 27, "content": body},
                {"page": 28, "content": "Continued clause"},
            ]
        ).encode()
        if is_pdf
        else body.encode()
    )
    path = await add_artifact(evidence_db, raw, pdf=is_pdf)
    fetch = AsyncMock(return_value=raw)
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", fetch)
    out = await kb_tools._kb_read_page_handler(
        {
            "kb_id": "kb",
            "path": path,
            "passage_id": "passage",
            "context": "surrounding",
        },
        agent_id="agent",
    )
    assert "anchor_characters=6-18" in out and "sha256=" in out
    assert body in out
    if is_pdf:
        assert "Page 26" in out and "Governing heading" in out
        assert "Page 28" in out and "Continued clause" in out
    else:
        assert "Document — characters 0-" in out
    assert fetch.call_args.kwargs["path"] == "wiki/.versions/published/manual"


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"passage_id": "passage", "pages": "27"},
        {"passage_id": "passage", "offset": 10},
    ],
)
async def test_surrounding_rejects_ambiguous_arguments(extra):
    out = await kb_tools._kb_read_page_handler(
        {
            "kb_id": "kb",
            "path": "sources/manual.json",
            "context": "surrounding",
            **extra,
        },
        agent_id="agent",
    )
    assert out.startswith("Error: surrounding context requires")


@pytest.mark.parametrize("limit,expected", [(None, 8000), (-1, 8000), (1000000, 24000)])
async def test_surrounding_handler_enforces_default_and_max_budget(
    evidence_db,
    monkeypatch,
    limit,
    expected,
):
    raw = b"prefixExact clause " + b"x" * 100000
    path = await add_artifact(evidence_db, raw)
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", AsyncMock(return_value=raw))
    out = await kb_tools._kb_read_page_handler(
        {
            "kb_id": "kb",
            "path": path,
            "passage_id": "passage",
            "context": "surrounding",
            "limit": limit,
        },
        agent_id="agent",
    )
    assert f"Document — characters 0-{expected} of" in out
    assert len(out) < expected + 1000


async def test_surrounding_obeys_pinned_plan(evidence_db, monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", fetch)
    out = await kb_tools._kb_read_page_handler(
        {
            "kb_id": "kb",
            "path": "sources/private.md",
            "passage_id": "passage",
            "context": "surrounding",
        },
        agent_id="agent",
        session_config={"entitlements": {"kb_ids": []}},
    )
    assert "not included" in out
    fetch.assert_not_called()


def link_response(raw, *, resolution="matched"):
    return {"status": "extracted", "artifact_sha256": hashlib.sha256(raw).hexdigest(), "links": [{
        "relation": "amends", "target": "SPEC/71", "target_edition": "B", "resolution": resolution,
        "documents": [{"kb_id": "kb", "path": "sources/spec.md"}] if resolution == "matched" else [],
        "evidence": {"page": None, "start": 0, "end": len(raw), "quote": raw.decode()},
    }]}


@pytest.mark.parametrize("reviewed", [False, True])
async def test_source_reads_preview_links_and_full_read_preserves_condition(evidence_db, monkeypatch, reviewed):
    raw = b"For indoor units only, this notice amends SPEC/71 revision B."
    path = await add_artifact(evidence_db, raw, file_id="source")
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", AsyncMock(return_value=raw))
    response = link_response(raw)
    if reviewed:
        response["links"][0]["resolution_basis"] = "human_review"
    lookup = AsyncMock(return_value=response)
    client = SimpleNamespace(get_agent_kb_document_links=lookup)
    preview = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": path, "passage_id": "passage"},
        agent_id="agent", platform_client=client)
    assert "resolution=matched" in preview and "context='links'" in preview
    assert "Supporting quote" not in preview
    assert lookup.call_args.kwargs == dict(file_id="source", kb_ids=["kb"], expected_artifact_sha256=hashlib.sha256(raw).hexdigest())
    full = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": path, "context": "links"},
        agent_id="agent", platform_client=client)
    assert "Supporting quote (source text): " + raw.decode() in full
    assert "path=sources/spec.md" in full
    if reviewed:
        assert "Confirmed in document review" in full


@pytest.mark.parametrize("status", ["not_found", "ambiguous", "conflict", "edition_unknown", "edition_mismatch", "self_reference"])
async def test_unresolved_links_never_instruct_agent_to_read_a_selected_target(evidence_db, monkeypatch, status):
    raw = b"Consult SPEC/71 revision B."
    path = await add_artifact(evidence_db, raw, file_id="source")
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", AsyncMock(return_value=raw))
    out = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": path, "context": "links"},
        agent_id="agent", platform_client=SimpleNamespace(get_agent_kb_document_links=AsyncMock(return_value=link_response(raw, resolution=status))))
    assert f"resolution={status}" in out and "Read with kb_read_page" not in out
    assert raw.decode() in out


@pytest.mark.parametrize("digest", [None, "0" * 64])
async def test_stale_or_unpinned_links_are_not_mixed_with_source(evidence_db, monkeypatch, digest):
    raw = b"Consult SPEC/71."
    path = await add_artifact(evidence_db, raw, file_id="source")
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", AsyncMock(return_value=raw))
    result = {**link_response(raw), "artifact_sha256": digest}
    out = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": path, "context": "links"},
        agent_id="agent", platform_client=SimpleNamespace(get_agent_kb_document_links=AsyncMock(return_value=result)))
    assert "publication changed" in out and "SPEC/71" not in out


async def test_link_service_failure_retains_source_without_leaking_exception(evidence_db, monkeypatch):
    raw = b"Exact original source."
    path = await add_artifact(evidence_db, raw, file_id="source")
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", AsyncMock(return_value=raw))
    client = SimpleNamespace(get_agent_kb_document_links=AsyncMock(side_effect=RuntimeError("private credential")))
    out = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": path}, agent_id="agent", platform_client=client)
    assert raw.decode() in out and "private credential" not in out
    out = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": path, "context": "links"}, agent_id="agent", platform_client=client)
    assert "temporarily unavailable" in out and "private credential" not in out


@pytest.mark.parametrize("agent,plan", [("agent", []), ("unattached", None)])
async def test_link_reads_preserve_attachment_and_session_plan(evidence_db, monkeypatch, agent, plan):
    raw = b"Consult SPEC/71."
    path = await add_artifact(evidence_db, raw, file_id="source")
    fetch, lookup = AsyncMock(), AsyncMock()
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", fetch)
    out = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": path, "context": "links"},
        agent_id=agent, session_config={"entitlements": {"kb_ids": plan}} if plan is not None else {},
        platform_client=SimpleNamespace(get_agent_kb_document_links=lookup))
    assert out.startswith("Error:")
    fetch.assert_not_called()
    lookup.assert_not_called()


@pytest.mark.parametrize("argument", ["passage_id", "pages", "offset", "limit"])
async def test_links_context_rejects_incompatible_read_arguments(argument):
    out = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": "sources/doc.md", "context": "links", argument: "1"})
    assert out.startswith("Error:") and "omit passage_id" in out


@pytest.mark.parametrize("basis", [None, "identifier_in_edition", "stale_review"])
async def test_candidates_are_readable_but_never_presented_as_verified_matches(evidence_db, monkeypatch, basis):
    raw = b"Use this supplement with the pump wiring instructions, edition B."
    path = await add_artifact(evidence_db, raw, file_id="source")
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", AsyncMock(return_value=raw))
    response = link_response(raw, resolution="needs_review")
    response["links"][0]["resolution_basis"] = basis
    if basis == "stale_review":
        response["links"][0]["review_status"] = "stale"
    response["links"][0].update(documents=[{"kb_id": "kb", "path": "sources/pump.md", "filename": "pump.md", "identity_status": "conflict"}], truncated=True)
    out = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": path, "context": "links"}, agent_id="agent", platform_client=SimpleNamespace(get_agent_kb_document_links=AsyncMock(return_value=response)))
    assert "path=sources/pump.md" in out and "identity=conflict" in out
    assert "No target or edition has been selected" in out
    if basis == "identifier_in_edition":
        assert "extracted edition matches a document identifier" in out
    if basis == "stale_review":
        assert "previous review no longer applies" in out
    assert "Candidate list is incomplete" in out
    assert "Read with kb_read_page" not in out and "resolution=matched" not in out
    assert raw.decode() in out
