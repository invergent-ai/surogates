"""Auxiliary LLM client construction for low-cost harness side work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI


@dataclass(frozen=True)
class AuxiliaryLLM:
    client: AsyncOpenAI
    model: str


def build_summary_auxiliary_llm(settings: Any, tenant: Any | None = None) -> AuxiliaryLLM | None:
    """Build an auxiliary client for compression summaries, if configured.

    Tenant org config may override the global summary model/client endpoint.
    User preferences may choose the summary model only; endpoint credentials
    stay operator/org controlled.
    """
    llm = settings.llm
    org_llm = _mapping_get(getattr(tenant, "org_config", None), "llm")
    user_llm = _mapping_get(getattr(tenant, "user_preferences", None), "llm")

    model = (
        _mapping_get(user_llm, "summary_model")
        or _mapping_get(org_llm, "summary_model")
        or getattr(llm, "summary_model", "")
    )
    if not model:
        return None

    base_url = (
        _mapping_get(org_llm, "summary_base_url")
        or getattr(llm, "summary_base_url", "")
        or getattr(llm, "base_url", "")
    )
    api_key = (
        _mapping_get(org_llm, "summary_api_key")
        or getattr(llm, "summary_api_key", "")
        or getattr(llm, "api_key", "")
    )

    kwargs: dict[str, Any] = {"api_key": api_key or "EMPTY"}
    if base_url:
        kwargs["base_url"] = base_url
    return AuxiliaryLLM(client=AsyncOpenAI(**kwargs), model=str(model))


def _mapping_get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return None
