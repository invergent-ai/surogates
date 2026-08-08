# Copyright (c) 2026, Invergent SA, developed by Flavius Burca
# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the kb_search_pages builtin tool.

Since the KB vector-search work, this tool calls the ops runtime
endpoint (hybrid lexical + vector RRF) via ``platform_client`` instead
of running its own copy of the search SQL against the ops DB. What must
hold and is cheap to break:

1. Routing. A builtin missing from ``TOOL_LOCATIONS`` falls through to
   the ``SANDBOX`` default and surfaces to the LLM as "Unknown tool"
   from a pod with no platform_client -- the handler never runs.
2. Scope. Ops derives the agent's KB attachment set itself and only
   INTERSECTS a caller-supplied ``kb_ids`` -- it can narrow, never
   widen. The one thing ops cannot see is the sender's pinned plan
   entitlement (lives on ``session.config``), so that check must stay
   harness-side and be sent as the narrowing filter.
3. Degradation. A missing platform_client, an ops timeout, or an ops
   auth failure must return a useful tool result, not raise or hang
   the turn.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from surogates.runtime.platform_client import PlatformAuthError
from surogates.tools.builtin import kb_tools
from surogates.tools.registry import ToolRegistry
from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

AGENT_ID = "43196a20-7af0-48c0-a355-3e3a03545f66"
KB_A = "11111111-1111-1111-1111-111111111111"
KB_B = "22222222-2222-2222-2222-222222222222"


def _hit(
    kb_id=KB_A,
    kb_name="Biology",
    path="sources/d1.md",
    title="Photosynthesis",
):
    return {
        "kb_id": kb_id,
        "kb_name": kb_name,
        "path": path,
        "page_type": "summary",
        "title": title,
        "brief": "How plants convert light into sugar.",
        "snippet": "chlorophyll absorbs **light** in the thylakoid",
        "rank": 0.42,
    }


def _platform_client(hits=None, side_effect=None) -> SimpleNamespace:
    search = AsyncMock(
        return_value=[] if hits is None else hits, side_effect=side_effect,
    )
    return SimpleNamespace(search_agent_kb=search)


# ---------------------------------------------------------------------
# 1. Routing regression
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "tool_name", ["kb_list_pages", "kb_read_page", "kb_search_pages"],
)
def test_kb_tools_route_to_harness(tool_name: str) -> None:
    assert TOOL_LOCATIONS.get(tool_name) is ToolLocation.HARNESS, (
        f"{tool_name} must have an explicit HARNESS entry in "
        "TOOL_LOCATIONS; the default SANDBOX fallback routes the call "
        "to a sandbox pod with no platform_client, which answers "
        "'Unknown tool' without ever running the handler."
    )


def test_resolve_location_for_kb_search() -> None:
    """End-to-end through the public resolver, not just the dict."""
    from unittest.mock import MagicMock

    from surogates.tools.router import ToolRouter

    router = ToolRouter(
        registry=MagicMock(),
        sandbox_pool=MagicMock(),
        governance=MagicMock(),
    )
    assert router.resolve_location("kb_search_pages") is (
        ToolLocation.HARNESS
    )


def test_registered_in_the_knowledge_toolset() -> None:
    registry = ToolRegistry()
    kb_tools.register(registry)

    entry = registry.get("kb_search_pages")
    assert entry is not None
    assert entry.toolset == "knowledge"
    assert entry.handler is kb_tools._kb_search_pages_handler
    assert entry.schema.parameters["required"] == ["query"]


def test_description_states_the_retrieval_order() -> None:
    """The description is the only place the model learns that the
    injected tree is partial and that search comes before reading.

    It must not overclaim what the tree is missing: the tree can carry
    a full listing with per-page briefs now, so "truncated ... titles
    only" is false and would mislead the model about what it can trust
    from the tree alone.
    """
    registry = ToolRegistry()
    kb_tools.register(registry)

    description = registry.get("kb_search_pages").schema.description
    assert "FIRST" in description
    assert "kb_read_page" in description
    assert "titles only" not in description
    assert "truncated" not in description


