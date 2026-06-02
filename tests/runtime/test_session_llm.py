"""Tests for SessionLLMClients.

Per-session bundle holding the four AsyncOpenAI
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


class _RecordingOpenAI:
    """Stand-in for ``AsyncOpenAI`` that records the api_key it was
    built with (so tests don't depend on a real key / ambient
    ``OPENAI_API_KEY`` env var) and supports aclose()."""

    def __init__(self, *, api_key=None, base_url=None, **_k):
        self.api_key = api_key
        self.base_url = base_url

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_build_session_llm_clients_falls_back_to_org_scoped_key(monkeypatch):
    """User-principal sessions (``user_id`` set) resolve the org-scoped
    platform/BYO key when the user has no personal override.

    The platform admission key is seeded org-scoped (``user_id IS
    NULL``); resolving it with the session's ``user_id`` misses that
    row, so without a user->org fallback the main client is built with
    a ``None`` api_key and ``AsyncOpenAI`` raises 'Missing credentials',
    failing every webapp (user-principal) session while service-account
    sessions (``user_id is None``) keep working.  Regression for PROD
    session 4309ac36.
    """
    monkeypatch.setattr(
        "surogates.harness.session_llm.AsyncOpenAI", _RecordingOpenAI,
        raising=False,
    )
    from surogates.harness.session_llm import build_session_llm_clients
    from surogates.runtime import AgentRuntimeContext, LLMEndpoint

    ctx = AgentRuntimeContext(
        agent_id="a-1", org_id="o-1", project_id="p-1",
        enabled=True, config_version=1, storage_key_prefix="p/a",
        llm_main=LLMEndpoint(
            model="gpt-4", base_url="https://api.example.com",
            api_key_ref="vault://platform_admission_key",
        ),
    )

    async def _resolve(_ref, *, org_id, user_id):
        # Only an org-scoped credential exists (user has no override).
        return "sk-org" if user_id is None else None

    vault = AsyncMock()
    vault.resolve_ref = AsyncMock(side_effect=_resolve)

    bundle = await build_session_llm_clients(ctx, vault=vault, user_id="u-1")
    try:
        assert bundle.main.client.api_key == "sk-org"
    finally:
        await bundle.aclose()


@pytest.mark.asyncio
async def test_build_session_llm_clients_prefers_user_scoped_key(monkeypatch):
    """A user-scoped override wins over the org default and the org
    fallback lookup is not issued when the user key resolves."""
    monkeypatch.setattr(
        "surogates.harness.session_llm.AsyncOpenAI", _RecordingOpenAI,
        raising=False,
    )
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

    async def _resolve(_ref, *, org_id, user_id):
        return "sk-user" if user_id == "u-1" else "sk-org"

    vault = AsyncMock()
    vault.resolve_ref = AsyncMock(side_effect=_resolve)

    bundle = await build_session_llm_clients(ctx, vault=vault, user_id="u-1")
    try:
        assert bundle.main.client.api_key == "sk-user"
        # User key resolved on the first call -> no org fallback lookup.
        vault.resolve_ref.assert_awaited_once_with(
            "vault://main-key", org_id="o-1", user_id="u-1",
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


@pytest.mark.asyncio
async def test_build_session_llm_clients_closes_partial_bundle_on_vault_failure(
    monkeypatch,
):
    """if vault.resolve_ref raises after main has
    been instantiated, the partially-built AsyncOpenAI instances must
    be aclose()d before re-raising the error.  Otherwise every failed
    session start leaks a connection pool per resolved slot, and a
    flaky vault produces an unbounded FD leak over the worker's
    lifetime."""
    from surogates.harness.session_llm import build_session_llm_clients
    from surogates.runtime import AgentRuntimeContext, LLMEndpoint

    closed: list[AsyncMock] = []

    class _FakeOpenAI:
        def __init__(self, *_a, **_k):
            self._closed = False
            closed.append(self)

        async def close(self):
            self._closed = True

    monkeypatch.setattr(
        "surogates.harness.session_llm.AsyncOpenAI", _FakeOpenAI,
        raising=False,
    )

    def _ep(name):
        return LLMEndpoint(
            model=name, base_url="u", api_key_ref=f"vault://{name}",
        )

    ctx = AgentRuntimeContext(
        agent_id="a-1", org_id="o-1", project_id="p-1",
        enabled=True, config_version=1, storage_key_prefix="p/a",
        llm_main=_ep("main"),
        llm_summary=_ep("summary"),  # this slot will fail
    )

    call_count = 0

    async def flaky_resolve(_ref, **_kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "sk-main"
        raise RuntimeError("vault unreachable")

    vault = AsyncMock()
    vault.resolve_ref = flaky_resolve

    with pytest.raises(RuntimeError, match="vault unreachable"):
        await build_session_llm_clients(ctx, vault=vault)

    # Main was instantiated; on the failure the factory must have
    # aclose()d it before propagating.
    assert len(closed) == 1
    assert closed[0]._closed is True


@pytest.mark.asyncio
async def test_build_session_llm_clients_closes_partial_bundle_on_later_slot_failure(
    monkeypatch,
):
    """The leak window extends to summary/vision/advisor too — if
    advisor fails after main+summary+vision succeeded, all three
    must close."""
    from surogates.harness.session_llm import build_session_llm_clients
    from surogates.runtime import AgentRuntimeContext, LLMEndpoint

    closed: list = []

    class _FakeOpenAI:
        def __init__(self, *_a, **_k):
            self._closed = False
            closed.append(self)

        async def close(self):
            self._closed = True

    monkeypatch.setattr(
        "surogates.harness.session_llm.AsyncOpenAI", _FakeOpenAI,
        raising=False,
    )

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

    call_count = 0

    async def flaky_resolve(_ref, **_kw):
        nonlocal call_count
        call_count += 1
        if call_count <= 3:  # main, summary, vision succeed
            return f"sk-{call_count}"
        raise RuntimeError("vault unreachable for advisor")

    vault = AsyncMock()
    vault.resolve_ref = flaky_resolve

    with pytest.raises(RuntimeError, match="advisor"):
        await build_session_llm_clients(ctx, vault=vault)

    # All three resolved slots must be closed.
    assert len(closed) == 3
    assert all(c._closed for c in closed)
