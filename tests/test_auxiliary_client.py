"""Auxiliary LLM client tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from surogates.harness.auxiliary_client import (
    build_summary_auxiliary_llm,
    build_vision_auxiliary_llm,
)
from surogates.harness.context import ContextCompressor, SUMMARY_PREFIX


def test_build_summary_auxiliary_llm_uses_tenant_overrides() -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            summary_model="global-summary",
            summary_base_url="https://global.example/v1",
            summary_api_key="global-key",
            base_url="https://main.example/v1",
            api_key="main-key",
        )
    )
    tenant = SimpleNamespace(
        org_config={
            "llm": {
                "summary_model": "org-summary",
                "summary_base_url": "https://org.example/v1",
                "summary_api_key": "org-key",
            }
        },
        user_preferences={"llm": {"summary_model": "user-summary"}},
    )

    aux = build_summary_auxiliary_llm(settings, tenant)

    assert aux is not None
    assert aux.model == "user-summary"
    assert str(aux.client.base_url).rstrip("/") == "https://org.example/v1"


def test_build_summary_auxiliary_llm_returns_none_without_model() -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            summary_model="",
            summary_base_url="",
            summary_api_key="",
            base_url="https://main.example/v1",
            api_key="main-key",
        )
    )

    assert build_summary_auxiliary_llm(settings) is None


def test_build_vision_auxiliary_llm_uses_dedicated_endpoint() -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            vision_model="vision-pro",
            vision_base_url="https://vision.example/v1",
            vision_api_key="vision-key",
            base_url="https://main.example/v1",
            api_key="main-key",
        )
    )

    aux = build_vision_auxiliary_llm(settings)

    assert aux is not None
    assert aux.model == "vision-pro"
    assert str(aux.client.base_url).rstrip("/") == "https://vision.example/v1"


def test_build_vision_auxiliary_llm_falls_back_to_main_endpoint() -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            vision_model="vision-pro",
            vision_base_url="",
            vision_api_key="",
            base_url="https://main.example/v1",
            api_key="main-key",
        )
    )

    aux = build_vision_auxiliary_llm(settings)

    assert aux is not None
    assert str(aux.client.base_url).rstrip("/") == "https://main.example/v1"


def test_build_vision_auxiliary_llm_returns_none_without_model() -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            vision_model="",
            vision_base_url="",
            vision_api_key="",
            base_url="https://main.example/v1",
            api_key="main-key",
        )
    )

    assert build_vision_auxiliary_llm(settings) is None


def test_build_vision_auxiliary_llm_user_pref_picks_model_only() -> None:
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            vision_model="global-vision",
            vision_base_url="https://global.example/v1",
            vision_api_key="global-key",
            base_url="https://main.example/v1",
            api_key="main-key",
        )
    )
    tenant = SimpleNamespace(
        org_config={
            "llm": {
                "vision_base_url": "https://org.example/v1",
                "vision_api_key": "org-key",
            }
        },
        user_preferences={"llm": {"vision_model": "user-vision"}},
    )

    aux = build_vision_auxiliary_llm(settings, tenant)

    assert aux is not None
    assert aux.model == "user-vision"
    assert str(aux.client.base_url).rstrip("/") == "https://org.example/v1"


@pytest.mark.asyncio
async def test_context_compressor_uses_summary_client_for_summaries() -> None:
    class FakeCompletions:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="## Goal\nKeep going")
                    )
                ]
            )

    summary_completions = FakeCompletions()
    main_completions = FakeCompletions()
    summary_client = SimpleNamespace(
        chat=SimpleNamespace(completions=summary_completions)
    )
    main_client = SimpleNamespace(chat=SimpleNamespace(completions=main_completions))
    compressor = ContextCompressor(
        "gpt-5.5",
        summary_model_override="gpt-5.4-mini",
        summary_client=summary_client,
        quiet_mode=True,
    )

    summary = await compressor._generate_summary(
        [{"role": "user", "content": "please summarize this"}],
        main_client,
    )

    assert summary is not None
    assert summary.startswith(SUMMARY_PREFIX)
    assert len(summary_completions.calls) == 1
    assert summary_completions.calls[0]["model"] == "gpt-5.4-mini"
    assert main_completions.calls == []
