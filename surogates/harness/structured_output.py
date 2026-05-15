"""Shared structured-output generation for internal harness LLM calls."""

from __future__ import annotations

import logging
import re
from typing import Any, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


async def generate_structured(
    *,
    llm_client: Any,
    model: str,
    messages: list[dict[str, str]],
    output_model: type[T],
    max_tokens: int = 300,
    temperature: float = 0,
) -> T | None:
    """Generate and validate a structured Pydantic object with Outlines."""
    try:
        import outlines
        from outlines.inputs import Chat
    except ImportError:
        logger.info("Outlines is not installed; structured generation skipped")
        return None

    try:
        outlines_model = _make_outlines_model(
            outlines_module=outlines,
            llm_client=llm_client,
            model=model,
        )
        result = await outlines_model.generate(
            Chat(messages),
            output_model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return _coerce_structured_result(result, output_model)
    except Exception as exc:
        logger.info(
            "Structured generation with Outlines failed: %s",
            exc,
        )
        return None


def _make_outlines_model(
    *,
    outlines_module: Any,
    llm_client: Any,
    model: str,
) -> Any:
    """Choose the Outlines adapter for the configured OpenAI-compatible client."""
    base_url = str(getattr(llm_client, "base_url", "") or "")
    if base_url and "api.openai.com" not in base_url:
        return outlines_module.from_vllm(llm_client, model)
    return outlines_module.from_openai(llm_client, model)


def _coerce_structured_result(value: Any, output_model: type[T]) -> T:
    if isinstance(value, output_model):
        return value
    if isinstance(value, dict):
        return output_model.model_validate(value)
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return output_model.model_validate_json(text)
