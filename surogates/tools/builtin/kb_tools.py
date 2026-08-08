"""Knowledge-base navigation tools: kb_search_pages, kb_list_pages,
kb_read_page.

These tools let the agent inspect the curated wiki of a knowledge base
and read individual pages on demand. Together they implement "agent
navigates the KB itself" -- there is no sub-LLM curator. The agent's
own reasoning chain decides which pages are worth reading, which keeps
cost low and makes every step visible in the trace.

Architecture:

    +----------------+   GET /agents/agents/{id}/kb/search  +--------+
    | kb_search_pages|-------------------------------------->| ops API|
    +----------------+   (hybrid lexical + vector RRF)       +--------+

    +--------------+       SELECT path, page_type, title  +----------+
    | kb_list_pages|------------------------------------>|  ops DB  |
    +--------------+                                      +----------+

    +--------------+       SELECT hub_ref + path -------->| ops DB  |
    | kb_read_page |
    |              |       GET /repositories/.../objects-->|  Hub   |
    +--------------+

kb_list_pages/kb_read_page read the read-only ops DB connection
directly (see ``surogates/db/ops_engine.py``). kb_search_pages instead
calls the runtime-scoped ``/agents/agents/{agent_id}/kb/search`` route
over ``platform_client`` (a bearer-token-authed service call, not the
JWT-authed Studio `/wiki/search` route) -- the fused lexical+vector
query and its tsvector/embedding-model configuration live once on the
ops side, so this tool never has its own copy to drift.

Both kb_list_pages/kb_read_page accept ``kb_id`` directly. We could
rewrite to take a human-friendly name, but production agents are wired
by the platform (not typed by humans), so UUIDs are the natural
primary key here. kb_search_pages searches every attached KB by
default since the agent usually knows what it's looking for, not
which KB holds it.

Validation: kb_list_pages/kb_read_page confirm ``(agent_id, kb_id)``
is present in ``agent_knowledge_bases`` before touching the DB --
defense-in-depth against a system-prompt injection that hands the
agent a kb_id outside its allowed set, on top of the platform's own
available_tools filtering. kb_search_pages has no DB handle to make
that check itself; ops derives the attachment set from the same table
server-side and only lets a caller-supplied ``kb_ids`` narrow it (see
``_kb_search_pages_handler``). The one check that must stay
harness-side either way is the sender's pinned plan entitlement
(``kb_allowed``/``session_config``), which ops has no way to see.
"""

from __future__ import annotations

import logging
from collections import Counter
from itertools import zip_longest
from typing import Any

import httpx
import sqlalchemy as sa

from surogates.db.ops_engine import ensure_ops_session_factory
from surogates.runtime.entitlements import dimension_allowlist, kb_allowed
from surogates.db.ops_models import (
    OpsKBWikiPage,
    OpsKnowledgeBase,
    agent_knowledge_bases,
)
from surogates.runtime.platform_client import PlatformAuthError
from surogates.storage.kb_hub import KBHubError, fetch_wiki_object
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)


def _kb_plan_denied(kb_id: str, kwargs: dict) -> str | None:
    """The refusal message when the sender's package excludes *kb_id*."""
    if kb_allowed(kwargs.get("session_config"), kb_id):
        return None
    return (
        f"Error: knowledge base {kb_id!r} is not included in the "
        f"current user's plan."
    )


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


