"""LLM retries shrink oversized image input after a provider rejection."""

from __future__ import annotations

import base64
import io
import random
from types import SimpleNamespace

import pytest
from PIL import Image

from surogates.harness.llm_call import call_llm_with_retry


def _noisy_image_data_url(size: int = 512) -> str:
    rng = random.Random(1234)
    data = bytes(rng.randrange(256) for _ in range(size * size * 3))
    image = Image.frombytes("RGB", (size, size), data)
    output = io.BytesIO()
    image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


@pytest.mark.asyncio
async def test_llm_retry_shrinks_image_after_provider_error(monkeypatch) -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": _noisy_image_data_url()}},
            ],
        }
    ]
    first = Exception("image exceeds the maximum 5MB")
    first.status_code = 400  # type: ignore[attr-defined]

    def fake_shrink(parts):
        parts[0]["content"][0]["image_url"]["url"] = "data:image/jpeg;base64,AAA="
        return 1

    monkeypatch.setattr(
        "surogates.harness.llm_call.shrink_image_parts_in_messages",
        fake_shrink,
    )

    async def create(**kwargs):
        if create.calls == 0:
            create.calls += 1
            raise first
        create.calls += 1
        url = kwargs["messages"][0]["content"][0]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        return SimpleNamespace(
            model="test-model",
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        model_dump=lambda **_kwargs: {
                            "role": "assistant",
                            "content": "ok",
                        }
                    )
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
            ),
        )

    create.calls = 0
    llm_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )

    assistant, _usage = await call_llm_with_retry(
        session=SimpleNamespace(id="session-1"),
        create_kwargs={"model": "test-model", "messages": messages},
        iteration=1,
        llm_client=llm_client,
        store=SimpleNamespace(emit_event=lambda *_args, **_kwargs: None),
        streaming_enabled=False,
        interrupt_check=lambda: False,
        activate_fallback=lambda: False,
        get_current_model=lambda: "test-model",
        set_streaming_enabled=lambda _enabled: None,
    )

    assert assistant["content"] == "ok"
    assert create.calls == 2
