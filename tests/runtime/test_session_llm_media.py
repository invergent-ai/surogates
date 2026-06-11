"""Tests for the image bundle slot and resolve_video_endpoint."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from surogates.runtime.context import AgentRuntimeContext, LLMEndpoint


def _ctx(**kw):
    return AgentRuntimeContext(
        agent_id="agent-1",
        org_id="org-1",
        enabled=True,
        config_version=1,
        storage_key_prefix="org-1/agent-1",
        llm_main=LLMEndpoint(
            model="main-model",
            base_url="https://openrouter.ai/api/v1",
            api_key_ref="vault://main-key",
        ),
        **kw,
    )


def _vault():
    return SimpleNamespace(resolve_ref=AsyncMock(return_value="sk-test"))


def _settings(**llm_overrides):
    llm = SimpleNamespace(
        model="main-model",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-global",
        image_model="",
        image_base_url="",
        image_api_key="",
        video_model="",
        video_base_url="",
        video_api_key="",
    )
    for key, value in llm_overrides.items():
        setattr(llm, key, value)
    return SimpleNamespace(llm=llm)


@pytest.mark.asyncio
async def test_bundle_builds_image_slot_from_context():
    from surogates.harness.session_llm import build_session_llm_clients

    ctx = _ctx(llm_image=LLMEndpoint(
        model="img-model",
        base_url="https://openrouter.ai/api/v1",
        api_key_ref="vault://img-key",
    ))
    bundle = await build_session_llm_clients(ctx, vault=_vault())
    assert bundle.image is not None
    assert bundle.image.model == "img-model"
    await bundle.aclose()


@pytest.mark.asyncio
async def test_bundle_image_slot_falls_back_to_settings():
    from surogates.harness.session_llm import build_session_llm_clients

    bundle = await build_session_llm_clients(
        _ctx(), vault=_vault(),
        settings=_settings(image_model="google/gemini-2.5-flash-image"),
    )
    assert bundle.image is not None
    assert bundle.image.model == "google/gemini-2.5-flash-image"
    await bundle.aclose()


@pytest.mark.asyncio
async def test_bundle_image_slot_none_when_unconfigured():
    from surogates.harness.session_llm import build_session_llm_clients

    bundle = await build_session_llm_clients(
        _ctx(), vault=_vault(), settings=_settings(),
    )
    assert bundle.image is None
    await bundle.aclose()


@pytest.mark.asyncio
async def test_resolve_video_endpoint_from_context_resolves_vault_key():
    from surogates.harness.session_llm import resolve_video_endpoint

    ctx = _ctx(llm_video=LLMEndpoint(
        model="google/veo-3.1",
        base_url="https://openrouter.ai/api/v1",
        api_key_ref="vault://vid-key",
    ))
    endpoint = await resolve_video_endpoint(ctx, vault=_vault())
    assert endpoint is not None
    assert endpoint.model == "google/veo-3.1"
    assert endpoint.api_key == "sk-test"


@pytest.mark.asyncio
async def test_resolve_video_endpoint_falls_back_to_settings():
    from surogates.harness.session_llm import resolve_video_endpoint

    endpoint = await resolve_video_endpoint(
        _ctx(), vault=_vault(),
        settings=_settings(video_model="google/veo-3.1"),
    )
    assert endpoint is not None
    assert endpoint.model == "google/veo-3.1"
    assert endpoint.base_url == "https://openrouter.ai/api/v1"  # main base_url fallback
    assert endpoint.api_key == "sk-global"  # main api_key fallback


@pytest.mark.asyncio
async def test_resolve_video_endpoint_none_when_unconfigured():
    from surogates.harness.session_llm import resolve_video_endpoint

    endpoint = await resolve_video_endpoint(_ctx(), vault=_vault(), settings=_settings())
    assert endpoint is None
