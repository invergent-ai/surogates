"""Builtin deep-research tools: ``research_memory`` and ``research_outline``.

Both persist to the **shared tenant workspace** under ``.research/`` so a
parent planner session and a child writer session see the same evidence
bank and outline.  This mirrors how ``file_ops`` uses the
``workspace_path`` kwarg injected by the harness; no API server or
database is involved.

Wire-level shape of the tool result (single JSON string on every
return so the LLM can parse it without branching):

* On success: ``{"success": true, ...action-specific keys...}``.
* On failure: ``{"success": false, "error": "<reason>"}``.

The handlers swallow no exceptions internally — file-IO failures are
surfaced as a structured error envelope, but unexpected exceptions
propagate to the registry's exception handler so a real bug in this
module turns into a visible ``tool execution failed`` line in the
session log rather than a silent miss.
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

__all__ = ["register"]


# Layout under the per-session workspace.  Kept private — the schema
# is what the LLM sees, the on-disk shape is an implementation detail
# the planner / writer never touch directly.
_RESEARCH_DIR = ".research"
_MEMORY_FILE = "memory.jsonl"
_OUTLINE_FILE = "outline.md"


def _research_root(workspace_path: str) -> str:
    """Return ``{workspace}/.research``, creating it on demand."""
    root = os.path.join(workspace_path, _RESEARCH_DIR)
    os.makedirs(root, exist_ok=True)
    return root


def _err(msg: str) -> str:
    return json.dumps({"success": False, "error": msg}, ensure_ascii=False)


def _ok(payload: dict[str, Any]) -> str:
    payload = {"success": True, **payload}
    return json.dumps(payload, ensure_ascii=False)


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
            "action": {
                "type": "string",
                "enum": ["add", "retrieve", "list"],
            },
            "url": {
                "type": "string",
                "description": "Source URL (action=add).",
            },
            "title": {
                "type": "string",
                "description": "Source title (action=add).",
            },
            "summary": {
                "type": "string",
                "description": (
                    "Concise summary of the source (action=add)."
                ),
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Short verbatim quotes supporting claims (action=add)."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "What to retrieve relevant sources for (action=retrieve)."
                ),
            },
            "k": {
                "type": "integer",
                "description": (
                    "Max sources to return (action=retrieve). Default 5."
                ),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
)


async def _research_memory_handler(
    arguments: dict[str, Any], **kwargs: Any,
) -> str:
    workspace_path = kwargs.get("workspace_path")
    if not workspace_path:
        return _err(
            "research_memory requires a workspace; none is available.",
        )

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
            return _err("action=add requires a non-empty url.")
        entry = add_entry(
            entries,
            url=url,
            title=arguments.get("title", "") or "",
            summary=arguments.get("summary", "") or "",
            evidence=arguments.get("evidence") or [],
        )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(serialize_jsonl(entries))
        return _ok({
            "source_id": entry.source_id,
            "url": entry.url,
            "title": entry.title,
            "total": len(entries),
        })

    if action == "retrieve":
        try:
            k = int(arguments.get("k", 5))
        except (TypeError, ValueError):
            return _err("action=retrieve: k must be an integer.")
        hits = retrieve(
            entries,
            query=arguments.get("query", "") or "",
            k=k,
        )
        return _ok({"sources": [asdict(e) for e in hits]})

    if action == "list":
        return _ok({"sources": [asdict(e) for e in entries]})

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


async def _research_outline_handler(
    arguments: dict[str, Any], **kwargs: Any,
) -> str:
    workspace_path = kwargs.get("workspace_path")
    if not workspace_path:
        return _err(
            "research_outline requires a workspace; none is available.",
        )

    path = os.path.join(_research_root(workspace_path), _OUTLINE_FILE)
    action = arguments.get("action", "")

    if action == "set":
        outline = normalize_outline(arguments.get("outline", "") or "")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(outline)
        return _ok({"sections": outline_sections(outline)})

    if action == "get":
        outline = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                outline = fh.read()
        return _ok({
            "outline": outline,
            "sections": outline_sections(outline),
        })

    return _err(f"Unknown action: {action!r}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(registry: ToolRegistry) -> None:
    """Register both deep-research tools on *registry*."""
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