def _agent_id_from_kwargs(kwargs: dict[str, Any]) -> str:
    """Resolve the per-session agent_id from the tool-dispatch context.

    The harness passes ``agent_id=session.agent_id`` into every tool
    call (see ``surogates.harness.tool_exec``). We read it from there
    rather than from a process env var because the shared runtime serves
    many agents from one worker and never sets ``SUROGATES_AGENT_ID`` --
    reading that env var broke KB tools for every shared-runtime session.
    A missing value is an internal wiring bug (the dispatch always
    supplies it), so we fail loudly rather than degrade.
    """
    agent_id = str(kwargs.get("agent_id") or "").strip()
    if not agent_id:
        raise RuntimeError(
            "agent_id missing from tool context; KB tools require it to "
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


# Section order in the rendered tree, shared with _select_tree_pages so
# selection and rendering agree on what comes first.
_TREE_TYPE_ORDER = {"index": 0, "summary": 1, "concept": 2, "source": 3}

# Page types that never belong in the injected ToC. ``source`` rows are
# verbatim dumps of the ingested documents -- hundreds of them, each
# huge, and every one of them is still reachable with kb_read_page. The
# tree exists so the LLM can pick a page to read, and it never wants to
# start from a raw source.
_TREE_EXCLUDED_TYPES = frozenset({"source"})

# Default ToC budget. Lives here next to the selection policy rather
# than at the call site so both the tree and its "showing N of M"
# announcement are computed from the same number.
_TREE_PAGE_BUDGET = 200


def _select_tree_pages(
    pages: list[OpsKBWikiPage], budget: int = _TREE_PAGE_BUDGET,
) -> list[OpsKBWikiPage]:
    """Pick at most *budget* pages for the injected ToC, fairly by type.

    The bug this replaces: the caller sliced a path-ordered list before
    _format_pages_tree grouped it by page_type. On a real KB (625
    concept + 3946 summary + 1 index) ``concepts/...`` sorts first, so
    the agent saw 200 concept pages, zero summaries and no index -- it
    could not even see that summaries existed.

    Policy:

    - ``index`` pages are taken whole and off the top. There are at
      most a handful and they are the entry point to everything else.
    - ``source`` pages are dropped (see _TREE_EXCLUDED_TYPES).
    - The rest of the budget is dealt round-robin across the remaining
      types, one page at a time in path order. A type smaller than its
      equal share is exhausted and its unused quota flows to the larger
      types automatically, so nothing is starved and nothing is wasted.
    - Within a type the first N pages in ``path`` order are kept, so the
      tree is byte-identical between wakes.
    """
    buckets: dict[str, list[OpsKBWikiPage]] = {}
    for page in sorted(pages, key=lambda p: p.path):
        if page.page_type in _TREE_EXCLUDED_TYPES:
            continue
        buckets.setdefault(page.page_type, []).append(page)

    # Slicing index too is theatre (a KB with >200 index pages does not
    # exist), but it keeps the "never returns more than budget" promise
    # unconditional.
    index_pages = buckets.pop("index", [])[:budget]
    remaining = budget - len(index_pages)

    types = sorted(buckets, key=lambda t: (_TREE_TYPE_ORDER.get(t, 99), t))
    fair = [
        page
        for row in zip_longest(*(buckets[t] for t in types))
        for page in row
        if page is not None
    ][:remaining]

    # The round-robin always draws from the front of each bucket, so a
    # per-type count is enough to rebuild the selection in tree order.
    counts = Counter(page.page_type for page in fair)
    return index_pages + [
        page for t in types for page in buckets[t][:counts[t]]
    ]


# Per-page brief budget in the rendered tree. The compile pipeline is
# instructed to keep briefs under 100 chars, but models overrun it and
# the column allows 512 -- at the 200-page tree cap that is ~100KB of
# system prompt per attachment, paid on every turn. 120 chars keeps a
# full sentence and costs ~25KB at the cap.
_BRIEF_MAX_CHARS = 120


def _brief_snippet(brief: str | None) -> str:
    """Flatten *brief* to one line, capped at ``_BRIEF_MAX_CHARS``.

    Collapsing whitespace is not cosmetic: briefs are model-written, and
    one containing a newline would otherwise inject bogus entries into
    the markdown tree the LLM reads as real pages.

    Truncation cuts at the last word boundary and marks the cut with
    ``...`` -- a hard mid-word cut yields fragments the LLM quotes back
    as whole words, and an unmarked cut reads as a complete summary so
    the agent never opens the page. A brief with no boundary to cut on
    (one long URL, an unspaced CJK/Greek run) falls back to a hard cut
    rather than degrading to a bare ellipsis.
    """
    flat = " ".join((brief or "").split())
    if len(flat) <= _BRIEF_MAX_CHARS:
        return flat
    cut = flat[:_BRIEF_MAX_CHARS - 3]
    head, _, _ = cut.rpartition(" ")
    return (head or cut) + "..."


def _format_pages_tree(pages: list[OpsKBWikiPage]) -> str:
    """Render the wiki page list as a markdown tree the LLM can read.

    Groups by ``page_type`` (index, summary, concept, source) so the LLM
    can scan the structure top-down: index first (the entry point), then
    summaries (file-level overviews), then concepts (cross-cuts
    extracted by the curator), then raw sources, then everything else.

    Each entry carries its ``brief`` when the compile pipeline wrote one
    -- that one line is what lets the agent pick a page without reading
    it. Pages with no brief render exactly as before, no dangling
    separator.
    """
    if not pages:
        return "(empty -- no wiki pages have been compiled yet)"

    pages_sorted = sorted(
        pages,
        key=lambda p: (_TREE_TYPE_ORDER.get(p.page_type, 99), p.path),
    )

    lines = []
    current_type: str | None = None
    for p in pages_sorted:
        if p.page_type != current_type:
            current_type = p.page_type
            lines.append(f"\n## {current_type}")
        size_kb = max(1, p.size_bytes // 1024)
        brief = _brief_snippet(p.brief)
        suffix = f" -- {brief}" if brief else ""
        lines.append(
            f"- `{p.path}` -- {p.title} ({size_kb} KB){suffix}"
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

    agent_id = _agent_id_from_kwargs(kwargs)
    denied = _kb_plan_denied(kb_id, kwargs)
    if denied is not None:
        return denied

    factory = ensure_ops_session_factory()
    if factory is None:
        return (
            "Error: KB tools are unavailable -- the worker was started "
            "without an ops database connection."
        )

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

    agent_id = _agent_id_from_kwargs(kwargs)
    denied = _kb_plan_denied(kb_id, kwargs)
    if denied is not None:
        return denied

    factory = ensure_ops_session_factory()
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


# Ranked-search caps. The result is a triage list, not a corpus: 10
# hits is enough for the LLM to pick a page, 25 is the ceiling so a
# vague query cannot flood the context window.
_SEARCH_LIMIT_DEFAULT = 10
_SEARCH_LIMIT_MAX = 25


def _clamp_limit(raw: Any) -> int:
    """Coerce the model-supplied ``limit`` into the allowed range.

    LLMs pass strings, floats and nonsense here; a bad value is not
    worth an error turn, so we fall back to the default.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _SEARCH_LIMIT_DEFAULT
    return max(1, min(value, _SEARCH_LIMIT_MAX))


def _format_search_hits(hits: list[dict[str, Any]], *, query: str) -> str:
    """Render ranked hits (raw ``WikiSearchHit`` dicts from ops) as
    lines the LLM can act on directly.

    Every line carries the kb_id and path, i.e. the exact arguments
    ``kb_read_page`` needs, so the follow-up call requires no guessing.
    ``kb_name`` comes from the hit itself -- the multi-KB search route
    stamps it per hit -- so this needs no separate id->name lookup.
    """
    if not hits:
        return (
            f"No knowledge-base pages matched {query!r}. Full-text"
            f" search matches whole words, not substrings -- retry with"
            f" fewer or different keywords, or browse with"
            f" `kb_list_pages`."
        )

    lines = [f"{len(hits)} match(es) for {query!r}, best first:", ""]
    for index, hit in enumerate(hits, start=1):
        kb_id = hit.get("kb_id", "")
        kb_name = hit.get("kb_name") or kb_id
        lines.append(
            f"{index}. `{hit.get('path', '')}` -- {hit.get('title', '')} "
            f"[{hit.get('page_type', '')} | kb {kb_name} `{kb_id}`]"
        )
        for text in (hit.get("brief"), hit.get("snippet")):
            flat = " ".join((text or "").split())
            if flat:
                lines.append(f"   {flat}")
    lines.append("")
    lines.append(
        "Snippets are excerpts. Read the promising hits in full with "
        "`kb_read_page(kb_id=..., path=...)` before answering."
    )
    return "\n".join(lines)


async def _kb_search_pages_handler(
    arguments: dict[str, Any], **kwargs: Any,
) -> str:
    """Handler for ``kb_search_pages``.

    One query across every KB the session may see, fanned out server-
    side by the ops runtime endpoint (hybrid lexical + vector RRF).
    Unlike the other two tools this one takes no mandatory kb_id -- the
    agent usually knows what it is looking for, not which of its KBs
    holds it.

    Ops derives the searchable set from ``agent_knowledge_bases`` and
    treats ``kb_ids`` as a narrowing filter only (never widening) --
    see ``PlatformClient.search_agent_kb``. The one thing ops cannot
    see is the sender's pinned plan entitlement, since that lives on
    ``session.config`` and never reaches ops: this handler resolves it
    locally through the same ``_kb_plan_denied``/``kb_allowed`` gate
    ``kb_list_pages``/``kb_read_page`` use, and sends the result as
    ``kb_ids`` so the two checks intersect.
    """
    query = (arguments.get("query") or "").strip()
    if not query:
        return "Error: query is required."
    named_kb = (arguments.get("kb") or "").strip()
    limit = _clamp_limit(arguments.get("limit"))

    agent_id = _agent_id_from_kwargs(kwargs)

    platform_client = kwargs.get("platform_client")
    if platform_client is None:
        return (
            "Error: knowledge-base search is unavailable right now -- "
            "no connection to the management plane. Use `kb_list_pages` "
            "to browse instead."
        )

    if named_kb:
        denied = _kb_plan_denied(named_kb, kwargs)
        if denied is not None:
            return denied
        kb_ids: list[str] | None = [named_kb]
    else:
        allowed = dimension_allowlist(kwargs.get("session_config"), "kb_ids")
        # ``None`` means unrestricted -- pass it straight through so ops
        # searches every KB it has attached, not zero. A pinned-but-empty
        # allowlist must never reach ops as ``kb_ids=[]``: both this
        # client and the ops service treat an empty list as falsy (no
        # filter), which would silently widen a zero-KB package back to
        # every attached KB. Refuse locally instead.
        if allowed is not None and not allowed:
            return "Error: no knowledge bases are available to this session."
        kb_ids = sorted(allowed) if allowed is not None else None

    try:
        hits = await platform_client.search_agent_kb(
            agent_id, query=query, kb_ids=kb_ids, limit=limit,
        )
    except (PlatformAuthError, httpx.HTTPError):
        # A failed search must not kill the turn: the LLM can fall back
        # to kb_list_pages + kb_read_page.
        logger.warning(
            "KB search failed for agent %s", agent_id, exc_info=True,
        )
        return (
            "Error: knowledge-base search is unavailable right now. "
            "Use `kb_list_pages` to browse instead."
        )

    return _format_search_hits(hits, query=query)


_KB_SEARCH_PAGES_PARAMS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "What you are looking for, as keywords or a short "
                "phrase in the language of the source material. "
                "Matching is on whole words, so prefer distinctive "
                "terms over full sentences."
            ),
        },
        "kb": {
            "type": "string",
            "description": (
                "Optional. Restrict the search to one knowledge base, "
                "by its UUID exactly as listed in the system prompt's "
                "Available Knowledge Bases section. Omit to search "
                "every knowledge base available to you -- that is "
                "usually what you want."
            ),
        },
        "limit": {
            "type": "integer",
            "description": (
                f"Optional. Maximum hits to return "
                f"(default {_SEARCH_LIMIT_DEFAULT}, "
                f"max {_SEARCH_LIMIT_MAX})."
            ),
            "minimum": 1,
            "maximum": _SEARCH_LIMIT_MAX,
            "default": _SEARCH_LIMIT_DEFAULT,
        },
    },
    "required": ["query"],
}


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
                "Browse a knowledge base's structure, grouped by page "
                "type (index, summary, concept, source). Returns a "
                "markdown tree showing each page's path, title, "
                "one-line brief and size (large knowledge bases may be "
                "cut off). To find the answer to a specific question, "
                "use kb_search_pages instead."
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
    registry.register(
        name="kb_search_pages",
        schema=ToolSchema(
            name="kb_search_pages",
            description=(
                "Full-text search across your knowledge bases. "
                "Returns pages ranked by relevance, each with its "
                "kb_id, path, title and the matching snippet.\n\n"
                "USE THIS FIRST for any question a knowledge base "
                "could answer. The page tree in your system prompt may "
                "be partial and each entry carries only a one-line "
                "brief -- neither tells you which page actually holds "
                "the answer, and a page missing from the tree may "
                "still exist.\n\n"
                "Order of retrieval: (1) kb_search_pages to find "
                "candidate pages, (2) kb_read_page(kb_id, path) on the "
                "top hits to read them in full, (3) only then answer. "
                "Never answer from the tree or from a snippet alone, "
                "and never guess a path -- use one returned here. "
                "kb_list_pages is for browsing a KB's full structure, "
                "not for finding an answer."
            ),
            parameters=_KB_SEARCH_PAGES_PARAMS,
        ),
        handler=_kb_search_pages_handler,
        toolset="knowledge",
        max_result_size=20_000,
    )
