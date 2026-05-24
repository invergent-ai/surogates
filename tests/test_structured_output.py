from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai import AsyncOpenAI
from pydantic import BaseModel

from surogates.harness.structured_output import generate_structured


class RoutingDecision(BaseModel):
    route: str
    confidence: float


async def test_generate_structured_uses_vllm_guided_json_for_custom_base_url() -> None:
    llm_client = AsyncOpenAI(
        api_key="test-key",
        base_url="http://localhost:8000/v1",
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
        model="surogate",
        messages=[{"role": "user", "content": "Route this."}],
        output_model=RoutingDecision,
        max_tokens=64,
    )

    assert decision == RoutingDecision(route="ask_user_question", confidence=0.95)
    call_kwargs = llm_client.chat.completions.create.await_args.kwargs
    assert call_kwargs["model"] == "surogate"
    assert "response_format" not in call_kwargs
    assert call_kwargs["extra_body"]["guided_json"]["type"] == "object"
    assert "route" in call_kwargs["extra_body"]["guided_json"]["properties"]


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


# ---------------------------------------------------------------------------
# JSON-mode fallback (when Outlines returns None)
# ---------------------------------------------------------------------------


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