def test_description_and_query_param_describe_hybrid_search() -> None:
    """The tool now runs hybrid (lexical + vector RRF) search, but the
    description said "Full-text search" and the query param told the
    model to "prefer distinctive terms over full sentences" -- guidance
    that was correct for lexical-only search and actively wrong once
    natural-language queries feed the vector half. Both must describe
    the capability (keywords AND natural language both work) without
    promising semantics unconditionally, since ops degrades to
    lexical-only when the embedding provider is unavailable."""
    registry = ToolRegistry()
    kb_tools.register(registry)
    entry = registry.get("kb_search_pages")

    description = entry.schema.description
    assert "semantic" in description or "vector" in description
    assert "natural-language" in description or "natural language" in description
    assert "prefer distinctive terms over full sentences" not in description

    query_description = entry.schema.parameters["properties"]["query"]["description"]
    assert "natural-language" in query_description or "natural language" in query_description
    assert "whole words" not in query_description


# ---------------------------------------------------------------------
# 2. Scope: entitlement narrowing sent to ops
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unrestricted_session_sends_no_kb_ids_filter() -> None:
    """No pinned package -- ops searches every KB it has attached."""
    platform_client = _platform_client()

    await kb_tools._kb_search_pages_handler(
        {"query": "electron transfer"},
        agent_id=AGENT_ID,
        platform_client=platform_client,
    )

    platform_client.search_agent_kb.assert_awaited_once_with(
        AGENT_ID,
        query="electron transfer",
        kb_ids=None,
        limit=kb_tools._SEARCH_LIMIT_DEFAULT,
    )


@pytest.mark.asyncio
async def test_pinned_package_is_sent_as_the_narrowing_kb_ids() -> None:
    platform_client = _platform_client()

    await kb_tools._kb_search_pages_handler(
        {"query": "electron transfer"},
        agent_id=AGENT_ID,
        platform_client=platform_client,
        session_config={"entitlements": {"kb_ids": [KB_B, KB_A]}},
    )

    _, kwargs = platform_client.search_agent_kb.call_args
    assert kwargs["kb_ids"] == sorted([KB_A, KB_B])


@pytest.mark.asyncio
async def test_named_kb_within_the_package_narrows_to_one() -> None:
    platform_client = _platform_client()

    await kb_tools._kb_search_pages_handler(
        {"query": "light", "kb": KB_A},
        agent_id=AGENT_ID,
        platform_client=platform_client,
        session_config={"entitlements": {"kb_ids": [KB_A, KB_B]}},
    )

    platform_client.search_agent_kb.assert_awaited_once_with(
        AGENT_ID, query="light", kb_ids=[KB_A],
        limit=kb_tools._SEARCH_LIMIT_DEFAULT,
    )


@pytest.mark.asyncio
async def test_named_kb_outside_the_package_is_denied_locally() -> None:
    """An injected/mistyped kb outside the sender's plan must not reach
    ops at all -- ops has no way to evaluate the plan entitlement."""
    platform_client = _platform_client()

    out = await kb_tools._kb_search_pages_handler(
        {"query": "light", "kb": KB_B},
        agent_id=AGENT_ID,
        platform_client=platform_client,
        session_config={"entitlements": {"kb_ids": [KB_A]}},
    )

    assert out.startswith("Error:")
    assert KB_B in out
    platform_client.search_agent_kb.assert_not_awaited()


