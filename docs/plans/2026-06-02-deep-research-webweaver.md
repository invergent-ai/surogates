# Deep Research (WebWeaver) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a WebWeaver-style deep-research capability to the surogates agent platform — a *planner* agent that interleaves web search with a living outline and a cited evidence bank, and a *writer* agent that synthesizes a long, citation-grounded report section-by-section — surfaced in `agent-chat-react` with a live outline and a sources/citations panel.

**Architecture:** Port the methodology from `/work/surogates/study/DeepResearch/WebAgent/WebWeaver` onto native surogates primitives. No DeepResearch code runs in production and no new model is served. Two new builtin tools (`research_memory`, `research_outline`) persist to the **shared tenant workspace** (the same `workspace_path` mechanism `file_ops` already uses), so a parent *planner* session and a child *writer* session — spawned via the existing `delegate_task` sub-agent path — share the evidence bank. The planner and writer are declared as platform `AGENT.md` sub-agent types backed by the existing base model. The final report is emitted through the existing `create_artifact` (markdown) path. The UI collects sources from `research_memory` tool results and renders an outline timeline entry, a sources panel, and `[S#]` citation chips.

**Tech Stack:** Python 3.12 (surogates harness, pytest, `asyncio`), TypeScript/React 19 (`agent-chat-react`, vitest, tsup), existing surogates tool/registry/loader/artifact infrastructure.

---

## Status

Updated before each commit. `[x]` done · `[~]` in progress · `[ ]` not started.

- [ ] Task 1 — Research memory-bank pure logic
- [ ] Task 2 — Living-outline pure logic
- [ ] Task 3 — `research_memory` / `research_outline` builtin tools
- [ ] Task 4 — Wire research tools into the builtin registry
- [ ] Task 5 — Planner + writer `AGENT.md` sub-agent types + packaging
- [ ] Task 6 — Manual end-to-end smoke (planner → writer)
- [ ] Task 7 — Collect research sources in runtime state
- [ ] Task 8 — Citation text component (`[S#]` linkification)
- [ ] Task 9 — Research tool renderers (outline + memory)
- [ ] Task 10 — Sources/citations panel
- [ ] Task 11 — Frontend visual verification

---

## Background: why this shape

Verified facts about the existing code that this plan relies on:

- **Tool contract** — `surogates/tools/registry.py`: handlers are `async def handler(arguments: dict, **kwargs) -> str`; registered via `registry.register(name, schema, handler, toolset=...)`. Each builtin module exposes `register(registry)`.
- **Builtin registration** — `surogates/tools/runtime.py` `ToolRuntime.register_builtins()` imports a tuple of builtin modules and calls `mod.register(self.registry)` for each (around lines 50–96).
- **Workspace file IO** — `surogates/tools/builtin/file_ops.py` handlers read `workspace_path = kwargs.get("workspace_path")` and do direct filesystem IO. The workspace is tenant-shared, so a child session sees what the parent wrote (`AgentDef` docstring in `surogates/tools/loader.py`: "The child inherits skills, MCP servers, experts, tenant memory, and workspace from the parent tenant.").
- **Sub-agent types** — `AGENT.md` files under `PLATFORM_AGENTS_DIR` (`/etc/surogates/agents`), loaded by `surogates/tools/loader.py:ResourceLoader.resolve_platform_agent_dir(name)`. Recognised frontmatter keys: `name, description, tools, disallowed_tools, model, max_iterations, policy_profile, category, tags, enabled`.
- **Sub-agent spawn** — `delegate_task` (registered in `surogates/tasks/tools.py`, see also `surogates/tools/builtin/delegate.py`) and `spawn_worker` (`surogates/tools/builtin/coordinator.py`) both accept an `agent_type` argument that resolves an `AgentDef`.
- **Artifacts** — `create_artifact` (`surogates/tools/builtin/artifact.py`, kind `markdown`) renders inline; the SDK already renders it via `src/components/chat/artifacts/artifact-markdown.tsx`.
- **SDK dispatch** — `agent-chat-react/src/components/chat/tool-call-block.tsx` is a `switch (tc.toolName)`; `src/runtime/reducer.ts` `applyAgentChatEvent` is the state reducer; vitest is configured (`npm test`).

Design consequences:

- Memory bank + outline persist as files in `{workspace_path}/.research/` → **no new API routes, no DB migration, no new event types**. Everything flows through existing `tool.call`/`tool.result` and `artifact.*` events.
- The writer has **no web tools** (only `research_memory` + `create_artifact`), matching WebWeaver: it writes only from curated, pre-cited evidence.

---

## File Structure

**Backend (`/work/surogates/surogates`)**

- Create `surogates/research/__init__.py` — package marker.
- Create `surogates/research/memory_bank.py` — pure logic: entry model, JSONL (de)serialization, `add` (assigns `S#`), `retrieve` (keyword scoring). No IO. One responsibility: evidence-bank data logic.
- Create `surogates/research/outline.py` — pure logic for the living outline (normalize/format). Small; kept separate so the tool module stays thin.
- Create `surogates/tools/builtin/research.py` — registers `research_memory` and `research_outline`; handlers do `workspace_path` file IO and call the `research/` logic.
- Modify `surogates/tools/runtime.py` — add `research` to the builtin import tuple and `modules` registration list.
- Create `surogates/platform_assets/agents/deep-research/AGENT.md` — planner sub-agent.
- Create `surogates/platform_assets/agents/research-writer/AGENT.md` — writer sub-agent.
- Test `tests/research/__init__.py`, `tests/research/test_memory_bank.py`, `tests/research/test_outline.py`.
- Test `tests/test_research_tools.py` — tool handlers against a temp workspace.
- Test `tests/research/test_agent_defs.py` — the two AGENT.md assets parse and declare the right tools.

**Frontend (`/work/surogates/sdk/agent-chat-react`)**

- Modify `src/types.ts` — add `AgentChatResearchSource` type and `researchSources` to `AgentChatState`.
- Modify `src/runtime/reducer.ts` — collect sources from `research_memory` tool results.
- Modify `src/runtime/use-agent-chat-runtime.ts` — expose `researchSources` on the runtime API.
- Create `src/components/chat/tools/research-tool.tsx` — renderers for `research_memory` and `research_outline` tool calls.
- Modify `src/components/chat/tool-call-block.tsx` — dispatch the two new tools.
- Create `src/components/research/research-sources-panel.tsx` — the sources/citations sidebar panel.
- Create `src/components/research/citation-text.tsx` — linkifies `[S#]` markers.
- Modify `src/agent-chat.tsx` — wire the sources panel.
- Modify `src/index.ts` — export the new public pieces.
- Test `src/runtime/reducer.research.test.ts` — reducer source collection.
- Test `src/components/research/citation-text.test.tsx` — `[S#]` parsing.

