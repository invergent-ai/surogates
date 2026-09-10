from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from surogates.tools.builtin.kb_tools import _kb_search_pages_handler


@pytest.mark.asyncio
async def test_document_mode_propagates_plan_and_keeps_ambiguity():
    lookup = AsyncMock(
        return_value={
            "resolution": "ambiguous",
            "documents": [
                {
                    "kb_id": "kb",
                    "filename": "manual.pdf",
                    "identity": {
                        "status": "conflict",
                        "artifact_path": "sources/manual.json",
                        "claims": [],
                    },
                },
            ],
        }
    )
    out = await _kb_search_pages_handler(
        {"query": "DOC-1", "mode": "documents"},
        agent_id="agent",
        platform_client=SimpleNamespace(find_agent_kb_documents=lookup),
        session_config={"entitlements": {"kb_ids": ["allowed"]}},
    )
    assert lookup.call_args.kwargs["kb_ids"] == ["allowed"]
    assert "Keep these candidates separate" in out
    assert "sources/manual.json" in out and "kb_read_page" in out


@pytest.mark.asyncio
async def test_document_mode_denies_empty_plan_without_calling_ops():
    lookup = AsyncMock()
    out = await _kb_search_pages_handler(
        {"query": "DOC-1", "mode": "documents"},
        agent_id="agent",
        platform_client=SimpleNamespace(find_agent_kb_documents=lookup),
        session_config={"entitlements": {"kb_ids": []}},
    )
    assert out.startswith("Error:")
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_missing_identity_recommends_content_search():
    out = await _kb_search_pages_handler(
        {"query": "DOC-1", "mode": "documents"},
        agent_id="agent",
        platform_client=SimpleNamespace(
            find_agent_kb_documents=AsyncMock(return_value={"documents": []})
        ),
    )
    assert "mode='passages'" in out
