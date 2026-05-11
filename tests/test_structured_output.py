from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
                            "route": "clarify",
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

    assert decision == RoutingDecision(route="clarify", confidence=0.95)
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