---

## PHASE 1 — Backend: evidence bank + outline + tools

### Task 1: Research memory-bank pure logic

**Files:**
- Create: `surogates/research/__init__.py`
- Create: `surogates/research/memory_bank.py`
- Test: `tests/research/__init__.py`, `tests/research/test_memory_bank.py`

- [ ] **Step 1: Write the failing test**

Create `tests/research/__init__.py` (empty file), then `tests/research/test_memory_bank.py`:

```python
"""Tests for the research evidence-bank pure logic."""

from __future__ import annotations

from surogates.research.memory_bank import (
    MemoryEntry,
    add_entry,
    parse_jsonl,
    retrieve,
    serialize_jsonl,
)


def test_add_entry_assigns_sequential_source_ids():
    entries: list[MemoryEntry] = []
    e1 = add_entry(entries, url="https://a.test", title="A", summary="alpha", evidence=["x"])
    e2 = add_entry(entries, url="https://b.test", title="B", summary="beta", evidence=["y"])
    assert e1.source_id == "S1"
    assert e2.source_id == "S2"
    assert len(entries) == 2


def test_add_entry_dedupes_by_url():
    entries: list[MemoryEntry] = []
    first = add_entry(entries, url="https://a.test", title="A", summary="alpha", evidence=["x"])
    again = add_entry(entries, url="https://a.test", title="A2", summary="alpha2", evidence=["z"])
    assert again.source_id == first.source_id
    assert len(entries) == 1


def test_roundtrip_jsonl():
    entries: list[MemoryEntry] = []
    add_entry(entries, url="https://a.test", title="A", summary="alpha", evidence=["x", "y"])
    text = serialize_jsonl(entries)
    parsed = parse_jsonl(text)
    assert parsed[0].url == "https://a.test"
    assert parsed[0].evidence == ["x", "y"]
    assert parsed[0].source_id == "S1"


def test_parse_jsonl_tolerates_blank_lines_and_garbage():
    text = '{"source_id":"S1","url":"u","title":"t","summary":"s","evidence":[]}\n\nnot-json\n'
    parsed = parse_jsonl(text)
    assert len(parsed) == 1
    assert parsed[0].source_id == "S1"


def test_retrieve_ranks_by_keyword_overlap():
    entries: list[MemoryEntry] = []
    add_entry(entries, url="u1", title="Quantum computing basics",
              summary="qubits and superposition", evidence=["qubit"])
    add_entry(entries, url="u2", title="Baking sourdough",
              summary="flour and starter", evidence=["bread"])
    hits = retrieve(entries, query="qubit superposition", k=1)
    assert len(hits) == 1
    assert hits[0].url == "u1"


def test_retrieve_k_caps_results():
    entries: list[MemoryEntry] = []
    for i in range(5):
        add_entry(entries, url=f"u{i}", title=f"topic {i}", summary="topic", evidence=[])
    hits = retrieve(entries, query="topic", k=3)
    assert len(hits) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /work/surogates && python -m pytest tests/research/test_memory_bank.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'surogates.research'`

- [ ] **Step 3: Write minimal implementation**

Create `surogates/research/__init__.py`:

```python
"""Deep-research support: evidence bank and living-outline logic."""
```

Create `surogates/research/memory_bank.py`:

```python
"""Pure logic for the deep-research evidence bank.

An evidence bank is an ordered list of :class:`MemoryEntry` records, each
a curated, pre-summarized source the writer agent later cites by its stable
``source_id`` (``S1``, ``S2``, ...).  This module is IO-free: callers load
the JSONL from the shared workspace, mutate the list, and serialize it back.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass(slots=True)
class MemoryEntry:
    """One curated source in the evidence bank."""

    source_id: str
    url: str
    title: str
    summary: str
    evidence: list[str] = field(default_factory=list)


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def add_entry(
    entries: list[MemoryEntry],
    *,
    url: str,
    title: str,
    summary: str,
    evidence: list[str] | None = None,
) -> MemoryEntry:
    """Append a new entry (or return the existing one for a duplicate URL).

    Source IDs are assigned sequentially as ``S{n}`` based on the current
    length so they remain stable for the lifetime of a research run.
    """
    for existing in entries:
        if existing.url == url:
            return existing
    entry = MemoryEntry(
        source_id=f"S{len(entries) + 1}",
        url=url,
        title=title,
        summary=summary,
        evidence=list(evidence or []),
    )
    entries.append(entry)
    return entry


def retrieve(entries: list[MemoryEntry], *, query: str, k: int = 5) -> list[MemoryEntry]:
    """Return up to *k* entries ranked by keyword overlap with *query*.

    Scoring is deliberately simple (token-set overlap over title + summary +
    evidence); it keeps the writer's per-section retrieval cheap and
    model-independent.  Ties break toward earlier (more established) sources.
    """
    q = _tokens(query)
    if not q:
        return entries[:k]
    scored: list[tuple[int, int, MemoryEntry]] = []
    for idx, e in enumerate(entries):
        haystack = _tokens(" ".join([e.title, e.summary, *e.evidence]))
        score = len(q & haystack)
        if score > 0:
            scored.append((score, -idx, e))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [e for _, _, e in scored[:k]]


def serialize_jsonl(entries: list[MemoryEntry]) -> str:
    """Serialize the bank as newline-delimited JSON."""
    return "".join(json.dumps(asdict(e), ensure_ascii=False) + "\n" for e in entries)


def parse_jsonl(text: str) -> list[MemoryEntry]:
    """Parse a JSONL bank, skipping blank or malformed lines."""
    out: list[MemoryEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append(
            MemoryEntry(
                source_id=str(obj.get("source_id", "")),
                url=str(obj.get("url", "")),
                title=str(obj.get("title", "")),
                summary=str(obj.get("summary", "")),
                evidence=[str(x) for x in obj.get("evidence", [])],
            )
        )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /work/surogates && python -m pytest tests/research/test_memory_bank.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/research/__init__.py surogates/research/memory_bank.py tests/research/__init__.py tests/research/test_memory_bank.py
git commit -m "feat(research): add evidence-bank pure logic"
```

---

### Task 2: Living-outline pure logic

**Files:**
- Create: `surogates/research/outline.py`
- Test: `tests/research/test_outline.py`

- [ ] **Step 1: Write the failing test**

Create `tests/research/test_outline.py`:

