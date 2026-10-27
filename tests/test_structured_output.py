from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel

from surogates.harness.structured_output import (
    StructuredCall,
    generate_structured,
    generate_structured_with_usage,
)


class RoutingDecision(BaseModel):
    route: str
    confidence: float


async def test_generate_structured_uses_json_schema_for_custom_base_url() -> None:
    """Non-OpenAI base URLs (OpenRouter, our proxy, DeepInfra, modern vLLM)
    all accept ``response_format={"type": "json_schema", strict: true}`` --
    the shape Outlines' ``from_openai`` adapter emits.  The old code
    routed these through ``from_vllm``, which sends a vLLM-only
    ``extra_body={"guided_json": ...}`` that routers silently drop; the
    model then returned free-form text and the call always failed.
    """
    llm_client = AsyncOpenAI(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
    )
    llm_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "route": "ask_user_question",
                            "confidence": 0.95,
                        }),
                        refusal=None,
                    )
                )
            ]
        )
    )

    decision = await generate_structured(
        llm_client=llm_client,
        model="@preset/summary",
        messages=[{"role": "user", "content": "Route this."}],
        output_model=RoutingDecision,
        max_tokens=64,
    )

    assert decision == RoutingDecision(route="ask_user_question", confidence=0.95)
    call_kwargs = llm_client.chat.completions.create.await_args.kwargs
    assert call_kwargs["model"] == "@preset/summary"
    assert "extra_body" not in call_kwargs
    assert call_kwargs["response_format"]["type"] == "json_schema"
    assert call_kwargs["response_format"]["json_schema"]["strict"] is True
    schema = call_kwargs["response_format"]["json_schema"]["schema"]
    assert schema["type"] == "object"
    assert "route" in schema["properties"]


async def test_generate_structured_uses_openai_response_format_by_default() -> None:
    llm_client = AsyncOpenAI(api_key="test-key")
    llm_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "route": "final",
                            "confidence": 0.7,
                        }),
                        refusal=None,
                    )
                )
            ]
        )
    )

    decision = await generate_structured(
        llm_client=llm_client,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Route this."}],
        output_model=RoutingDecision,
    )

    assert decision == RoutingDecision(route="final", confidence=0.7)
    call_kwargs = llm_client.chat.completions.create.await_args.kwargs
    assert call_kwargs["model"] == "gpt-4o-mini"
    assert "extra_body" not in call_kwargs
    assert call_kwargs["response_format"]["type"] == "json_schema"
    assert call_kwargs["response_format"]["json_schema"]["schema"]["type"] == "object"


async def test_generate_structured_strips_markdown_fences() -> None:
    llm_client = AsyncOpenAI(api_key="test-key")
    fenced = "```json\n" + json.dumps(
        {"route": "ask_user_question", "confidence": 0.42}
    ) + "\n```"
    llm_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=fenced, refusal=None)
                )
            ]
        )
    )

    decision = await generate_structured(
        llm_client=llm_client,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Route this."}],
        output_model=RoutingDecision,
    )

    assert decision == RoutingDecision(route="ask_user_question", confidence=0.42)


async def test_generate_structured_returns_none_when_outlines_fails() -> None:
    llm_client = AsyncOpenAI(api_key="test-key")
    llm_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("boom"))

    decision = await generate_structured(
        llm_client=llm_client,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Route this."}],
        output_model=RoutingDecision,
    )

    assert decision is None


async def test_falls_back_to_json_mode_when_outlines_returns_none() -> None:
    """When _try_outlines returns None, _try_openai_json_mode runs."""
    llm_client = AsyncOpenAI(api_key="test-key")
    llm_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"route": "ask_user_question", "confidence": 0.8}),
                        refusal=None,
                    )
                )
            ]
        )
    )

    with patch(
        "surogates.harness.structured_output._try_outlines",
        AsyncMock(return_value=None),
    ):
        decision = await generate_structured(
            llm_client=llm_client,
            model="gemma-4-31B",
            messages=[
                {"role": "system", "content": "Classify the user message."},
                {"role": "user", "content": "Route this."},
            ],
            output_model=RoutingDecision,
        )

    assert decision == RoutingDecision(route="ask_user_question", confidence=0.8)
    call_kwargs = llm_client.chat.completions.create.await_args.kwargs
    # Fallback uses the simpler json_object mode, not Outlines' json_schema.
    assert call_kwargs["response_format"] == {"type": "json_object"}
    # Schema is injected into the LAST message (so the model knows the shape).
    last_content = call_kwargs["messages"][-1]["content"]
    assert "Schema" in last_content
    assert "route" in last_content  # field name from the schema


