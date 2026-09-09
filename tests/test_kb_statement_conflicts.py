"""Current review decisions accompany real authorized source reads."""

import copy
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from surogates.tools.builtin import kb_tools
from surogates.tools.builtin.kb_statement_conflicts import format_statement_conflicts
from tests.test_kb_evidence_reads import add_artifact, link_response
from tests.test_kb_evidence_reads import evidence_db as evidence_db


def conflict_result(raw, *, status="pending"):
    digest = hashlib.sha256(raw).hexdigest()
    def side(label, quote):
        return {"document": {"kb_id": "kb", "filename": label + ".md", "path": "sources/" + label + ".md"},
                "statement": {"evidence": {"sha256": digest, "page": 7, "start": 0, "end": len(quote), "quote": quote}, "context": []}}
    return {"status": "extracted", "artifact_sha256": digest, "truncated": False, "items": [{
        "id": "pair", "current": True, "status": status, "explanation": "Different maximum pressures.",
        "scope": "Both refer to cold water.", "review": None, "review_stale": False,
        "left": side("manual", raw.decode()), "right": side("supplement", "Pump PX7 allows 12 bar for cold water only.")} ]}


@pytest.mark.parametrize("status", ["pending", "confirm_conflict", "both_valid", "prefer_left", "prefer_right"])
async def test_source_reads_preview_conflicts_and_full_read_keeps_both_quotes(evidence_db, monkeypatch, status):
    raw = b"Pump PX7 allows 8 bar for cold water only."
    path = await add_artifact(evidence_db, raw, file_id="source")
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", AsyncMock(return_value=raw))
    response = link_response(raw)
    response["statement_conflicts"] = conflict_result(raw, status=status)
    if status != "pending":
        response["statement_conflicts"]["items"][0]["review"] = {"reason": "Use within the tested cold-water conditions only."}
    client = SimpleNamespace(get_agent_kb_document_links=AsyncMock(return_value=response))
    preview = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": path}, agent_id="agent", platform_client=client)
    assert "context='conflicts'" in preview and "Do not silently combine" in preview
    full = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": path, "context": "conflicts"}, agent_id="agent", platform_client=client)
    assert raw.decode() in full and "12 bar for cold water only." in full
    assert "page=7" in full and "never to an entire document" in full
    if status != "pending":
        assert "Reviewer reason (data): Use within the tested cold-water conditions only." in full


async def test_stale_metadata_does_not_forward_a_preference(evidence_db, monkeypatch):
    raw = b"Pump PX7 allows 8 bar."
    path = await add_artifact(evidence_db, raw, file_id="source")
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", AsyncMock(return_value=raw))
    response = link_response(raw)
    response["statement_conflicts"] = conflict_result(raw, status="prefer_left")
    response["statement_conflicts"]["artifact_sha256"] = "f" * 64
    client = SimpleNamespace(get_agent_kb_document_links=AsyncMock(return_value=response))
    result = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": path, "context": "conflicts"}, agent_id="agent", platform_client=client)
    assert "earlier reviewer preferences must not be applied" in result
    assert "Reviewer prefers" not in result and "12 bar" not in result


async def test_failed_conflict_metadata_is_visible_but_original_source_remains_readable(evidence_db, monkeypatch):
    raw = b"Pump PX7 allows 8 bar."
    path = await add_artifact(evidence_db, raw, file_id="source")
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", AsyncMock(return_value=raw))
    client = SimpleNamespace(get_agent_kb_document_links=AsyncMock(side_effect=RuntimeError("private provider key")))
    result = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": path}, agent_id="agent", platform_client=client)
    assert raw.decode() in result and "checks are unavailable" in result and "private provider key" not in result


@pytest.mark.parametrize("argument", ["pages", "offset", "limit", "passage_id"])
async def test_conflict_context_rejects_ambiguous_window_arguments(argument):
    result = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": "sources/a.md", "context": "conflicts", argument: "1" if argument == "passage_id" else 1})
    assert result.startswith("Error:")


async def test_conflict_reads_obey_session_entitlements(evidence_db, monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", fetch)
    result = await kb_tools._kb_read_page_handler({"kb_id": "kb", "path": "sources/a.md", "context": "conflicts"}, agent_id="agent", session_config={"entitlements": {"kb_ids": []}})
    assert "not included" in result
    fetch.assert_not_called()


def test_conflict_output_cap_omits_whole_pairs_not_trailing_conditions():
    raw = ("Long original sentence. " * 25 + "For cold water only.").encode()
    result = conflict_result(raw)
    pair = result["items"][0]
    pair["left"]["statement"]["context"] = [pair["left"]["statement"]["evidence"]] * 2
    result["items"] = [copy.deepcopy(pair) for _ in range(16)]
    out = format_statement_conflicts(result, full=True)
    assert len(out) < 21500 and "omitted by the response limit" in out
    assert "For cold water only." in out


def test_old_or_dismissed_items_cannot_be_rendered_as_current_preferences():
    result = conflict_result(b"Source", status="prefer_left")
    result["items"][0]["current"] = False
    assert "Reviewer prefers" not in format_statement_conflicts(result, full=True)
    result["items"][0].update(current=True, status="dismiss")
    assert "Reviewer prefers" not in format_statement_conflicts(result, full=True)