```python
"""Tests for living-outline logic."""

from __future__ import annotations

from surogates.research.outline import normalize_outline, outline_sections


def test_normalize_strips_trailing_space_and_blank_runs():
    raw = "# Title  \n\n\n\n## A   \ncontent\n\n\n"
    out = normalize_outline(raw)
    assert "   \n" not in out
    assert "\n\n\n" not in out
    assert out.endswith("content")


def test_outline_sections_extracts_markdown_headings():
    raw = "# Report\n## Background\ntext\n## Methods\nmore\n### Sub\n"
    assert outline_sections(raw) == ["Background", "Methods", "Sub"]


def test_outline_sections_empty_when_no_headings():
    assert outline_sections("just prose, no headings") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /work/surogates && python -m pytest tests/research/test_outline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'surogates.research.outline'`

- [ ] **Step 3: Write minimal implementation**

Create `surogates/research/outline.py`:

```python
"""Pure logic for the living research outline.

The outline is a markdown document the planner rewrites as the research
direction evolves.  These helpers keep it tidy and let callers enumerate
its sections (used by the writer to drive section-by-section synthesis).
"""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"^#{2,6}\s+(.*\S)\s*$")


def normalize_outline(text: str) -> str:
    """Strip trailing whitespace per line and collapse blank-line runs."""
    lines = [line.rstrip() for line in text.splitlines()]
    collapsed: list[str] = []
    blank = False
    for line in lines:
        if line == "":
            if not blank and collapsed:
                collapsed.append("")
            blank = True
        else:
            collapsed.append(line)
            blank = False
    return "\n".join(collapsed).strip()


def outline_sections(text: str) -> list[str]:
    """Return the heading titles (level 2+) in document order."""
    sections: list[str] = []
    for line in text.splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            sections.append(m.group(1).strip())
    return sections
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /work/surogates && python -m pytest tests/research/test_outline.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/research/outline.py tests/research/test_outline.py
git commit -m "feat(research): add living-outline logic"
```

---

### Task 3: `research_memory` and `research_outline` builtin tools

**Files:**
- Create: `surogates/tools/builtin/research.py`
- Test: `tests/test_research_tools.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_research_tools.py`:

```python
"""Tests for the research builtin tools (workspace-backed)."""

from __future__ import annotations

import json

import pytest

from surogates.tools.builtin import research
from surogates.tools.registry import ToolRegistry


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    research.register(reg)
    return reg


@pytest.mark.asyncio
async def test_research_memory_add_then_list(tmp_path, registry):
    ws = str(tmp_path)
    add = await registry.dispatch(
        "research_memory",
        {"action": "add", "url": "https://a.test", "title": "A",
         "summary": "alpha facts", "evidence": ["e1"]},
        workspace_path=ws,
    )
    add_obj = json.loads(add)
    assert add_obj["success"] is True
    assert add_obj["source_id"] == "S1"

    listing = await registry.dispatch(
        "research_memory", {"action": "list"}, workspace_path=ws,
    )
    list_obj = json.loads(listing)
    assert len(list_obj["sources"]) == 1
    assert list_obj["sources"][0]["url"] == "https://a.test"


@pytest.mark.asyncio
async def test_research_memory_retrieve_ranks(tmp_path, registry):
    ws = str(tmp_path)
    await registry.dispatch(
        "research_memory",
        {"action": "add", "url": "u1", "title": "Quantum",
         "summary": "qubits superposition", "evidence": []},
        workspace_path=ws,
    )
    await registry.dispatch(
        "research_memory",
        {"action": "add", "url": "u2", "title": "Sourdough",
         "summary": "flour starter", "evidence": []},
        workspace_path=ws,
    )
    res = await registry.dispatch(
        "research_memory",
        {"action": "retrieve", "query": "qubits", "k": 1},
        workspace_path=ws,
    )
    obj = json.loads(res)
    assert [s["url"] for s in obj["sources"]] == ["u1"]


@pytest.mark.asyncio
async def test_research_memory_persists_ids_across_calls(tmp_path, registry):
    ws = str(tmp_path)
    await registry.dispatch(
        "research_memory",
        {"action": "add", "url": "u1", "title": "A", "summary": "a"},
        workspace_path=ws,
    )
    second = json.loads(await registry.dispatch(
        "research_memory",
        {"action": "add", "url": "u2", "title": "B", "summary": "b"},
        workspace_path=ws,
    ))
    assert second["source_id"] == "S2"


@pytest.mark.asyncio
async def test_research_outline_set_and_get(tmp_path, registry):
    ws = str(tmp_path)
    setres = json.loads(await registry.dispatch(
        "research_outline",
        {"action": "set", "outline": "# R\n## Background\ntext\n"},
        workspace_path=ws,
    ))
    assert setres["success"] is True
    assert setres["sections"] == ["Background"]

    getres = json.loads(await registry.dispatch(
        "research_outline", {"action": "get"}, workspace_path=ws,
    ))
    assert "Background" in getres["outline"]


@pytest.mark.asyncio
async def test_missing_workspace_returns_error(registry):
    res = json.loads(await registry.dispatch(
        "research_memory", {"action": "list"},
    ))
    assert res["success"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /work/surogates && python -m pytest tests/test_research_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'surogates.tools.builtin.research'`

- [ ] **Step 3: Write minimal implementation**

Create `surogates/tools/builtin/research.py`:

