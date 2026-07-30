"""Shared structured-output generation for internal harness LLM calls."""

from __future__ import annotations

import json
import logging
from typing import Any, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def iter_json_objects(content: Any):
    """Yield every decodable JSON object embedded in raw model text.

    Providers that ignore ``response_format={"type": "json_object"}``
    (Claude via OpenAI-compatible gateways, notably) return the object
    wrapped in markdown fences and sometimes surrounded by prose.
    Accepts the text as-is, fenced, or embedded in prose.  Valid JSON
    that is not an object (array, string, ...) is skipped -- every
    caller wants a mapping.  A truncated outer object can still yield
    complete objects nested inside it; callers that validate against a
    schema should keep scanning past candidates that fail validation.
    """
    if not isinstance(content, str):
        return
    text = content.strip()
    if not text:
        return
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(parsed, dict):
            yield parsed
        return
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            candidate, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
            continue
        if isinstance(candidate, dict):
            yield candidate
            idx = text.find("{", end)
        else:
            idx = text.find("{", idx + 1)
    return


def parse_json_object(content: Any) -> dict[str, Any] | None:
    """First JSON object embedded in raw model text, or ``None``.

    See :func:`iter_json_objects` for the extraction rules.  First
    object wins by design -- without a schema there is no way to rank
    candidates, and clean fenced output has exactly one.
    """
    return next(iter_json_objects(content), None)


async def generate_structured(
    *,
    llm_client: Any,
    model: str,
    messages: list[dict[str, str]],
    output_model: type[T],
    max_tokens: int = 300,
    temperature: float = 0,
) -> T | None:
    """Generate and validate a structured Pydantic object.

    Tries Outlines first (vLLM / OpenAI guided-JSON via grammar
    constraint).  If that returns nothing usable -- the model didn't
    emit valid output, the provider silently ignored the constraint,
    or Outlines isn't installed -- falls through to an OpenAI
    ``response_format={"type": "json_object"}`` request with the
    schema injected into the prompt and validates the response with
    Pydantic ourselves.

    Both paths returning ``None`` means the caller should fall back to
    a non-structured strategy.
    """
    result = await _try_outlines(
        llm_client=llm_client,
        model=model,
        messages=messages,
        output_model=output_model,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if result is not None:
        return result

    return await _try_openai_json_mode(
        llm_client=llm_client,
        model=model,
        messages=messages,
        output_model=output_model,
        max_tokens=max_tokens,
        temperature=temperature,
    )


async def _try_outlines(
    *,
    llm_client: Any,
    model: str,
    messages: list[dict[str, str]],
    output_model: type[T],
    max_tokens: int,
    temperature: float,
) -> T | None:
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


async def _try_openai_json_mode(
    *,
    llm_client: Any,
    model: str,
    messages: list[dict[str, str]],
    output_model: type[T],
    max_tokens: int,
    temperature: float,
) -> T | None:
    """Fallback: plain chat completion with ``response_format=json_object``.

    Some providers (DeepInfra in particular) accept the OpenAI JSON
    mode flag even when their vLLM ``guided_json`` passthrough is
    flaky.  We inject the JSON schema into the last message so the
    model knows what shape to emit, request ``json_object`` mode, and
    validate the result with Pydantic ourselves.  Returns ``None``
    when the provider rejects the parameter, returns empty, or the
    output doesn't validate.
    """
    if not messages:
        return None

    try:
        schema_str = json.dumps(
            output_model.model_json_schema(), separators=(",", ":"),
        )
    except Exception:
        logger.debug("Schema generation failed for JSON-mode fallback")
        return None

    augmented = [dict(m) for m in messages]
    last = augmented[-1]
    original = last.get("content", "")
    if not isinstance(original, str):
        # Tool-result or multimodal content -- JSON-mode injection isn't
        # safe to attempt here without flattening, and the kinds of
        # callers that hit this path use string content anyway.
        return None

    last["content"] = (
        f"{original}\n\n"
        "Respond with a single JSON object matching this schema. "
        "Do not include any text before or after the JSON.\n"
        f"Schema: {schema_str}"
    )

    try:
        response = await llm_client.chat.completions.create(
            model=model,
            messages=augmented,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        logger.debug("JSON-mode fallback provider call failed: %s", exc)
        return None

    if not getattr(response, "choices", None):
        return None
    message = getattr(response.choices[0], "message", None)

    # Reasoning-mode models (DeepSeek-R1, Qwen3 with thinking on, GLM 5.1
    # with enable_thinking=True) often put visible text in
    # ``message.content`` and the JSON we asked for in
    # ``message.reasoning_content`` -- empty content alone isn't a failure
    # if the JSON is sitting in the reasoning channel.  Both channels are
    # tried, and within each, every embedded object -- leaked reasoning
    # can contain draft objects before the real answer, and the schema
    # is the only reliable way to tell them apart.
    for attr in ("content", "reasoning_content"):
        for parsed in iter_json_objects(getattr(message, attr, None)):
            try:
                return output_model.model_validate(parsed)
            except Exception as exc:
                logger.debug(
                    "JSON-mode fallback validation failed on %s: %s",
                    attr, exc,
                )
    return None


def _make_outlines_model(
    *,
    outlines_module: Any,
    llm_client: Any,
    model: str,
) -> Any:
    """Build an Outlines adapter for an OpenAI-compatible client.

    Every OpenAI-compatible endpoint we target -- OpenAI, OpenRouter,
    DeepInfra, Together, our own chat-completion proxy, and modern vLLM
    deployments -- accepts ``response_format={"type": "json_schema",
    "strict": true, ...}``, which is what Outlines' ``from_openai``
    adapter emits.  The ``from_vllm`` adapter instead sends
    ``extra_body={"guided_json": ...}``, a vLLM server-only parameter
    that routers like OpenRouter silently drop; the model then returns
    free-form text and the call always fails.  Using ``from_openai``
    unconditionally is correct for every endpoint we actually call; if
    a provider rejects ``json_schema`` mode the exception is caught
    upstream and the JSON-mode fallback in
    :func:`_try_openai_json_mode` takes over.
    """
    return outlines_module.from_openai(llm_client, model)


def _coerce_structured_result(value: Any, output_model: type[T]) -> T:
    if isinstance(value, output_model):
        return value
    if isinstance(value, dict):
        return output_model.model_validate(value)
    parsed = parse_json_object(str(value or ""))
    if parsed is None:
        raise ValueError("model output contains no JSON object")
    return output_model.model_validate(parsed)
