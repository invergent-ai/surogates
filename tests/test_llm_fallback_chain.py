"""The fallback provider chain: configured per agent, resolved with the
session's other LLM slots, walked by the harness when the primary fails."""
from __future__ import annotations

import importlib.util
import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from surogates.harness.session_llm import (
    ResolvedLLM,
    SessionLLMClients,
    build_session_llm_clients,
)
from surogates.runtime.resolver import build_agent_runtime_context
from tests.test_steer_loop import _make_loop_harness


def _payload(**extra: Any) -> dict[str, Any]:
    return {
        "agent_id": "agent-1", "org_id": "org-1", "project_id": "proj-1",
        "enabled": True, "version": 3, "storage_key_prefix": "p/",
        "llm_main": {
            "model": "main-model", "base_url": "https://primary.example",
            "api_key_ref": "vault://main",
        },
        **extra,
    }


# -- config projection ------------------------------------------------------


def test_runtime_config_projects_the_fallback_chain():
    ctx = build_agent_runtime_context(_payload(llm_fallbacks=[
        {"model": "second", "base_url": "https://b.example", "api_key_ref": "vault://b"},
        {"model": "third", "base_url": "https://c.example", "api_key_ref": "vault://c"},
    ]))
    assert [e.model for e in ctx.llm_fallbacks] == ["second", "third"]
    assert ctx.llm_fallbacks[0].base_url == "https://b.example"


def test_an_agent_without_a_chain_gets_an_empty_one():
    assert build_agent_runtime_context(_payload()).llm_fallbacks == ()


# -- bundle resolution ------------------------------------------------------


class _Vault:
    async def resolve_ref(self, ref: str, **_kw: Any) -> str:
        return f"key-for-{ref.rsplit('/', 1)[-1]}"


@pytest.mark.asyncio
async def test_bundle_resolves_each_fallback_endpoint():
    ctx = build_agent_runtime_context(_payload(llm_fallbacks=[
        {"model": "second", "base_url": "https://b.example", "api_key_ref": "vault://b"},
    ]))
    bundle = await build_session_llm_clients(ctx, vault=_Vault())
    try:
        assert [s.model for s in bundle.fallbacks] == ["second"]
        # A distinct client, not the primary's: the endpoint moves with
        # the model or the request misroutes.
        assert bundle.fallbacks[0].client is not bundle.main.client
        assert str(bundle.fallbacks[0].client.base_url).startswith("https://b.example")
    finally:
        await bundle.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_the_fallback_clients():
    closed: list[str] = []
    bundle = SessionLLMClients(
        main=ResolvedLLM(client=SimpleNamespace(
            close=AsyncMock(side_effect=lambda: closed.append("main"))), model="m"),
        summary=None, vision=None, advisor=None,
        fallbacks=(
            ResolvedLLM(client=SimpleNamespace(
                close=AsyncMock(side_effect=lambda: closed.append("fb"))), model="f"),
        ),
    )
    await bundle.aclose()
    assert closed == ["main", "fb"]


# -- the harness walks it ---------------------------------------------------


def _harness_with_chain(*models: str):
    h = _make_loop_harness(session_store=AsyncMock())
    h._llm = SimpleNamespace(name="primary")
    h._current_model = "main-model"
    h._fallback_chain = tuple(
        ResolvedLLM(client=SimpleNamespace(name=m), model=m) for m in models
    )
    h._fallback_index = 0
    h._fallback_activated = False
    h._primary_config = None
    return h


def test_activating_a_fallback_swaps_client_and_model_together():
    h = _harness_with_chain("second")
    assert h._try_activate_fallback() is True
    assert h._current_model == "second"
    assert h._llm.name == "second"
    assert h._fallback_activated is True


def test_the_primary_is_remembered_on_the_first_activation():
    h = _harness_with_chain("second", "third")
    h._try_activate_fallback()
    h._try_activate_fallback()
    assert h._primary_config == {"llm_client": SimpleNamespace(name="primary"),
                                 "model": "main-model"}
    assert h._current_model == "third"


def test_an_exhausted_chain_returns_false():
    h = _harness_with_chain("second")
    assert h._try_activate_fallback() is True
    assert h._try_activate_fallback() is False


def test_no_chain_returns_false():
    assert _harness_with_chain()._try_activate_fallback() is False


@pytest.mark.asyncio
async def test_a_rate_limit_moves_the_session_to_the_next_provider(monkeypatch):
    """Waiting out a retry-after is worse for the session than finishing
    it somewhere else, so a 429 fails over rather than sleeping."""
    from surogates.harness import llm_call

    calls: list[str] = []

    class _RateLimited(Exception):
        status_code = 429

    async def create(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise _RateLimited("rate limited")
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="done", tool_calls=None, role="assistant"),
                finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            model="second",
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        base_url="https://primary.example",
    )
    model = {"v": "main-model"}

    def activate_fallback() -> bool:
        model["v"] = "second"
        return True

    store = AsyncMock()
    store.emit_event = AsyncMock(return_value=1)

    await llm_call.call_llm_with_retry(
        session=SimpleNamespace(id="s", config={}),
        create_kwargs={"model": "main-model", "messages": []},
        iteration=1,
        llm_client=client,
        store=store,
        streaming_enabled=False,
        interrupt_check=lambda: False,
        activate_fallback=activate_fallback,
        get_current_model=lambda: model["v"],
        set_streaming_enabled=lambda _v: None,
    )

    assert calls == ["main-model", "second"]


def test_the_harness_takes_the_chain_from_its_caller():
    from surogates.harness.loop import AgentHarness

    assert "fallback_chain" in inspect.signature(AgentHarness.__init__).parameters


# -- credential pool is gone ------------------------------------------------


def test_the_credential_pool_is_gone():
    assert importlib.util.find_spec("surogates.harness.credentials") is None


def test_nothing_still_offers_credential_rotation():
    from surogates.harness import llm_call, resilience

    assert not hasattr(resilience, "try_rotate_credential")
    assert "rotate_credential" not in inspect.signature(
        llm_call.call_llm_with_retry,
    ).parameters