```python
"""Builtin deep-research tools: ``research_memory`` and ``research_outline``.

Both persist to the **shared tenant workspace** under ``.research/`` so a
parent planner session and a child writer session see the same evidence
bank and outline.  This mirrors how ``file_ops`` uses the ``workspace_path``
kwarg injected by the harness; no API server or database is involved.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any

from surogates.research.memory_bank import (
    add_entry,
    parse_jsonl,
    retrieve,
    serialize_jsonl,
)
from surogates.research.outline import normalize_outline, outline_sections
from surogates.tools.registry import ToolRegistry, ToolSchema

_RESEARCH_DIR = ".research"
_MEMORY_FILE = "memory.jsonl"
_OUTLINE_FILE = "outline.md"


def _research_root(workspace_path: str) -> str:
    root = os.path.join(workspace_path, _RESEARCH_DIR)
    os.makedirs(root, exist_ok=True)
    return root


def _err(msg: str) -> str:
    return json.dumps({"success": False, "error": msg}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# research_memory
# ---------------------------------------------------------------------------

_MEMORY_SCHEMA = ToolSchema(
    name="research_memory",
    description=(
        "Curated evidence bank for deep research. Record each useful source "
        "once with a concise summary and verbatim evidence quotes; the writer "
        "later cites sources by their returned source_id (e.g. S3).\n\n"
        "ACTIONS:\n"
        "- add: store a source. Provide url, title, summary, and evidence "
        "(short verbatim quotes). Returns a stable source_id.\n"
        "- retrieve: get the sources most relevant to a query/section "
        "(use this per report section while writing).\n"
        "- list: return every source in order (use for the References section)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "retrieve", "list"]},
            "url": {"type": "string", "description": "Source URL (action=add)."},
            "title": {"type": "string", "description": "Source title (action=add)."},
            "summary": {
                "type": "string",
                "description": "Concise summary of the source (action=add).",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short verbatim quotes supporting claims (action=add).",
            },
            "query": {
                "type": "string",
                "description": "What to retrieve relevant sources for (action=retrieve).",
            },
            "k": {
                "type": "integer",
                "description": "Max sources to return (action=retrieve). Default 5.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
)


async def _research_memory_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    workspace_path = kwargs.get("workspace_path")
    if not workspace_path:
        return _err("research_memory requires a workspace; none is available.")

    path = os.path.join(_research_root(workspace_path), _MEMORY_FILE)
    text = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    entries = parse_jsonl(text)

    action = arguments.get("action", "")
    if action == "add":
        url = (arguments.get("url") or "").strip()
        if not url:
            return _err("action=add requires a url.")
        entry = add_entry(
            entries,
            url=url,
            title=arguments.get("title", ""),
            summary=arguments.get("summary", ""),
            evidence=arguments.get("evidence") or [],
        )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(serialize_jsonl(entries))
        return json.dumps(
            {"success": True, "source_id": entry.source_id, "url": entry.url,
             "title": entry.title, "total": len(entries)},
            ensure_ascii=False,
        )

    if action == "retrieve":
        hits = retrieve(entries, query=arguments.get("query", ""),
                        k=int(arguments.get("k", 5)))
        return json.dumps(
            {"success": True, "sources": [asdict(e) for e in hits]},
            ensure_ascii=False,
        )

    if action == "list":
        return json.dumps(
            {"success": True, "sources": [asdict(e) for e in entries]},
            ensure_ascii=False,
        )

    return _err(f"Unknown action: {action!r}")


# ---------------------------------------------------------------------------
# research_outline
# ---------------------------------------------------------------------------

_OUTLINE_SCHEMA = ToolSchema(
    name="research_outline",
    description=(
        "The living research outline (a markdown document). As your research "
        "evolves, rewrite the whole outline to reflect new structure and "
        "open questions. Use markdown headings (## / ###) for sections.\n\n"
        "ACTIONS:\n"
        "- set: replace the outline with the provided markdown.\n"
        "- get: return the current outline."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["set", "get"]},
            "outline": {
                "type": "string",
                "description": "Full markdown outline (action=set).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
)


async def _research_outline_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    workspace_path = kwargs.get("workspace_path")
    if not workspace_path:
        return _err("research_outline requires a workspace; none is available.")

    path = os.path.join(_research_root(workspace_path), _OUTLINE_FILE)
    action = arguments.get("action", "")

    if action == "set":
        outline = normalize_outline(arguments.get("outline", ""))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(outline)
        return json.dumps(
            {"success": True, "sections": outline_sections(outline)},
            ensure_ascii=False,
        )

    if action == "get":
        outline = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                outline = fh.read()
        return json.dumps(
            {"success": True, "outline": outline,
             "sections": outline_sections(outline)},
            ensure_ascii=False,
        )

    return _err(f"Unknown action: {action!r}")


def register(registry: ToolRegistry) -> None:
    """Register the deep-research tools."""
    registry.register(
        name="research_memory",
        schema=_MEMORY_SCHEMA,
        handler=_research_memory_handler,
        toolset="research",
    )
    registry.register(
        name="research_outline",
        schema=_OUTLINE_SCHEMA,
        handler=_research_outline_handler,
        toolset="research",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /work/surogates && python -m pytest tests/test_research_tools.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/tools/builtin/research.py tests/test_research_tools.py
git commit -m "feat(research): add research_memory and research_outline tools"
```

---

### Task 4: Wire the research tools into the builtin registry

**Files:**
- Modify: `surogates/tools/runtime.py` (the `register_builtins` import tuple, around lines 50–96)
- Test: `tests/test_research_registration.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_research_registration.py`:

```python
"""The research tools must be registered by the default ToolRuntime."""

from __future__ import annotations

from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime


def test_research_tools_registered_by_default():
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    names = registry.tool_names
    assert "research_memory" in names
    assert "research_outline" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /work/surogates && python -m pytest tests/test_research_registration.py -v`
Expected: FAIL — `research_memory`/`research_outline` not in `tool_names`.

- [ ] **Step 3: Add `research` to the builtin import tuple**

In `surogates/tools/runtime.py`, add `research` to both the import block inside `register_builtins` and the `modules = [...]` list. Keep it near the other harness-local builtin modules:

```python
        from surogates.tools.builtin import (
            artifact,
            # existing modules omitted here for brevity
            memory,
            research,
            session_search,
            terminal,  # also registers the 'process' tool
            todo,
        )
```

```python
        modules = [
            memory,
            research,
            skills,
            # keep the rest of the existing modules in their current order
        ]
```

Do not remove or reorder unrelated modules; the final `for mod in modules:` loop will call `research.register(self.registry)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /work/surogates && python -m pytest tests/test_research_registration.py tests/test_research_tools.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/tools/runtime.py tests/test_research_registration.py
git commit -m "feat(research): register research tools in default runtime"
```

---

### Task 5: Planner and writer `AGENT.md` sub-agent types

**Files:**
- Create: `surogates/platform_assets/agents/deep-research/AGENT.md`
- Create: `surogates/platform_assets/agents/research-writer/AGENT.md`
- Modify: `pyproject.toml`
- Modify: `images/api/Dockerfile`
- Modify: `images/worker/Dockerfile`
- Test: `tests/research/test_agent_defs.py`

- [ ] **Step 1: Write the failing test**

Create `tests/research/test_agent_defs.py`:

```python
"""The platform research AGENT.md assets must exist and parse correctly."""

from __future__ import annotations

from pathlib import Path

import yaml

AGENTS_ROOT = Path(__file__).resolve().parents[2] / "surogates" / "platform_assets" / "agents"


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} missing YAML frontmatter"
    _, fm, _body = text.split("---\n", 2)
    return yaml.safe_load(fm)


def test_planner_agent_def():
    fm = _frontmatter(AGENTS_ROOT / "deep-research" / "AGENT.md")
    assert fm["name"] == "deep-research"
    assert "research_memory" in fm["tools"]
    assert "research_outline" in fm["tools"]
    assert "web_search" in fm["tools"]
    assert "delegate_task" in fm["tools"]
    # The planner must NOT write the report itself.
    assert "create_artifact" not in fm["tools"]


def test_writer_agent_def():
    fm = _frontmatter(AGENTS_ROOT / "research-writer" / "AGENT.md")
    assert fm["name"] == "research-writer"
    assert "research_memory" in fm["tools"]
    assert "create_artifact" in fm["tools"]
    # The writer has no web access — it writes only from curated evidence.
    assert "web_search" not in fm["tools"]
    assert "web_extract" not in fm["tools"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /work/surogates && python -m pytest tests/research/test_agent_defs.py -v`