async def test_fallback_strips_markdown_fences_around_json() -> None:
    llm_client = AsyncOpenAI(api_key="test-key")
    fenced = "```json\n" + json.dumps(
        {"route": "final", "confidence": 0.42}
    ) + "\n```"
    llm_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=fenced, refusal=None)
                )
            ]
        )
    )

    with patch(
        "surogates.harness.structured_output._try_outlines",
        AsyncMock(return_value=None),
    ):
        decision = await generate_structured(
            llm_client=llm_client,
            model="gemma-4-31B",
            messages=[{"role": "user", "content": "Route this."}],
            output_model=RoutingDecision,
        )

    assert decision == RoutingDecision(route="final", confidence=0.42)


async def test_fallback_returns_none_when_provider_rejects_response_format() -> None:
    llm_client = AsyncOpenAI(api_key="test-key")
    llm_client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("response_format not supported"),
    )

    with patch(
        "surogates.harness.structured_output._try_outlines",
        AsyncMock(return_value=None),
    ):
        decision = await generate_structured(
            llm_client=llm_client,
            model="some-old-model",
            messages=[{"role": "user", "content": "Route this."}],
            output_model=RoutingDecision,
        )

    assert decision is None


async def test_fallback_returns_none_when_response_empty() -> None:
    llm_client = AsyncOpenAI(api_key="test-key")
    llm_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="", refusal=None)
                )
            ]
        )
    )

    with patch(
        "surogates.harness.structured_output._try_outlines",
        AsyncMock(return_value=None),
    ):
        decision = await generate_structured(
            llm_client=llm_client,
            model="gemma-4-31B",
            messages=[{"role": "user", "content": "Route this."}],
            output_model=RoutingDecision,
        )

    assert decision is None


async def test_fallback_returns_none_when_response_invalid_json() -> None:
    llm_client = AsyncOpenAI(api_key="test-key")
    llm_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="this is not json at all",
                        refusal=None,
                    )
                )
            ]
        )
    )

    with patch(
        "surogates.harness.structured_output._try_outlines",
        AsyncMock(return_value=None),
    ):
        decision = await generate_structured(
            llm_client=llm_client,
            model="gemma-4-31B",
            messages=[{"role": "user", "content": "Route this."}],
            output_model=RoutingDecision,
        )

    assert decision is None


async def test_fallback_returns_none_when_validation_fails() -> None:
    """JSON parses but doesn't match the schema."""
    llm_client = AsyncOpenAI(api_key="test-key")
    llm_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"unrelated_field": 42}),
                        refusal=None,
                    )
                )
            ]
        )
    )

    with patch(
        "surogates.harness.structured_output._try_outlines",
        AsyncMock(return_value=None),
    ):
        decision = await generate_structured(
            llm_client=llm_client,
            model="gemma-4-31B",
            messages=[{"role": "user", "content": "Route this."}],
            output_model=RoutingDecision,
        )

    assert decision is None


async def test_fallback_skipped_for_empty_messages() -> None:
    llm_client = AsyncOpenAI(api_key="test-key")
    create_mock = AsyncMock()
    llm_client.chat.completions.create = create_mock

    with patch(
        "surogates.harness.structured_output._try_outlines",
        AsyncMock(return_value=None),
    ):
        decision = await generate_structured(
            llm_client=llm_client,
            model="gemma-4-31B",
            messages=[],
            output_model=RoutingDecision,
        )

    assert decision is None
    create_mock.assert_not_awaited()


async def test_fallback_reads_json_from_reasoning_content() -> None:
    """Prose in ``content`` must not mask the object a reasoning-mode
    model parked in ``reasoning_content``."""
    llm_client = AsyncOpenAI(api_key="test-key")
    llm_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="I thought about it and answered below.",
                        reasoning_content=json.dumps(
                            {"route": "final", "confidence": 0.9}
                        ),
                        refusal=None,
                    )
                )
            ]
        )
    )

    with patch(
        "surogates.harness.structured_output._try_outlines",
        AsyncMock(return_value=None),
    ):
        decision = await generate_structured(
            llm_client=llm_client,
            model="deepseek-r1",
            messages=[{"role": "user", "content": "Route this."}],
            output_model=RoutingDecision,
        )

    assert decision == RoutingDecision(route="final", confidence=0.9)