@pytest.mark.asyncio
async def test_named_kb_that_is_not_a_uuid_is_refused_with_a_corrective_message() -> None:
    """A display name (the old resolution path required a DB lookup that
    search no longer has) must not fall through to _kb_plan_denied: on an
    unrestricted session it would reach ops, intersect to [], and render
    as "no matches" (the model blames the query and rephrases pointlessly);
    on a pinned session it would say "not included in your plan", which is
    false when the KB *is* in the plan under its real UUID. Neither is a
    query-shaped problem, so the message must not blame the query."""
    platform_client = _platform_client()

    out = await kb_tools._kb_search_pages_handler(
        {"query": "light", "kb": "Biology"},
        agent_id=AGENT_ID,
        platform_client=platform_client,
        session_config={"entitlements": {"kb_ids": [KB_A]}},
    )

    assert out.startswith("Error:")
    assert "Biology" in out
    assert "UUID" in out
    assert "not included in the current user's plan" not in out
    platform_client.search_agent_kb.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_pinned_allowlist_refuses_rather_than_widen() -> None:
    """A package pinned to zero KBs must not fall through to
    kb_ids=None. An empty list can't cross the wire as a query param at
    all -- httpx drops it from the query string entirely, so ops would
    see the exact same request as an unrestricted search regardless of
    how it branches on the value. The refusal has to happen locally,
    before the call (PlatformClient.search_agent_kb also refuses this
    shape as a second line of defense -- see
    tests/runtime/test_platform_client.py)."""
    platform_client = _platform_client()

    out = await kb_tools._kb_search_pages_handler(
        {"query": "light"},
        agent_id=AGENT_ID,
        platform_client=platform_client,
        session_config={"entitlements": {"kb_ids": []}},
    )

    assert out.startswith("Error:")
    platform_client.search_agent_kb.assert_not_awaited()


@pytest.mark.asyncio
async def test_fails_closed_without_agent_id(monkeypatch) -> None:
    """agent_id comes from the dispatch kwargs, never from
    SUROGATES_AGENT_ID -- that env var is unset in the shared runtime."""
    monkeypatch.setenv("SUROGATES_AGENT_ID", "env-agent-must-be-ignored")
    with pytest.raises(RuntimeError, match="agent_id"):
        await kb_tools._kb_search_pages_handler({"query": "anything"})


@pytest.mark.asyncio
async def test_empty_query_is_rejected() -> None:
    out = await kb_tools._kb_search_pages_handler(
        {"query": "   "}, agent_id=AGENT_ID,
    )
    assert out == "Error: query is required."


# ---------------------------------------------------------------------
# 3. Degradation
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_platform_client_degrades() -> None:
    out = await kb_tools._kb_search_pages_handler(
        {"query": "anything"}, agent_id=AGENT_ID,
    )

    assert out.startswith("Error:")
    assert "kb_list_pages" in out


@pytest.mark.asyncio
async def test_ops_timeout_degrades_instead_of_raising() -> None:
    platform_client = _platform_client(
        side_effect=httpx.ReadTimeout("ops took too long"),
    )

    out = await kb_tools._kb_search_pages_handler(
        {"query": "anything"},
        agent_id=AGENT_ID,
        platform_client=platform_client,
    )

    assert out.startswith("Error:")
    assert "kb_list_pages" in out


@pytest.mark.asyncio
async def test_ops_auth_failure_degrades_instead_of_raising() -> None:
    platform_client = _platform_client(
        side_effect=PlatformAuthError("token rejected"),
    )

    out = await kb_tools._kb_search_pages_handler(
        {"query": "anything"},
        agent_id=AGENT_ID,
        platform_client=platform_client,
    )

    assert out.startswith("Error:")
    assert "kb_list_pages" in out


@pytest.mark.asyncio
async def test_ops_http_status_error_degrades_instead_of_raising() -> None:
    request = httpx.Request("GET", "http://ops/api/agents/agents/x/kb/search")
    response = httpx.Response(500, request=request)
    platform_client = _platform_client(
        side_effect=httpx.HTTPStatusError(
            "boom", request=request, response=response,
        ),
    )

    out = await kb_tools._kb_search_pages_handler(
        {"query": "anything"},
        agent_id=AGENT_ID,
        platform_client=platform_client,
    )

    assert out.startswith("Error:")