Expected: FAIL — files do not exist.

- [ ] **Step 3: Create the AGENT.md assets**

Create `surogates/platform_assets/agents/deep-research/AGENT.md`:

```markdown
---
name: deep-research
description: >-
  Plans and executes deep, multi-source research. Interleaves web search with a
  living outline and a cited evidence bank, then delegates report writing to the
  research-writer sub-agent. Use for open-ended research questions that need a
  thorough, citation-grounded report.
tools:
  - web_search
  - web_extract
  - research_memory
  - research_outline
  - delegate_task
  - ask_user_question
max_iterations: 60
category: research
tags: [research, planner]
---

You are the **planner** in a two-agent deep-research workflow. Your job is to
*explore and structure* the topic, not to write the final report.

Operate in a loop, the way an expert human researcher works:

1. **Decompose** the question into the key sub-questions that a complete answer
   must cover. Capture them as an initial outline with `research_outline(action="set", ...)`.
2. **Search and read.** Use `web_search` to find candidate sources and
   `web_extract` to read the promising ones. Cast a wide net across diverse,
   credible sources.
3. **Curate evidence.** For every source that genuinely informs the question,
   call `research_memory(action="add", url, title, summary, evidence)`. Write a
   concise `summary` and include short *verbatim* quotes in `evidence`. Record
   the returned `source_id` mentally — the writer will cite by it.
4. **Refine the outline.** Treat the outline as a *living document*: after new
   discoveries, rewrite it with `research_outline(action="set", ...)` so the
   structure reflects what you have actually found and the open gaps that remain.
   Do not let an early outline fossilize.
5. **Decide when to stop.** Stop searching when new searches stop changing the
   outline and stop adding materially new evidence — i.e. the outline is stable
   and the major sub-questions are each backed by multiple sources. Avoid
   endless searching.

When the outline is saturated, hand off to the writer:

- Call `delegate_task` with `agent_type="research-writer"`. In the `goal`,
  paste the **full final outline**. In the `context`, state the original
  research question and remind the writer that the shared evidence bank is
  available via `research_memory` and that every claim must cite a `source_id`.

If the question is ambiguous or under-scoped, ask the user to clarify with
`ask_user_question` *before* spending a large search budget.

Be rigorous and objective. Prefer primary and authoritative sources. Note
disagreements between sources rather than silently picking one.
```

Create `surogates/platform_assets/agents/research-writer/AGENT.md`:

