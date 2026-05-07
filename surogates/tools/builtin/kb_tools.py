"""Knowledge-base navigation tools: kb_list_pages + kb_read_page.

These two tools let the agent inspect the curated wiki of a knowledge
base and read individual pages on demand. Together they implement
"agent navigates the KB itself" -- there is no sub-LLM curator. The
agent's own reasoning chain decides which pages are worth reading,
which keeps cost low and makes every step visible in the trace.

Architecture:

    +--------------+       SELECT path, page_type, title  +----------+
    | kb_list_pages|------------------------------------>|  ops DB  |
    +--------------+                                      +----------+

    +--------------+       SELECT hub_ref + path -------->| ops DB  |
    | kb_read_page |
    |              |       GET /repositories/.../objects-->|  Hub   |
    +--------------+

Both tools accept ``kb_id`` directly. We could rewrite to take a
human-friendly name, but production agents are wired by the platform
(not typed by humans), so UUIDs are the natural primary key here.

Validation: every call confirms ``(agent_id, kb_id)`` is present in
``agent_knowledge_bases``. This is defense-in-depth -- the platform
already filters available_tools per agent, but a tool handler that
trusted only the registry would be vulnerable to a system-prompt
injection that handed the agent a kb_id outside its allowed set.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import sqlalchemy as sa

from surogates.db.ops_engine import get_ops_session_factory
from surogates.db.ops_models import (
    OpsKBWikiPage,
    OpsKnowledgeBase,
    agent_knowledge_bases,
)
from surogates.storage.kb_hub import KBHubError, fetch_wiki_object
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)


# Wiki content sits in Hub under wiki/<path>. The DB stores the
# logical path (``sources/d1.md``) so the same path is reusable as a
# stable identifier; the prefix is a Hub-side storage layout choice.
_HUB_WIKI_PREFIX = "wiki/"

# Branch used for wiki content in the v1 KB design. Pinned here as a
# constant rather than a config knob: the compile pipeline writes only
# to ``main``, so reading any other branch would always 404.
_HUB_BRANCH = "main"

# Cap returned content per call. Wiki pages are typically small (a few
# KB), but a malformed compile could produce a huge index page. The
# cap protects the LLM context window from accidental flooding.
_MAX_PAGE_BYTES = 200_000


def _agent_id_from_env() -> str:
    """Resolve the worker's agent_id, raising if it isn't set.

    Tools never run outside a worker process, so the env var is
    guaranteed at startup -- but we re-read it per call to keep the
    handler self-contained and easy to test.
    """
    agent_id = os.getenv("SUROGATES_AGENT_ID", "")
    if not agent_id:
        raise RuntimeError(
            "SUROGATES_AGENT_ID is not set; KB tools require it to "
            "validate KB attachment."
        )
    return agent_id


async def _is_kb_attached(
    session: Any, *, agent_id: str, kb_id: str,
) -> bool:
    """Check the M2M table for (agent_id, kb_id).

    Returns True only if the row exists. We don't cache here -- the
    overhead of a primary-key lookup is sub-millisecond and the worst
    case (miss after detach) is a stale-tool error message that the
    LLM can recover from on the next iteration.
    """
    result = await session.execute(
        sa.select(agent_knowledge_bases.c.kb_id).where(
            agent_knowledge_bases.c.agent_id == agent_id,
            agent_knowledge_bases.c.kb_id == kb_id,
        )
    )
    return result.first() is not None


def _format_pages_tree(pages: list[OpsKBWikiPage]) -> str:
    """Render the wiki page list as a markdown tree the LLM can read.

    Groups by ``page_type`` (index, summary, concept, ...) so the LLM
    can scan the structure top-down: index first (the entry point),
    then summaries (file-level overviews), then concepts (cross-cuts
    extracted by the curator), then everything else.
    """
    if not pages:
        return "(empty -- no wiki pages have been compiled yet)"

    type_order = {"index": 0, "summary": 1, "concept": 2}
    pages_sorted = sorted(
        pages,
        key=lambda p: (type_order.get(p.page_type, 99), p.path),
    )

    lines = []
    current_type: str | None = None
    for p in pages_sorted:
        if p.page_type != current_type:
            current_type = p.page_type
            lines.append(f"\n## {current_type}")
        size_kb = max(1, p.size_bytes // 1024)
        lines.append(
            f"- `{p.path}` -- {p.title} ({size_kb} KB)"
        )
    return "\n".join(lines).strip()


async def _kb_list_pages_handler(
    arguments: dict[str, Any], **kwargs: Any,
) -> str:
    """Handler for ``kb_list_pages``.

    Returns a markdown-formatted tree of every wiki page in the KB,
    grouped by page type. The LLM uses this to decide which page to
    read next.
    """
    kb_id = (arguments.get("kb_id") or "").strip()
    if not kb_id:
        return "Error: kb_id is required."

    factory = get_ops_session_factory()
    if factory is None:
        return (
            "Error: KB tools are unavailable -- the worker was started "
            "without an ops database connection."
        )

    agent_id = _agent_id_from_env()

    async with factory() as session:
        if not await _is_kb_attached(
            session, agent_id=agent_id, kb_id=kb_id,
        ):
            return (
                f"Error: knowledge base {kb_id!r} is not attached to "
                f"this agent."
            )

        kb_row = (await session.execute(
            sa.select(OpsKnowledgeBase).where(
                OpsKnowledgeBase.id == kb_id,
            )
        )).scalar_one_or_none()
        if kb_row is None:
            return f"Error: knowledge base {kb_id!r} no longer exists."

        pages = list((await session.execute(
            sa.select(OpsKBWikiPage)
            .where(OpsKBWikiPage.kb_id == kb_id)
            .order_by(OpsKBWikiPage.path.asc())
        )).scalars().all())

    tree = _format_pages_tree(pages)
    return (
        f"# Knowledge base: {kb_row.display_name}\n"
        f"**ID:** `{kb_id}` | **Status:** {kb_row.status} | "
        f"**Pages:** {len(pages)}\n\n"
        f"{kb_row.description or '(no description)'}\n"
        f"{tree}\n\n"
        f"Use `kb_read_page` with the kb_id and one of the paths "
        f"above to read its content."
    )


async def _kb_read_page_handler(
    arguments: dict[str, Any], **kwargs: Any,
) -> str:
    """Handler for ``kb_read_page``.

    Returns the markdown content of a single wiki page. The path must
    match a row in ``kb_wiki_pages`` for this KB; we don't accept
    arbitrary Hub paths.
    """
    kb_id = (arguments.get("kb_id") or "").strip()
    path = (arguments.get("path") or "").strip()
    if not kb_id or not path:
        return "Error: both kb_id and path are required."

    factory = get_ops_session_factory()
    if factory is None:
        return (
            "Error: KB tools are unavailable -- the worker was started "
            "without an ops database connection."
        )

    # Hub creds come from settings injected at worker startup.
    from surogates.config import Settings
    cfg = Settings()
    if not cfg.kb_hub.endpoint_url:
        return (
            "Error: KB Hub endpoint not configured on this worker."
        )

    agent_id = _agent_id_from_env()

    async with factory() as session:
        if not await _is_kb_attached(
            session, agent_id=agent_id, kb_id=kb_id,
        ):
            return (
                f"Error: knowledge base {kb_id!r} is not attached to "
                f"this agent."
            )

        kb_row = (await session.execute(
            sa.select(OpsKnowledgeBase).where(
                OpsKnowledgeBase.id == kb_id,
            )
        )).scalar_one_or_none()
        if kb_row is None or not kb_row.hub_ref:
            return f"Error: knowledge base {kb_id!r} has no Hub repo."

        page = (await session.execute(
            sa.select(OpsKBWikiPage).where(
                OpsKBWikiPage.kb_id == kb_id,
                OpsKBWikiPage.path == path,
            )
        )).scalar_one_or_none()
        if page is None:
            return (
                f"Error: page {path!r} not found in this KB. Use "
                f"`kb_list_pages` to see available paths."
            )

    hub_path = _HUB_WIKI_PREFIX + path.lstrip("/")
    try:
        raw = await fetch_wiki_object(
            endpoint_url=cfg.kb_hub.endpoint_url,
            access_key_id=cfg.kb_hub.access_key_id,
            secret_access_key=cfg.kb_hub.secret_access_key,
            repo_id=kb_row.hub_ref,
            branch=_HUB_BRANCH,
            path=hub_path,
        )
    except KBHubError as exc:
        return f"Error reading wiki page: {exc}"

    if len(raw) > _MAX_PAGE_BYTES:
        truncated = raw[:_MAX_PAGE_BYTES].decode("utf-8", errors="replace")
        return (
            f"# {page.title}\n\n"
            f"_Note: page truncated to {_MAX_PAGE_BYTES} bytes (full "
            f"size {page.size_bytes} bytes)._\n\n"
            f"{truncated}"
        )

    content = raw.decode("utf-8", errors="replace")
    return f"# {page.title}\n\n{content}"


_KB_LIST_PAGES_PARAMS = {
    "type": "object",
    "properties": {
        "kb_id": {
            "type": "string",
            "description": (
                "The knowledge base ID to list pages from. Pass the "
                "UUID exactly as listed in the system prompt's "
                "Available Knowledge Bases section."
            ),
        },
    },
    "required": ["kb_id"],
}


_KB_READ_PAGE_PARAMS = {
    "type": "object",
    "properties": {
        "kb_id": {
            "type": "string",
            "description": (
                "The knowledge base ID. Same UUID you used with "
                "kb_list_pages."
            ),
        },
        "path": {
            "type": "string",
            "description": (
                "The page path as returned by kb_list_pages "
                "(e.g. 'index.md', 'sources/d1.md', "
                "'concepts/photosynthesis.md'). Wiki paths are case- "
                "and slash-sensitive."
            ),
        },
    },
    "required": ["kb_id", "path"],
}


def register(registry: ToolRegistry) -> None:
    """Register kb_list_pages and kb_read_page on *registry*.

    Always registered when the worker has KB connectivity (ops DB +
    Hub creds). The per-session ``available_tools`` filter decides
    whether the LLM actually sees them, so we don't gate registration
    on attached-KB count -- the worker may pick up new attachments
    without restarting.
    """
    registry.register(
        name="kb_list_pages",
        schema=ToolSchema(
            name="kb_list_pages",
            description=(
                "List all pages in a knowledge base, grouped by page "
                "type (index, summary, concept). Returns a markdown "
                "tree showing each page's path, title, and size. Use "
                "this first to see what's available, then read "
                "specific pages with kb_read_page."
            ),
            parameters=_KB_LIST_PAGES_PARAMS,
        ),
        handler=_kb_list_pages_handler,
        toolset="knowledge",
        max_result_size=50_000,
    )
    registry.register(
        name="kb_read_page",
        schema=ToolSchema(
            name="kb_read_page",
            description=(
                "Read the markdown content of a single wiki page from "
                "a knowledge base. Returns the page's title and full "
                "content. Pages over 200KB are truncated."
            ),
            parameters=_KB_READ_PAGE_PARAMS,
        ),
        handler=_kb_read_page_handler,
        toolset="knowledge",
        max_result_size=200_000,
    )