# ---------------------------------------------------------------------
# 4. Dispatch threading -- platform_client actually reaches the handler
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_registry_dispatch_threads_platform_client() -> None:
    registry = ToolRegistry()
    kb_tools.register(registry)
    platform_client = _platform_client([_hit()])

    out = await registry.dispatch(
        "kb_search_pages",
        {"query": "light"},
        agent_id=AGENT_ID,
        platform_client=platform_client,
    )

    platform_client.search_agent_kb.assert_awaited_once()
    assert "sources/d1.md" in out


@pytest.mark.asyncio
async def test_execute_single_tool_threads_platform_client() -> None:
    """The real production call path: execute_single_tool ->
    tools.dispatch -> _kb_search_pages_handler. If any hop in
    tool_exec.py drops ``platform_client``, the handler sees None and
    degrades instead of actually searching."""
    from uuid import uuid4

    from surogates.harness.tool_exec import execute_single_tool

    registry = ToolRegistry()
    kb_tools.register(registry)
    platform_client = _platform_client([_hit()])

    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(1, 500))
    store.advance_harness_cursor = AsyncMock()

    session = SimpleNamespace(
        id=uuid4(), config={}, agent_id=AGENT_ID, org_id=uuid4(),
    )

    result = await execute_single_tool(
        {
            "id": "call_1",
            "function": {
                "name": "kb_search_pages",
                "arguments": '{"query": "light"}',
            },
        },
        session=session,
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store,
        tools=registry,
        tenant=SimpleNamespace(org_id=session.org_id),
        platform_client=platform_client,
    )

    platform_client.search_agent_kb.assert_awaited_once()
    assert "sources/d1.md" in result["content"]


# ---------------------------------------------------------------------
# 5. Limit clamping + rendering
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, kb_tools._SEARCH_LIMIT_DEFAULT),
        ("seven", kb_tools._SEARCH_LIMIT_DEFAULT),
        ("3", 3),
        (0, 1),
        (-5, 1),
        (10_000, kb_tools._SEARCH_LIMIT_MAX),
    ],
)
def test_limit_is_clamped(raw, expected) -> None:
    assert kb_tools._clamp_limit(raw) == expected


def test_hits_render_path_title_and_snippet() -> None:
    out = kb_tools._format_search_hits([_hit()], query="light")

    assert "`sources/d1.md`" in out
    assert "Photosynthesis" in out
    assert "chlorophyll absorbs **light**" in out
    assert KB_A in out
    assert "kb_read_page" in out


def test_no_hits_points_at_the_recovery_path() -> None:
    out = kb_tools._format_search_hits([], query="quantum badgers")

    assert "quantum badgers" in out
    assert "kb_list_pages" in out
    # The no-hits message must not blame the query the way the old
    # lexical-only "matches whole words, not substrings" wording did --
    # a natural-language query genuinely not finding anything is not a
    # word-boundary problem under hybrid search.
    assert "whole words" not in out


def test_hits_missing_path_are_dropped_not_rendered_blank() -> None:
    """A renamed/missing ops response field must not hand the model a
    blank path it can't feed back into kb_read_page -- drop the hit
    instead of rendering it uselessly."""
    good = _hit(path="sources/d1.md", title="Photosynthesis")
    broken = {
        "kb_id": KB_B, "kb_name": "Chemistry", "page_type": "summary",
        "title": "No path here", "brief": "", "snippet": "", "rank": 0.1,
    }

    out = kb_tools._format_search_hits([broken, good], query="light")

    assert "sources/d1.md" in out
    assert "No path here" not in out
    assert "1 match(es)" in out


def test_all_hits_missing_path_falls_back_to_no_hits_message() -> None:
    broken = {
        "kb_id": KB_A, "kb_name": "Biology", "page_type": "summary",
        "title": "No path here", "brief": "", "snippet": "", "rank": 0.1,
    }

    out = kb_tools._format_search_hits([broken], query="light")

    assert out.startswith("No knowledge-base pages matched")
    assert "kb_list_pages" in out
