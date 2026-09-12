"""Reasoning controls for short auxiliary LLM requests."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def reasoning_disabled_extra_body(model: str, base_url: str = "") -> dict[str, Any] | None:
    url = urlsplit(base_url)
    # The managed summary route can retain an old model alias while
    # its proxy switches upstream models. Resolve this by endpoint so
    # short output allowances are available for the requested answer.
    if url.hostname == "openrouter.ai" or "/proxy/services/_summary_llm/" in url.path:
        return {"reasoning": {"enabled": False}}
    if model.lower() == "surogate":
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
