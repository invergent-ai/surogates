"""kb_search HARNESS tool — hybrid retrieval over the agent's KBs.

Mirrors the shape of ``session_search``: declared schema, a thin
``register`` function, and a kwargs-driven async handler. All KB tools
live in HARNESS so the per-session sandbox never sees KB bytes —
agents that have access to GB-scale KBs do not pay any per-session
copy cost.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any
from uuid import UUID

from surogates.storage.kb_store import KbStore
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)


KB_SEARCH_SCHEMA = ToolSchema(
    name="kb_search",
    description=(
        "Search the agent's authorized knowledge bases for grounded "
        "answers. Returns chunks of relevant wiki entries with their "
        "source paths so you can cite where information came from. "
        "Use this whenever the user asks about product behavior, "
        "configuration, APIs, or concepts that should come from "
        "official documentation. Always cite ``document_path`` in "
        "your answer."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "3-15 word natural-language query.",
            },
            "kb": {
                "type": "string",
                "description": (
                    "Optional: filter to a single KB by name. Omit "
                    "to search every KB the agent can read (the "
                    "org's own KBs plus all platform-shared KBs)."
                ),
            },
            "top_k": {
                "type": "integer",
                "description": "Max results per KB (default 5).",
                "default": 5,
            },
        },
        "required": ["query"],
    },
)


def register(registry: ToolRegistry) -> None:
    """Register the ``kb_search`` tool."""
    registry.register(
        name="kb_search",
        schema=KB_SEARCH_SCHEMA,
        handler=_kb_search_handler,
        toolset="kb",
    )


async def _kb_search_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Handle a ``kb_search`` tool call.

    Pulls ``session_factory`` and ``tenant`` (with ``org_id``) from the
    runtime kwargs (the harness already injects both — see
    ``harness/tool_exec.py:709``). Returns a JSON string the LLM can
    read directly.
    """
    session_factory = kwargs.get("session_factory")
    if session_factory is None:
        return json.dumps({
            "success": False,
            "error": "session_factory not available in this context",
        })

    tenant = kwargs.get("tenant", {})
    org_id = (
        tenant.get("org_id")
        if isinstance(tenant, dict)
        else getattr(tenant, "org_id", None)
    )
    if isinstance(org_id, str):
        org_id = UUID(org_id)
    if org_id is None:
        return json.dumps({
            "success": False,
            "error": "tenant context missing org_id",
        })

    query = (arguments.get("query") or "").strip()
    if not query:
        return json.dumps({"success": True, "results": []})

    kb_name = arguments.get("kb")
    top_k_raw = arguments.get("top_k", 5)
    try:
        top_k = max(1, int(top_k_raw))
    except (TypeError, ValueError):
        top_k = 5

    embedder = kwargs.get("embedder")
    # agent_id is the same string identifier the harness uses to
    # route the session (``session.agent_id``); the grant filter
    # compares it directly. Empty string / missing → no filter (legacy
    # admin / direct-test path).
    agent_str = kwargs.get("agent_id") or None
    if not isinstance(agent_str, str) or not agent_str:
        agent_str = None

    store = KbStore(session_factory, embedder=embedder)
    try:
        hits = await store.search(
            org_id=org_id,
            query=query,
            kb_name=kb_name,
            top_k=top_k,
            agent_id=agent_str,
        )
    except Exception as exc:
        logger.exception("kb_search failed")
        return json.dumps({
            "success": False,
            "error": f"kb_search error: {exc}",
        })

    return json.dumps({
        "success": True,
        "results": [asdict(h) for h in hits],
    })
