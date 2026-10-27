"""Reasoning controls for short auxiliary LLM requests."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from surogates.harness.expert_routing import model_supports_thinking_toggle


def reasoning_disabled_extra_body(model: str, base_url: str = "") -> dict[str, Any] | None:
    url = urlsplit(base_url)
    # The managed summary route can retain an old model alias while
    # its proxy switches upstream models. Resolve this by endpoint so
    # short output allowances are available for the requested answer.
    if url.hostname == "openrouter.ai" or "/proxy/services/_summary_llm/" in url.path:
        return {"reasoning": {"enabled": False}}
    if model.lower() == "surogate":
        # The proxy forwards the sentinel to the live base tier: OpenRouter
        # on PROD, which honours only ``reasoning`` (DeepSeek v4.1 flash
        # ignores the chat-template form: 132 reasoning tokens with it,
        # 0 with ``reasoning``), or a vLLM server, which reads the latter.
        return {
            "reasoning": {"enabled": False},
            "chat_template_kwargs": {"enable_thinking": False},
        }
    if model.lower() != "surogate-pro" and model_supports_thinking_toggle(model):
        # A chat-template model reached by name (surogate3.7 through the
        # /_model route, Qwen3, GLM). The pro sentinel is Claude behind the
        # proxy, which has no template to switch.
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return None


def rejects_reasoning_controls(exc: Exception) -> bool:
    """Identify an unsupported reasoning setting without blaming JSON mode."""
    status = getattr(exc, "status_code", None)
    if not isinstance(exc, TypeError) and not (
        isinstance(status, int) and 400 <= status < 500 and status != 429
    ):
        return False
    text = str(exc).lower()
    return any(name in text for name in (
        "reasoning", "chat_template_kwargs", "enable_thinking",
    )) and any(reason in text for reason in (
        "unsupported", "not supported", "unknown", "unrecognized", "unexpected",
    ))


async def create_without_reasoning(client: Any, **request: Any) -> Any:
    """``client.chat.completions.create(**request)`` with the model's reasoning off where it can be turned off.

    A gateway that rejects the reasoning controls gets the request as it
    was, its own ``extra_body``, ``response_format`` and output bound kept.
    """
    extra_body = reasoning_disabled_extra_body(
        request["model"], str(getattr(client, "base_url", "") or ""),
    )
    if extra_body is None:
        return await client.chat.completions.create(**request)
    try:
        return await client.chat.completions.create(
            **{**request, "extra_body": {**request.get("extra_body", {}), **extra_body}},
        )
    except Exception as exc:
        if not rejects_reasoning_controls(exc):
            raise
        return await client.chat.completions.create(**request)
