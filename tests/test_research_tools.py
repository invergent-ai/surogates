"""Tests for the research builtin tools (workspace-backed file IO).

The tools translate JSON-shaped tool calls into reads/writes against
``{workspace_path}/.research/{memory.jsonl,outline.md}``.  We exercise
them through the registry's ``dispatch`` surface (the same path the
harness uses) so the test pins the wire-level contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from surogates.tools.builtin import research
from surogates.tools.registry import ToolRegistry


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    research.register(reg)
    return reg


def _research_dir(workspace: Path) -> Path:
    return workspace / ".research"


# ---------------------------------------------------------------------------
# research_memory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_research_memory_add_returns_source_id(tmp_path, registry):
    out = await registry.dispatch(
        "research_memory",
        {
            "action": "add",
            "url": "https://a.test",
            "title": "A",
            "summary": "alpha facts",
            "evidence": ["e1"],
        },
        workspace_path=str(tmp_path),
    )
    obj = json.loads(out)

    assert obj["success"] is True
    assert obj["source_id"] == "S1"
    assert obj["url"] == "https://a.test"
    assert obj["title"] == "A"
    assert obj["total"] == 1


@pytest.mark.asyncio
async def test_research_memory_add_persists_to_jsonl(tmp_path, registry):
    """``add`` must write the JSONL file before returning so the child
    writer session can read the bank on its next ``list`` call."""

    await registry.dispatch(
        "research_memory",
        {"action": "add", "url": "u1", "title": "t", "summary": "s"},
        workspace_path=str(tmp_path),
    )
    jsonl = (_research_dir(tmp_path) / "memory.jsonl").read_text()

    assert jsonl.strip()
    parsed = json.loads(jsonl.strip())
    assert parsed["source_id"] == "S1"
    assert parsed["url"] == "u1"


@pytest.mark.asyncio
async def test_research_memory_list_returns_every_source(tmp_path, registry):
    await registry.dispatch(
        "research_memory",
        {"action": "add", "url": "u1", "title": "A", "summary": "a"},
        workspace_path=str(tmp_path),
    )
    await registry.dispatch(
        "research_memory",
        {"action": "add", "url": "u2", "title": "B", "summary": "b"},
        workspace_path=str(tmp_path),
    )
    out = await registry.dispatch(
        "research_memory", {"action": "list"},
        workspace_path=str(tmp_path),
    )
    obj = json.loads(out)

    urls = [s["url"] for s in obj["sources"]]
    ids = [s["source_id"] for s in obj["sources"]]
    assert urls == ["u1", "u2"]
    assert ids == ["S1", "S2"]


@pytest.mark.asyncio
async def test_research_memory_retrieve_ranks_by_keyword(tmp_path, registry):
    await registry.dispatch(
        "research_memory",
        {
            "action": "add", "url": "u1", "title": "Quantum",
            "summary": "qubits superposition", "evidence": [],
        },
        workspace_path=str(tmp_path),
    )
    await registry.dispatch(
        "research_memory",
        {
            "action": "add", "url": "u2", "title": "Sourdough",
            "summary": "flour starter", "evidence": [],
        },
        workspace_path=str(tmp_path),
    )
    out = await registry.dispatch(
        "research_memory",
        {"action": "retrieve", "query": "qubits", "k": 1},
        workspace_path=str(tmp_path),
    )
    obj = json.loads(out)

    assert [s["url"] for s in obj["sources"]] == ["u1"]


@pytest.mark.asyncio
async def test_research_memory_persists_ids_across_dispatch(tmp_path, registry):
    """A second ``add`` must see the JSONL from the first ``add`` and
    assign ``S2`` — proves the round-trip is per-call rather than
    per-process."""

    await registry.dispatch(
        "research_memory",
        {"action": "add", "url": "u1", "title": "A", "summary": "a"},
        workspace_path=str(tmp_path),
    )
    second = json.loads(
        await registry.dispatch(
            "research_memory",
            {"action": "add", "url": "u2", "title": "B", "summary": "b"},
            workspace_path=str(tmp_path),
        )
    )
    assert second["source_id"] == "S2"


@pytest.mark.asyncio
async def test_research_memory_dedupes_by_url(tmp_path, registry):
    """Same URL twice → same source_id; total stays at 1."""

    first = json.loads(
        await registry.dispatch(
            "research_memory",
            {"action": "add", "url": "u1", "title": "A", "summary": "a"},
            workspace_path=str(tmp_path),
        )
    )
    second = json.loads(
        await registry.dispatch(
            "research_memory",
            {
                "action": "add", "url": "u1", "title": "A v2",
                "summary": "a v2",
            },
            workspace_path=str(tmp_path),
        )
    )
    assert second["source_id"] == first["source_id"]
    assert second["total"] == 1


@pytest.mark.asyncio
async def test_research_memory_add_requires_url(tmp_path, registry):
    out = await registry.dispatch(
        "research_memory",
        {"action": "add", "title": "A", "summary": "a"},
        workspace_path=str(tmp_path),
    )
    obj = json.loads(out)
    assert obj["success"] is False
    assert "url" in obj["error"].lower()


@pytest.mark.asyncio
async def test_research_memory_unknown_action_returns_error(tmp_path, registry):
    out = await registry.dispatch(
        "research_memory",
        {"action": "bogus"},
        workspace_path=str(tmp_path),
    )
    obj = json.loads(out)
    assert obj["success"] is False
    assert "bogus" in obj["error"]


@pytest.mark.asyncio
async def test_research_memory_missing_workspace_returns_error(registry):
    out = await registry.dispatch(
        "research_memory", {"action": "list"},
    )
    obj = json.loads(out)
    assert obj["success"] is False
    assert "workspace" in obj["error"].lower()


# ---------------------------------------------------------------------------
# research_outline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_research_outline_set_persists_and_returns_sections(
    tmp_path, registry,
):
    out = await registry.dispatch(
        "research_outline",
        {
            "action": "set",
            "outline": "# Report\n## Background\nbody\n## Methods\nmore\n",
        },
        workspace_path=str(tmp_path),
    )
    obj = json.loads(out)

    assert obj["success"] is True
    assert obj["sections"] == ["Background", "Methods"]
    on_disk = (_research_dir(tmp_path) / "outline.md").read_text()
    assert "## Background" in on_disk


@pytest.mark.asyncio
async def test_research_outline_set_normalizes_trailing_whitespace(
    tmp_path, registry,
):
    """Whatever the planner pasted in, the persisted outline is
    normalized so a later ``get`` returns the canonical form."""

    await registry.dispatch(
        "research_outline",
        {
            "action": "set",
            "outline": "# R\n\n\n## A   \nbody\n\n\n",
        },
        workspace_path=str(tmp_path),
    )
    out = json.loads(
        await registry.dispatch(
            "research_outline",
            {"action": "get"},
            workspace_path=str(tmp_path),
        )
    )

    assert "\n\n\n" not in out["outline"]
    assert "## A   \n" not in out["outline"]


@pytest.mark.asyncio
async def test_research_outline_get_returns_empty_string_when_missing(
    tmp_path, registry,
):
    """A planner that calls ``get`` before any ``set`` must see an
    empty outline rather than a 'file not found' error."""

    out = json.loads(
        await registry.dispatch(
            "research_outline",
            {"action": "get"},
            workspace_path=str(tmp_path),
        )
    )
    assert out["success"] is True
    assert out["outline"] == ""
    assert out["sections"] == []


@pytest.mark.asyncio
async def test_research_outline_unknown_action_returns_error(tmp_path, registry):
    out = json.loads(
        await registry.dispatch(
            "research_outline",
            {"action": "ehh"},
            workspace_path=str(tmp_path),
        )
    )
    assert out["success"] is False


@pytest.mark.asyncio
async def test_research_outline_missing_workspace_returns_error(registry):
    out = json.loads(
        await registry.dispatch(
            "research_outline", {"action": "get"},
        )
    )
    assert out["success"] is False
    assert "workspace" in out["error"].lower()
