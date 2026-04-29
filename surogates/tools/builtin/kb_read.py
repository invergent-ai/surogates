"""kb_read HARNESS tool — fetches a wiki/ or raw/ entry from a KB.

Step 3 (this revision) verifies registration and tenant scoping only;
the byte-content fetch from object storage is wired in step 4 alongside
``markdown_dir`` ingestion. Until then a successful resolution returns
the entry's kind + path with a placeholder content note so the LLM can
confirm visibility without breaking on a missing storage backend.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from surogates.storage.kb_store import KbStore
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)


KB_READ_SCHEMA = ToolSchema(
    name="kb_read",
    description=(
        "Read a wiki or raw entry from a knowledge base by path. "
        "Use this after ``kb_search`` returns a ``document_path`` to "
        "load the full entry. Path may be passed either as "
        "``<kb_name>/<rest>`` or with ``kb`` set explicitly. Returns "
        "the entry's kind ('wiki' or 'raw') and content."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Entry path. With ``kb`` set: relative to the KB "
                    "root (e.g. ``wiki/summaries/foo.md``). Without "
                    "``kb``: include the KB name as the first "
                    "segment (e.g. "
                    "``invergent-docs/wiki/summaries/foo.md``)."
                ),
            },
            "kb": {
                "type": "string",
                "description": (
                    "Optional: KB name. Required when ``path`` does "
                    "not include it as the first segment."
                ),
            },
        },
        "required": ["path"],
    },
)


def register(registry: ToolRegistry) -> None:
    """Register the ``kb_read`` tool."""
    registry.register(
        name="kb_read",
        schema=KB_READ_SCHEMA,
        handler=_kb_read_handler,
        toolset="kb",
    )


async def _kb_read_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Handle a ``kb_read`` tool call. See module docstring."""
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

    path = (arguments.get("path") or "").strip()
    if not path:
        return json.dumps({
            "success": False,
            "error": "path is required",
        })
    kb_name = arguments.get("kb")

    store = KbStore(session_factory)
    try:
        result = await store.read_entry(
            org_id=org_id,
            path=path,
            kb_name=kb_name,
        )
    except Exception as exc:
        logger.exception("kb_read failed")
        return json.dumps({
            "success": False,
            "error": f"kb_read error: {exc}",
        })

    if result is None:
        return json.dumps({
            "success": False,
            "error": (
                f"Entry not found (kb={kb_name!r}, path={path!r}). "
                "Either the kb name is unknown to your org, the path "
                "is not registered in the wiki / raw layer, or the "
                "kb belongs to a different tenant."
            ),
        })

    # Step-3 stub: registration confirmed; full byte fetch from object
    # storage is wired in step 4 along with ingestion. Keeping the
    # field name ``content`` so the agent's call shape stays stable.
    return json.dumps({
        "success": True,
        "kb_name": result["kb_name"],
        "path": result["path"],
        "kind": result["kind"],
        "content": (
            "[byte content fetch lands in MVP step 4 alongside "
            "ingestion; this step verifies KB registration and "
            "tenant scoping]"
        ),
    })
