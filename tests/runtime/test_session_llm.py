"""Tests for SessionLLMClients.

Plan 2 / Task 5.  Per-session bundle holding the four AsyncOpenAI
instances + the model strings for main / summary / vision / advisor.
Immutable so the harness can pass the bundle around without worrying
about concurrent mutation; aclose() lifecycle so connection pools
shut down at session end.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_session_llm_clients_aclose_closes_all_present_clients():
    from surogates.harness.session_llm import (
        ResolvedLLM, SessionLLMClients,
    )

    main = AsyncMock()
    summary = AsyncMock()
    bundle = SessionLLMClients(
        main=ResolvedLLM(client=main, model="m"),
        summary=ResolvedLLM(client=summary, model="s"),
        vision=None,
        advisor=None,
    )
    await bundle.aclose()
    main.close.assert_awaited_once()
    summary.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_llm_clients_aclose_tolerates_none_slots():
    """Optional slots (vision / advisor) are commonly None for
    cheaper agents; aclose() must skip them without raising."""
    from surogates.harness.session_llm import (
        ResolvedLLM, SessionLLMClients,
    )

    main = AsyncMock()
    bundle = SessionLLMClients(
        main=ResolvedLLM(client=main, model="m"),
        summary=None,
        vision=None,
        advisor=None,
    )
    await bundle.aclose()
    main.close.assert_awaited_once()


def test_session_llm_clients_is_frozen():
    """Immutable so the harness cannot accidentally swap clients
    mid-turn (which would silently route a continuation through a
    different LLM than the turn started on)."""
    import dataclasses
    from surogates.harness.session_llm import SessionLLMClients

    assert dataclasses.is_dataclass(SessionLLMClients)
    fields = dataclasses.fields(SessionLLMClients)
    assert SessionLLMClients.__dataclass_params__.frozen is True
    assert {f.name for f in fields} == {
        "main", "summary", "vision", "advisor",
    }