```markdown
---
name: research-writer
description: >-
  Writes a long, citation-grounded research report section-by-section from a
  curated evidence bank. Invoked by the deep-research planner; not intended for
  direct use.
tools:
  - research_memory
  - create_artifact
max_iterations: 40
category: research
tags: [research, writer]
---

You are the **writer** in a two-agent deep-research workflow. The planner has
already gathered evidence into a shared bank and produced an outline (provided
in your goal). You have **no web access** — write *only* from the curated
evidence bank.

Work section-by-section to keep each step grounded and avoid losing the thread:

1. Read the outline you were given. Identify its sections in order.
2. For each section, call `research_memory(action="retrieve", query="<section
   topic>", k=8)` to pull the most relevant sources for *that* section.
3. Write the section using only those sources. Support every non-obvious claim
   with an inline citation in the form `[S3]` (the `source_id` from the bank).
   Never invent a `source_id`.
4. After all sections, call `research_memory(action="list")` and write a
   **References** section mapping each cited `[S#]` to its title and URL.

Finally, emit the complete report as a single artifact:

- `create_artifact(name="<report title>", kind="markdown", spec={"content": "<full report markdown>"})`

Write comprehensively and readably: clear headings, well-structured prose,
balanced coverage of the sub-questions, and faithful, accurate citations. Do not
include claims the evidence bank does not support.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /work/surogates && python -m pytest tests/research/test_agent_defs.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Package and deploy platform agents**

The loader reads platform agents from `PLATFORM_AGENTS_DIR` (`/etc/surogates/agents`, see `surogates/tools/loader.py`). API pods need the files for `GET /agents`; worker pods need the same files so `delegate_task(agent_type="research-writer")` can resolve the writer.

In `pyproject.toml`, extend the existing package-data entry:

```toml
surogates = ["web/dist/**/*", "harness/prompts/**/*.md", "platform_assets/agents/**/*.md"]
```

In `images/api/Dockerfile`, add `/etc/surogates/agents` to the existing `RUN mkdir -p /etc/surogates/policies ...` block and copy the assets near the existing skills copy:

```dockerfile
RUN mkdir -p /etc/surogates/policies \
             /etc/surogates/skills \
             /etc/surogates/agents \
    && chown -R surogates:surogates /etc/surogates

COPY skills/ /etc/surogates/skills/
COPY surogates/platform_assets/agents/ /etc/surogates/agents/
```

In `images/worker/Dockerfile`, add the same directory and copy line:

```dockerfile
RUN mkdir -p /etc/surogates/policies \
             /etc/surogates/skills \
             /etc/surogates/tools \
             /etc/surogates/mcp \
             /etc/surogates/agents \
    && chown -R surogates:surogates /etc/surogates

COPY skills/ /etc/surogates/skills/
COPY surogates/platform_assets/agents/ /etc/surogates/agents/
```

Run: `cd /work/surogates && python -m build --wheel`
Expected: the wheel builds and includes `surogates/platform_assets/agents/deep-research/AGENT.md` and `surogates/platform_assets/agents/research-writer/AGENT.md`.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/platform_assets/agents/deep-research/AGENT.md \
        surogates/platform_assets/agents/research-writer/AGENT.md \
        pyproject.toml images/api/Dockerfile images/worker/Dockerfile \
        tests/research/test_agent_defs.py
git commit -m "feat(research): add deep-research planner and research-writer agent types"
```

- [ ] **Step 7: Run the full backend research test suite**

Run: `cd /work/surogates && python -m pytest tests/research tests/test_research_tools.py tests/test_research_registration.py -v`
Expected: all PASS.

---

### Task 6: Manual end-to-end smoke (planner → writer)

**Files:** none (verification only)

- [ ] **Step 1: Start a local server and a session**

Follow the project's local-dev path (`surogate-ops server`, local k3d). In a chat session, send:
`Use the deep-research agent to research: "What are the leading approaches to long-context retrieval in LLM agents in 2025, and their trade-offs?"`

- [ ] **Step 2: Observe planner behavior**

Expected (in Expert mode, using existing tool renderers): `web_search` / `web_extract` calls, repeated `research_memory action=add` calls returning `S1, S2, ...`, and `research_outline action=set` calls. Confirm `{workspace_path}/.research/memory.jsonl` and `outline.md` are written.

- [ ] **Step 3: Observe handoff and writer**

Expected: a `delegate_task` call with `agent_type="research-writer"`; the child session issues `research_memory action=retrieve` per section and finishes with a `create_artifact` (markdown) report whose body contains `[S#]` citations and a References section.

- [ ] **Step 4: Record findings**

Note research quality with the base model, planner termination behavior, and citation accuracy. These inform Phase 3 tuning. No commit.

---

## PHASE 2 — Frontend: outline + sources/citations panel

### Task 7: Collect research sources in runtime state

**Files:**
- Modify: `src/types.ts`
- Modify: `src/runtime/reducer.ts`
- Test: `src/runtime/reducer.research.test.ts`

- [ ] **Step 1: Write the failing test**

Create `src/runtime/reducer.research.test.ts`:

```typescript
import { describe, expect, it } from "vitest";
import { applyAgentChatEvent, createInitialAgentChatState } from "./reducer";

function toolCall(name: string, callId: string) {
  return {
    type: "tool.call" as const,
    eventId: 1,
    data: { tool_call_id: callId, name, arguments: "{}" },
  };
}

function toolResult(result: string, callId: string, eventId = 2) {
  return {
    type: "tool.result" as const,
    eventId,
    data: { tool_call_id: callId, result },
  };
}

describe("research source collection", () => {
  it("adds a source when research_memory add succeeds", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false, viewMode: "expert" });
    state = applyAgentChatEvent(state, toolCall("research_memory", "c1"));
    state = applyAgentChatEvent(
      state,
      toolResult(
        JSON.stringify({ success: true, source_id: "S1", url: "https://a.test", title: "A" }),
        "c1",
      ),
    );
    expect(state.researchSources).toHaveLength(1);
    expect(state.researchSources[0]).toMatchObject({ sourceId: "S1", url: "https://a.test", title: "A" });
  });

  it("dedupes by sourceId", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false, viewMode: "expert" });
    state = applyAgentChatEvent(state, toolCall("research_memory", "c1"));
    const ev = toolResult(
      JSON.stringify({ success: true, source_id: "S1", url: "https://a.test", title: "A" }),
      "c1",
    );
    state = applyAgentChatEvent(state, ev);
    state = applyAgentChatEvent(state, toolResult(
      JSON.stringify({ success: true, source_id: "S1", url: "https://a.test", title: "A" }),
      "c1",
      3,
    ));
    expect(state.researchSources).toHaveLength(1);
  });

  it("ignores non-research tool results", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false, viewMode: "expert" });
    state = applyAgentChatEvent(state, toolCall("web_search", "c2"));
    state = applyAgentChatEvent(state, toolResult("{}", "c2"));
    expect(state.researchSources).toHaveLength(0);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /work/surogates/sdk/agent-chat-react && npm test -- reducer.research`
Expected: FAIL — `researchSources` is undefined / property missing on state type.

- [ ] **Step 3: Add the type and state field**

In `src/types.ts`, add the source type near the other state types and extend `AgentChatState`:

```typescript
export interface AgentChatResearchSource {
  sourceId: string; // e.g. "S3"
  url: string;
  title: string;
}
```

Then add to the `AgentChatState` interface (alongside `browser`, `viewMode`, etc.):

```typescript
  researchSources: AgentChatResearchSource[];
```

In `src/runtime/reducer.ts`, initialize the field in `createInitialAgentChatState`:

```typescript
    researchSources: [],
```

- [ ] **Step 4: Handle the tool result in the reducer**

In `src/runtime/reducer.ts`, add this helper near the other reducer helpers:

```typescript
function collectResearchSource(
  state: AgentChatState,
  toolName: string | null,
  data: Record<string, unknown>,
): AgentChatState {
  if (toolName !== "research_memory") return state;
  const rawResult = data.content ?? data.result;
  const result = typeof rawResult === "string"
    ? rawResult
    : JSON.stringify(rawResult ?? {});
  try {
    const parsed = JSON.parse(result) as {
      success?: boolean;
      source_id?: string;
      url?: string;
      title?: string;
    };
    if (!parsed.success || !parsed.source_id || !parsed.url) return state;
    if (state.researchSources.some((s) => s.sourceId === parsed.source_id)) {
      return state;
    }
    return {
      ...state,
      researchSources: [
        ...state.researchSources,
        {
          sourceId: parsed.source_id,
          url: parsed.url,
          title: parsed.title ?? "",
        },
      ],
    };
  } catch {
    return state;
  }
}
```

Then update the existing `tool.result` case. It already derives `toolName` with `findToolNameById(...)`; use that derived value rather than expecting the `tool.result` event to carry a `name` field:

```typescript
      const withResult = {
        ...nextState,
        messages,
        workspaceRefreshKey: mutatesWorkspace
          ? nextState.workspaceRefreshKey + 1
          : nextState.workspaceRefreshKey,
      };
      return collectResearchSource(withResult, toolName, event.data);
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /work/surogates/sdk/agent-chat-react && npm test -- reducer.research`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
cd /work/surogates/sdk/agent-chat-react
git add src/types.ts src/runtime/reducer.ts src/runtime/reducer.research.test.ts
git commit -m "feat(agent-chat): collect deep-research sources in runtime state"
```

---

### Task 8: Citation text component (`[S#]` linkification)

**Files:**
- Create: `src/components/research/citation-text.tsx`
- Test: `src/components/research/citation-text.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `src/components/research/citation-text.test.tsx`:

```typescript
import { describe, expect, it } from "vitest";
import { splitCitations } from "./citation-text";

describe("splitCitations", () => {
  it("splits plain text with no citations into one text segment", () => {
    expect(splitCitations("hello world")).toEqual([{ kind: "text", value: "hello world" }]);
  });

  it("extracts a single citation", () => {
    expect(splitCitations("see [S3] for details")).toEqual([
      { kind: "text", value: "see " },
      { kind: "cite", value: "S3" },
      { kind: "text", value: " for details" },
    ]);
  });

  it("extracts multiple and comma-grouped citations", () => {
    expect(splitCitations("a [S1] b [S2, S3]")).toEqual([
      { kind: "text", value: "a " },
      { kind: "cite", value: "S1" },
      { kind: "text", value: " b " },
      { kind: "cite", value: "S2" },
      { kind: "cite", value: "S3" },
    ]);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /work/surogates/sdk/agent-chat-react && npm test -- citation-text`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

Create `src/components/research/citation-text.tsx`:

```typescript
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renders inline [S#] citation markers as clickable chips that resolve
// against the collected research sources.

import type { AgentChatResearchSource } from "../../types";

export type CitationSegment =
  | { kind: "text"; value: string }
  | { kind: "cite"; value: string };

const CITATION_RE = /\[(S\d+(?:\s*,\s*S\d+)*)\]/g;

/** Split text into plain-text and citation segments. Comma-grouped
 *  markers like `[S2, S3]` expand into individual `cite` segments. */
export function splitCitations(text: string): CitationSegment[] {
  const segments: CitationSegment[] = [];
  let lastIndex = 0;
  for (const match of text.matchAll(CITATION_RE)) {
    const start = match.index ?? 0;
    if (start > lastIndex) {
      segments.push({ kind: "text", value: text.slice(lastIndex, start) });
    }
    for (const id of match[1].split(",")) {
      segments.push({ kind: "cite", value: id.trim() });
    }
    lastIndex = start + match[0].length;
  }
  if (lastIndex < text.length) {
    segments.push({ kind: "text", value: text.slice(lastIndex) });
  }
  return segments;
}

export function CitationText({
  text,
  sources,
  onCitationClick,
}: {
  text: string;
  sources: AgentChatResearchSource[];
  onCitationClick?: (sourceId: string) => void;
}) {
  const byId = new Map(sources.map((s) => [s.sourceId, s]));
  return (
    <>
      {splitCitations(text).map((seg, i) => {
        if (seg.kind === "text") return <span key={i}>{seg.value}</span>;
        const src = byId.get(seg.value);
        return (
          <button
            key={i}
            type="button"
            title={src ? `${src.title} — ${src.url}` : seg.value}
            onClick={() => onCitationClick?.(seg.value)}
            className="mx-0.5 inline-flex items-center rounded-sm bg-muted px-1 text-[10px] font-semibold text-primary hover:bg-primary/10"
          >
            {seg.value}
          </button>
        );
      })}
    </>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /work/surogates/sdk/agent-chat-react && npm test -- citation-text`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd /work/surogates/sdk/agent-chat-react
git add src/components/research/citation-text.tsx src/components/research/citation-text.test.tsx
git commit -m "feat(agent-chat): add [S#] citation linkification"
```

---

### Task 9: Research tool renderers (outline + memory)

**Files:**
- Create: `src/components/chat/tools/research-tool.tsx`
- Modify: `src/components/chat/tool-call-block.tsx`

- [ ] **Step 1: Write the renderers**

Create `src/components/chat/tools/research-tool.tsx`. Follow the existing one-liner tool renderers in `src/components/chat/tools/oneliner-tools.tsx` for the status/parsing helpers (`shared.ts`):

```typescript
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renderers for the deep-research tools: research_outline (a living
// outline card) and research_memory (a compact source-recorded line).

import type { ToolCallInfo } from "../../../types";
import { parseArgs } from "./shared";

export function ResearchOutlineBlock({ tc }: { tc: ToolCallInfo }) {
  const args = parseArgs<{ action?: string; outline?: string }>(tc.args) ?? {};
  const result = tc.result
    ? parseArgs<{ outline?: string; sections?: string[] }>(tc.result) ?? {}
    : {};
  const outline = args.action === "set" ? args.outline ?? "" : result.outline ?? "";
  const sections = result.sections ?? [];
  return (
    <div className="rounded-sm border border-border bg-muted/40 p-2 text-xs">
      <div className="mb-1 font-semibold uppercase tracking-widest text-muted-foreground">
        Research outline{sections.length ? ` · ${sections.length} sections` : ""}
      </div>
      {outline ? (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-snug">
          {outline}
        </pre>
      ) : (
        <span className="text-muted-foreground">updated</span>
      )}
    </div>
  );
}

export function ResearchMemoryBlock({ tc }: { tc: ToolCallInfo }) {
  const args = parseArgs<{ action?: string; url?: string; query?: string }>(tc.args) ?? {};
  const result = tc.result ? parseArgs<{
    source_id?: string;
    sources?: { source_id: string }[];
  }>(tc.result) ?? {} : {};
  let label: string;
  if (args.action === "add") {
    label = `Recorded source ${result.source_id ?? ""}${args.url ? ` · ${hostname(args.url)}` : ""}`;
  } else if (args.action === "retrieve") {
    label = `Retrieved ${result.sources?.length ?? 0} sources${args.query ? ` for "${truncate(args.query)}"` : ""}`;
  } else {
    label = `Listed ${result.sources?.length ?? 0} sources`;
  }
  return (
    <div className="text-xs text-muted-foreground">
      <span className="font-semibold text-foreground">research</span> {label}
    </div>
  );
}

function hostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function truncate(s: string, n = 40): string {
  return s.length > n ? `${s.slice(0, n)}…` : s;
}
```

- [ ] **Step 2: Dispatch the new tools**

In `src/components/chat/tool-call-block.tsx`, add the import and two `case`s to the `switch (tc.toolName)`:

```typescript
import { ResearchOutlineBlock, ResearchMemoryBlock } from "./tools/research-tool";
```

```typescript
    case "research_outline":
      return <ResearchOutlineBlock tc={tc} />;

    case "research_memory":
      return <ResearchMemoryBlock tc={tc} />;
```

- [ ] **Step 3: Verify typecheck**

Run: `cd /work/surogates/sdk/agent-chat-react && npm run typecheck`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
cd /work/surogates/sdk/agent-chat-react
git add src/components/chat/tools/research-tool.tsx src/components/chat/tool-call-block.tsx
git commit -m "feat(agent-chat): render research_outline and research_memory tool calls"
```

---

### Task 10: Sources/citations panel

**Files:**
- Create: `src/components/research/research-sources-panel.tsx`
- Modify: `src/runtime/use-agent-chat-runtime.ts` (expose `researchSources`)
- Modify: `src/agent-chat.tsx` (wire the panel)
- Modify: `src/index.ts` (exports)

- [ ] **Step 1: Expose `researchSources` on the runtime API**

In `src/types.ts`, add to the `AgentChatRuntimeApi` interface:

```typescript
  researchSources: AgentChatResearchSource[];
```

In `src/runtime/use-agent-chat-runtime.ts`, return it from the hook (alongside `messages`, `viewMode`, etc.):

```typescript
    researchSources: state.researchSources,
```

- [ ] **Step 2: Write the panel**

Create `src/components/research/research-sources-panel.tsx`:

```typescript
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Sidebar panel listing the curated research sources. Citation chips
// ([S#]) in the report deep-link here via element id `source-<id>`.

import type { AgentChatResearchSource } from "../../types";

export function ResearchSourcesPanel({
  sources,
}: {
  sources: AgentChatResearchSource[];
}) {
  if (sources.length === 0) {
    return (
      <div className="p-3 text-xs text-muted-foreground">
        No research sources yet. Sources appear here as the deep-research agent
        curates evidence.
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-1 p-2">
      <div className="px-1 pb-1 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
        Sources · {sources.length}
      </div>
      {sources.map((s) => (
        <a
          key={s.sourceId}
          id={`source-${s.sourceId}`}
          href={s.url}
          target="_blank"
          rel="noreferrer"
          className="group flex items-baseline gap-2 rounded-sm px-1 py-1 hover:bg-muted"
        >
          <span className="shrink-0 text-[10px] font-semibold text-primary">{s.sourceId}</span>
          <span className="flex flex-col overflow-hidden">
            <span className="truncate text-xs text-foreground group-hover:underline">
              {s.title || s.url}
            </span>
            <span className="truncate text-[10px] text-muted-foreground">{hostname(s.url)}</span>
          </span>
        </a>
      ))}
    </div>
  );
}

function hostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}
```

- [ ] **Step 3: Wire the panel into `AgentChat`**

In `src/agent-chat.tsx`, render `ResearchSourcesPanel` in the right-stack area next to the existing workspace/browser panes, gated on `runtime.researchSources.length > 0` (follow the existing pattern that conditionally shows `WorkspacePanel`/`BrowserPane`). Pass `sources={runtime.researchSources}`. Make the citation chip `onCitationClick` (Task 8) scroll to `#source-<id>`:

```typescript
const scrollToSource = (id: string) => {
  document.getElementById(`source-${id}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
};
```

Wire `scrollToSource` through to wherever the markdown report renders citations (the report artifact / final assistant text), using the `CitationText` component for any assistant text that contains `[S#]` while `researchSources.length > 0`.

- [ ] **Step 4: Export new public API**

In `src/index.ts`, add:

```typescript
export { ResearchSourcesPanel } from "./components/research/research-sources-panel";
export { CitationText, splitCitations } from "./components/research/citation-text";
export type { AgentChatResearchSource } from "./types";
```

- [ ] **Step 5: Verify typecheck and build**

Run: `cd /work/surogates/sdk/agent-chat-react && npm run typecheck && npm run build`
Expected: typecheck clean; tsup build produces `dist/` with no errors.

- [ ] **Step 6: Run the full SDK test suite**

Run: `cd /work/surogates/sdk/agent-chat-react && npm test`
Expected: all tests pass (including the new reducer + citation tests).

- [ ] **Step 7: Commit**

```bash
cd /work/surogates/sdk/agent-chat-react
git add src/components/research/research-sources-panel.tsx src/runtime/use-agent-chat-runtime.ts src/agent-chat.tsx src/index.ts src/types.ts
git commit -m "feat(agent-chat): add research sources/citations panel"
```

---

### Task 11: Frontend visual verification

**Files:** none (verification only)

- [ ] **Step 1: Consume the SDK in the host app**

Build the SDK (`npm run build`) and run the consuming web UI (`frontend/`, `npm run dev`). Re-run the deep-research smoke from Task 6.

- [ ] **Step 2: Verify the research UX**

Expected: outline cards render and update during the planner phase; `research_memory` lines appear ("Recorded source S3 · …"); the sources panel populates with `S#` entries linking out; the final markdown report renders with `[S#]` chips that scroll to the matching source in the panel.

- [ ] **Step 3: Verify Simple vs Expert modes**

Confirm Simple mode collapses the planner iterations sensibly and still surfaces the final report + sources panel; Expert mode shows the full timeline. No commit.

---

## PHASE 3 — Tuning (follow-up, scope as a separate plan if large)

These are deferred items surfaced by Tasks 6 and 11; each becomes its own task/plan once Phase 1–2 data is in hand:

- **Planner termination heuristic** — if the model over-searches, add an explicit "outline unchanged for N iterations → hand off" instruction or a lightweight `research_outline` change-detector signal in the prompt.
- **Evidence summary quality** — if `web_extract` output is noisy, add an extraction step (an LLM summary pass like WebWeaver's `EXTRACTOR_PROMPT`) before `research_memory.add`.
- **Citation accuracy guardrail** — optional post-write check that every `[S#]` in the report exists in the bank.
- **Eval harness** — reuse prompts from `study/DeepResearch/WebAgent/WebWeaver/eval_data/sample.jsonl` as a small regression set scored for comprehensiveness + citation accuracy.
- **Retrieval upgrade** — if keyword overlap under-retrieves, swap `retrieve()` scoring for embeddings (the platform already has an LLM/embeddings client) behind the same function signature.

---

## Self-Review

**Spec coverage** (against the four locked decisions):

- *WebWeaver dual-agent (planner + writer, memory bank, dynamic outline, section-by-section writing)* → Tasks 1–5 (bank logic, outline logic, tools, planner AGENT.md, writer AGENT.md). ✓
- *Sub-agent type + memory tool integration depth* → planner/writer `AGENT.md` (Task 5), `research_memory`/`research_outline` tools (Task 3), spawned via existing `delegate_task`. ✓
- *Existing base model* → no model serving; `AGENT.md` omits a `model:` override, inheriting the session's base model. ✓
- *Outline + citations panel UI* → outline renderer (Task 9), sources/citations panel + `[S#]` chips (Tasks 8, 10). ✓

**Placeholder scan:** Every code step contains complete, runnable code. Repo-specific constructor/helper names have been resolved against the current checkout (`ToolRuntime(registry).register_builtins()`, `createInitialAgentChatState` from `reducer.ts`, and `parseArgs` from `shared.ts`). No deferred-work placeholders remain.

**Type consistency:** `MemoryEntry(source_id, url, title, summary, evidence)` is used identically across `memory_bank.py`, `research.py`, and tests. The tool JSON contract (`success`, `source_id`, `url`, `title`, `sources[]`, `outline`, `sections[]`) is consistent between `research.py` handlers and both the backend tests and the frontend reducer/renderers. The TS `AgentChatResearchSource{ sourceId, url, title }` is consistent across `types.ts`, reducer, runtime API, panel, and `CitationText`. Tool names `research_memory` / `research_outline` match between backend registration, AGENT.md `tools:` lists, and the SDK dispatch `case`s.
