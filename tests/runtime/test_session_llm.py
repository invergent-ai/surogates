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


@pytest.mark.asyncio
async def test_build_session_llm_clients_constructs_main_only():
    """Minimum viable: ctx has only llm_main; the factory returns a
    bundle with summary/vision/advisor all None and the main client
    wired with the vault-resolved key."""
    from surogates.harness.session_llm import build_session_llm_clients
    from surogates.runtime import AgentRuntimeContext, LLMEndpoint

    ctx = AgentRuntimeContext(
        agent_id="a-1", org_id="o-1", project_id="p-1",
        enabled=True, config_version=1, storage_key_prefix="p/a",
        llm_main=LLMEndpoint(
            model="gpt-4", base_url="https://api.example.com",
            api_key_ref="vault://main-key",
        ),
    )
    vault = AsyncMock()
    vault.resolve_ref = AsyncMock(return_value="sk-resolved")

    bundle = await build_session_llm_clients(ctx, vault=vault)
    try:
        assert bundle.main.model == "gpt-4"
        assert bundle.summary is None
        assert bundle.vision is None
        assert bundle.advisor is None
        vault.resolve_ref.assert_awaited_once_with(
            "vault://main-key", org_id="o-1", user_id=None,
        )
    finally:
        await bundle.aclose()


@pytest.mark.asyncio
async def test_build_session_llm_clients_constructs_all_four_slots():
    from surogates.harness.session_llm import build_session_llm_clients
    from surogates.runtime import AgentRuntimeContext, LLMEndpoint

    def _ep(name):
        return LLMEndpoint(
            model=name, base_url="u", api_key_ref=f"vault://{name}",
        )

    ctx = AgentRuntimeContext(
        agent_id="a-1", org_id="o-1", project_id="p-1",
        enabled=True, config_version=1, storage_key_prefix="p/a",
        llm_main=_ep("main"),
        llm_summary=_ep("summary"),
        llm_vision=_ep("vision"),
        llm_advisor=_ep("advisor"),
    )
    vault = AsyncMock()
    vault.resolve_ref = AsyncMock(side_effect=lambda ref, **_: f"sk-{ref[8:]}")

    bundle = await build_session_llm_clients(ctx, vault=vault)
    try:
        assert bundle.main.model == "main"
        assert bundle.summary.model == "summary"
        assert bundle.vision.model == "vision"
        assert bundle.advisor.model == "advisor"
        assert vault.resolve_ref.await_count == 4
    finally:
        await bundle.aclose()


@pytest.mark.asyncio
async def test_build_session_llm_clients_main_required():
    """ctx.llm_main must be present — every session needs a main
    LLM.  None is a misconfigured runtime-config payload and the
    factory raises so the dispatcher fails the session."""
    from surogates.harness.session_llm import build_session_llm_clients
    from surogates.runtime import AgentRuntimeContext

    ctx = AgentRuntimeContext(
        agent_id="a-1", org_id="o-1", project_id="p-1",
        enabled=True, config_version=1, storage_key_prefix="p/a",
        llm_main=None,
    )
    with pytest.raises(ValueError, match="llm_main"):
        await build_session_llm_clients(ctx, vault=AsyncMock())
