"""Auxiliary LLM client construction for low-cost harness side work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from surogates.config import Settings
    from surogates.tenant.context import TenantContext


@dataclass(frozen=True)
class AuxiliaryLLM:
    client: AsyncOpenAI
    model: str


def build_summary_auxiliary_llm(
    settings: Settings,
    tenant: TenantContext | None = None,
) -> AuxiliaryLLM | None:
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
        or llm.summary_model
    )
    if not model:
        return None

    base_url = (
        _mapping_get(org_llm, "summary_base_url")
        or llm.summary_base_url
        or llm.base_url
    )
    api_key = (
        _mapping_get(org_llm, "summary_api_key")
        or llm.summary_api_key
        or llm.api_key
    )

    kwargs: dict[str, Any] = {"api_key": api_key or "EMPTY"}
    if base_url:
        kwargs["base_url"] = base_url
    return AuxiliaryLLM(client=AsyncOpenAI(**kwargs), model=str(model))


def build_base_auxiliary_llm(
    settings: Settings,
    tenant: TenantContext | None = None,
) -> AuxiliaryLLM | None:
    """Build an auxiliary client targeting the base LLM, if configured.

    Used for harness-side tasks that should run against the same model
    that handles the main turn (e.g. the hard-task classifier in
    :func:`classify_hard_task_async`). Reusing the base endpoint means
    the classifier benefits from the same upstream prefix cache and
    provider warmth as the iteration loop, instead of paying an extra
    round trip against a separately-tiered summary endpoint.
    """
    llm = settings.llm
    org_llm = _mapping_get(getattr(tenant, "org_config", None), "llm")
    user_llm = _mapping_get(getattr(tenant, "user_preferences", None), "llm")

    model = (
        _mapping_get(user_llm, "model")
        or _mapping_get(org_llm, "model")
        or llm.model
    )
    if not model:
        return None

    base_url = (
        _mapping_get(org_llm, "base_url")
        or llm.base_url
    )
    api_key = (
        _mapping_get(org_llm, "api_key")
        or llm.api_key
    )

    kwargs: dict[str, Any] = {"api_key": api_key or "EMPTY"}
    if base_url:
        kwargs["base_url"] = base_url
    return AuxiliaryLLM(client=AsyncOpenAI(**kwargs), model=str(model))


def build_vision_auxiliary_llm(
    settings: Settings,
    tenant: TenantContext | None = None,
) -> AuxiliaryLLM | None:
    """Build an auxiliary client for image description, if configured.

    The harness consults a vision model when the active LLM lacks vision
    support, replacing image parts with text descriptions before sending
    the messages on.  ``llm.vision_model`` is required; the endpoint and
    key fall back to the main LLM credentials when ``vision_base_url`` /
    ``vision_api_key`` are blank, matching the summary client convention.
    """
    llm = settings.llm
    org_llm = _mapping_get(getattr(tenant, "org_config", None), "llm")
    user_llm = _mapping_get(getattr(tenant, "user_preferences", None), "llm")

    model = (
        _mapping_get(user_llm, "vision_model")
        or _mapping_get(org_llm, "vision_model")
        or llm.vision_model
    )
    if not model:
        return None

    base_url = (
        _mapping_get(org_llm, "vision_base_url")
        or llm.vision_base_url
        or llm.base_url
    )
    api_key = (
        _mapping_get(org_llm, "vision_api_key")
        or llm.vision_api_key
        or llm.api_key
    )

    kwargs: dict[str, Any] = {"api_key": api_key or "EMPTY"}
    if base_url:
        kwargs["base_url"] = base_url
    return AuxiliaryLLM(client=AsyncOpenAI(**kwargs), model=str(model))


def build_advisor_auxiliary_llm(
    settings: Settings,
    tenant: TenantContext | None = None,
) -> AuxiliaryLLM | None:
    """Build an auxiliary client for hidden strategic advisor calls."""
    llm = settings.llm
    org_llm = _mapping_get(getattr(tenant, "org_config", None), "llm")
    user_llm = _mapping_get(getattr(tenant, "user_preferences", None), "llm")

    enabled = (
        _mapping_get(user_llm, "advisor_enabled")
        if _mapping_get(user_llm, "advisor_enabled") is not None
        else _mapping_get(org_llm, "advisor_enabled")
    )
    if enabled is None:
        enabled = llm.advisor_enabled
    if not bool(enabled):
        return None

    model = (
        _mapping_get(user_llm, "advisor_model")
        or _mapping_get(org_llm, "advisor_model")
        or llm.advisor_model
    )
    if not model:
        return None

    base_url = (
        _mapping_get(org_llm, "advisor_base_url")
        or llm.advisor_base_url
        or llm.base_url
    )
    api_key = (
        _mapping_get(org_llm, "advisor_api_key")
        or llm.advisor_api_key
        or llm.api_key
    )

    kwargs: dict[str, Any] = {"api_key": api_key or "EMPTY"}
    if base_url:
        kwargs["base_url"] = base_url
    return AuxiliaryLLM(client=AsyncOpenAI(**kwargs), model=str(model))


def _mapping_get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return None
