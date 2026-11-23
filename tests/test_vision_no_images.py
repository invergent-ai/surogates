"""An engine that takes no images gets one clear tool error, and any other 400 behaves as before."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI
from PIL import Image

from surogates.harness import image_read
from surogates.harness.image_read import handle_image_read
from surogates.tools.builtin import vision
from surogates.tools.registry import ToolRegistry

#: As an engine with images off answered, from a recorded run.
RECORDED = "At most 0 image(s) may be provided in one prompt. (parameter=image)"
CLEAR = (
    "This model cannot view images here. Do not retry this or another image; "
    "check the file with text tools instead."
)


def _engine(message: str, status: int = 400) -> tuple[AsyncOpenAI, list[httpx.Request]]:
    """The real client, against an engine that answers every request with this error."""
    asked: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        error = {"message": message, "type": "BadRequestError", "param": "image", "code": status}
        return httpx.Response(status, json={"error": error})

    client = AsyncOpenAI(
        api_key="synthetic", base_url="http://engine.invalid/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(answer)),
    )
    return client, asked


@pytest.fixture
def picture(tmp_path: Path) -> Path:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), (200, 40, 10)).save(buffer, format="PNG")
    (tmp_path / "chart.png").write_bytes(buffer.getvalue())
    return tmp_path


@pytest.fixture(autouse=True)
def empty_cache():
    image_read._CACHE.clear()
    yield
    image_read._CACHE.clear()


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    vision.register(registry)
    return registry


async def _analyze(client, workspace: Path) -> dict:
    answer = await _registry().dispatch(
        "vision_analyze", {"image": "chart.png"},
        llm_client=client, model="m", workspace_path=str(workspace),
    )
    return json.loads(answer)


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [RECORDED, "At most 0 image(s) may be provided in one prompt."])
async def test_vision_analyze_says_the_model_cannot_view_images(picture, message) -> None:
    client, asked = _engine(message)
    async with client:
        said = await _analyze(client, picture)
    assert said == {"error": CLEAR}
    assert len(asked) == 1


@pytest.mark.asyncio
async def test_read_file_on_an_image_gets_the_same_message(picture) -> None:
    client, asked = _engine(RECORDED)
    registry = _registry()
    kwargs = {"session_id": "s1", "llm_client": client, "model": "m", "workspace_path": str(picture)}
    async with client:
        answer = await handle_image_read("chart.png", {"path": "chart.png"}, registry.dispatch, kwargs)
    error = json.loads(answer)["error"]
    assert error == f"read_file could not analyze image 'chart.png': {CLEAR}"
    assert "400" not in error and len(asked) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "At most 1 image(s) may be provided in one prompt. (parameter=image)",
    "At most 0 video(s) may be provided in one prompt.",
    f"{RECORDED} Also, the prompt is too long.",
    f"Your text said: {RECORDED}",
    "Invalid image format",
])
async def test_any_other_400_behaves_as_before(picture, message) -> None:
    client, _asked = _engine(message)
    async with client:
        said = await _analyze(client, picture)
    assert said["error"].startswith("Tool execution failed: Error code: 400")
    assert CLEAR not in said["error"]


@pytest.mark.asyncio
async def test_the_same_words_with_another_status_behave_as_before(picture) -> None:
    client, _asked = _engine(RECORDED, status=422)
    async with client:
        said = await _analyze(client, picture)
    assert said["error"].startswith("Tool execution failed: Error code: 422")