async def test_fallback_skips_draft_objects_that_fail_the_schema() -> None:
    """Leaked reasoning can park a draft object ahead of the answer;
    the schema is what tells them apart."""
    llm_client = AsyncOpenAI(api_key="test-key")
    leaked = (
        'Let me think. Maybe {"route": "unsure"} — no. Final answer:\n'
        "```json\n"
        + json.dumps({"route": "final", "confidence": 0.6})
        + "\n```"
    )
    llm_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=leaked, refusal=None)
                )
            ]
        )
    )

    with patch(
        "surogates.harness.structured_output._try_outlines",
        AsyncMock(return_value=None),
    ):
        decision = await generate_structured(
            llm_client=llm_client,
            model="gemma-4-31B",
            messages=[{"role": "user", "content": "Route this."}],
            output_model=RoutingDecision,
        )

    assert decision == RoutingDecision(route="final", confidence=0.6)


def _reply(content: str, *, usage: SimpleNamespace | None = None, model: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, refusal=None))],
        usage=usage,
        model=model,
    )


async def test_a_structured_call_counts_every_completion_it_received() -> None:
    llm_client = AsyncOpenAI(api_key="test-key")
    llm_client.chat.completions.create = AsyncMock(side_effect=[
        _reply(
            "not json",
            usage=SimpleNamespace(
                prompt_tokens=900,
                completion_tokens=40,
                prompt_tokens_details=SimpleNamespace(cached_tokens=300),
            ),
            model="provider/model-a",
        ),
        _reply(
            json.dumps({"route": "ask_user_question", "confidence": 0.5}),
            usage=SimpleNamespace(
                prompt_tokens=950,
                completion_tokens=60,
                cache_read_input_tokens=100,
                prompt_tokens_details=None,
            ),
            model="provider/model-b",
        ),
    ])

    call = await generate_structured_with_usage(
        llm_client=llm_client,
        model="@preset/summary",
        messages=[{"role": "user", "content": "Route this."}],
        output_model=RoutingDecision,
        max_tokens=64,
    )

    assert call == StructuredCall(
        RoutingDecision(route="ask_user_question", confidence=0.5),
        completions=2,
        input_tokens=1850,
        output_tokens=100,
        cache_read_tokens=400,
        served_by="provider/model-b",
    )
    first, second = (c.kwargs for c in llm_client.chat.completions.create.await_args_list)
    assert first["response_format"]["type"] == "json_schema"
    assert second["response_format"] == {"type": "json_object"}


async def test_a_structured_call_that_reaches_no_model_used_nothing() -> None:
    llm_client = AsyncOpenAI(api_key="test-key")
    llm_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("boom"))

    call = await generate_structured_with_usage(
        llm_client=llm_client,
        model="@preset/summary",
        messages=[{"role": "user", "content": "Route this."}],
        output_model=RoutingDecision,
    )

    assert call == StructuredCall(None, 0, 0, 0, 0, None)


async def test_a_reply_without_usage_counts_as_a_completion_that_used_nothing() -> None:
    llm_client = AsyncOpenAI(api_key="test-key")
    llm_client.chat.completions.create = AsyncMock(
        return_value=_reply(json.dumps({"route": "r", "confidence": 1.0})),
    )

    call = await generate_structured_with_usage(
        llm_client=llm_client,
        model="m",
        messages=[{"role": "user", "content": "Route this."}],
        output_model=RoutingDecision,
    )

    assert call == StructuredCall(RoutingDecision(route="r", confidence=1.0), 1, 0, 0, 0, None)


@pytest.mark.parametrize("function", [generate_structured, generate_structured_with_usage])
async def test_a_failed_structured_attempt_logs_only_the_errors_class(function, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="surogates.harness.structured_output")
    llm_client = AsyncOpenAI(api_key="test-key")
    llm_client.chat.completions.create = AsyncMock(side_effect=[
        _reply("MARKER-7f3 not json"),
        _reply('{"route": "MARKER-7f3"}'),
    ])

    result = await function(
        llm_client=llm_client,
        model="m",
        messages=[{"role": "user", "content": "Route this."}],
        output_model=RoutingDecision,
    )

    assert getattr(result, "value", result) is None
    assert "MARKER-7f3" not in caplog.text
