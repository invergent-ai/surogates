"""Tests for the generate_image / generate_video builtin tools."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from surogates.tools.registry import ToolRegistry


def _registry() -> ToolRegistry:
    from surogates.tools.builtin import media_gen

    registry = ToolRegistry()
    media_gen.register(registry)
    return registry


def test_media_gen_tools_register_unconditionally():
    registry = _registry()
    assert registry.get("generate_image") is not None
    assert registry.get("generate_video") is not None


@pytest.mark.asyncio
async def test_generate_image_errors_when_unconfigured():
    from surogates.tools.builtin.media_gen import _generate_image_handler

    result = json.loads(await _generate_image_handler({"prompt": "a cat"}))
    assert "not available" in result["error"]


@pytest.mark.asyncio
async def test_generate_video_errors_when_unconfigured():
    from surogates.tools.builtin.media_gen import _generate_video_handler

    result = json.loads(await _generate_video_handler({"prompt": "a cat"}))
    assert "not available" in result["error"]


@pytest.mark.asyncio
async def test_save_media_bytes_writes_local_workspace(tmp_path):
    from surogates.tools.builtin.media_gen import _save_media_bytes

    saved = await _save_media_bytes(
        b"png-bytes",
        relative_path="media/images/x.png",
        workspace_path=str(tmp_path),
        storage=None,
        session_id=None,
        session_config=None,
    )
    assert saved is True
    assert (tmp_path / "media" / "images" / "x.png").read_bytes() == b"png-bytes"


@pytest.mark.asyncio
async def test_save_media_bytes_writes_storage_backend():
    from surogates.tools.builtin.media_gen import _save_media_bytes

    storage = SimpleNamespace(write=AsyncMock())
    saved = await _save_media_bytes(
        b"mp4-bytes",
        relative_path="media/videos/x.mp4",
        workspace_path=None,
        storage=storage,
        session_id="sess-1",
        session_config={"storage_bucket": "agent-bucket", "storage_key_prefix": "org/agent"},
    )
    assert saved is True
    storage.write.assert_awaited_once()
    bucket, key, data = storage.write.await_args.args
    assert bucket == "agent-bucket"
    assert key.endswith("media/videos/x.mp4")
    assert data == b"mp4-bytes"


@pytest.mark.asyncio
async def test_save_media_bytes_false_when_no_destination():
    from surogates.tools.builtin.media_gen import _save_media_bytes

    saved = await _save_media_bytes(
        b"data",
        relative_path="media/images/x.png",
        workspace_path=None,
        storage=None,
        session_id=None,
        session_config=None,
    )
    assert saved is False


def test_normalize_output_path_blocks_traversal():
    from surogates.tools.builtin.media_gen import _normalize_output_path
    from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError

    with pytest.raises(WorkspaceSandboxError):
        _normalize_output_path("../../etc/passwd", default="x.png")


def test_normalize_output_path_defaults_when_empty():
    from surogates.tools.builtin.media_gen import _normalize_output_path

    assert _normalize_output_path("", default="media/images/d.png") == "media/images/d.png"
    assert _normalize_output_path("/abs/cleaned.png", default="d.png") == "abs/cleaned.png"


_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="  # 1x1 png


class _FakeMessage:
    def __init__(self, images, content="here is your image"):
        self.images = images
        self.content = content
        self.role = "assistant"

    def model_dump(self, **_):
        return {
            "role": self.role,
            "content": self.content,
            "images": self.images,
        }


class _FakeImageClient:
    def __init__(self, images, model="google/gemini-2.5-flash-image"):
        self._response = SimpleNamespace(
            choices=[SimpleNamespace(message=_FakeMessage(images))],
            model=model,
        )
        self.last_create_kwargs = None
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    async def _create(self, **kwargs):
        self.last_create_kwargs = kwargs
        return self._response


def _image_cfg(client):
    from surogates.tools.builtin.media_gen import MediaGenConfig

    return MediaGenConfig(
        image_client=client, image_model="google/gemini-2.5-flash-image",
    )


@pytest.mark.asyncio
async def test_generate_image_saves_png_and_returns_path(tmp_path):
    from surogates.tools.builtin.media_gen import _generate_image_handler

    client = _FakeImageClient(
        images=[{"image_url": {"url": f"data:image/png;base64,{_PNG_B64}"}}],
    )
    result = json.loads(await _generate_image_handler(
        {"prompt": "a red square", "aspect_ratio": "1:1"},
        media_gen=_image_cfg(client),
        workspace_path=str(tmp_path),
    ))
    assert "error" not in result
    assert result["path"].startswith("media/images/")
    assert result["path"].endswith(".png")
    assert (tmp_path / result["path"]).is_file()
    assert result["text"] == "here is your image"
    extra_body = client.last_create_kwargs["extra_body"]
    assert extra_body["modalities"] == ["image", "text"]
    assert extra_body["aspect_ratio"] == "1:1"


@pytest.mark.asyncio
async def test_generate_image_honors_output_path(tmp_path):
    from surogates.tools.builtin.media_gen import _generate_image_handler

    client = _FakeImageClient(
        images=[{"image_url": {"url": f"data:image/png;base64,{_PNG_B64}"}}],
    )
    result = json.loads(await _generate_image_handler(
        {"prompt": "a red square", "output_path": "art/logo.png"},
        media_gen=_image_cfg(client),
        workspace_path=str(tmp_path),
    ))
    assert result["path"] == "art/logo.png"
    assert (tmp_path / "art" / "logo.png").is_file()


@pytest.mark.asyncio
async def test_generate_image_rejects_traversal_output_path(tmp_path):
    from surogates.tools.builtin.media_gen import _generate_image_handler

    client = _FakeImageClient(images=[])
    result = json.loads(await _generate_image_handler(
        {"prompt": "x", "output_path": "../escape.png"},
        media_gen=_image_cfg(client),
        workspace_path=str(tmp_path),
    ))
    assert "Path traversal blocked" in result["error"]
    assert client.last_create_kwargs is None  # rejected before the API call


@pytest.mark.asyncio
async def test_generate_image_errors_when_model_returns_no_image(tmp_path):
    from surogates.tools.builtin.media_gen import _generate_image_handler

    client = _FakeImageClient(images=[])
    result = json.loads(await _generate_image_handler(
        {"prompt": "a red square"},
        media_gen=_image_cfg(client),
        workspace_path=str(tmp_path),
    ))
    assert result["error"] == "The image model returned no image"


@pytest.mark.asyncio
async def test_generate_image_sends_input_images_as_content_parts(tmp_path):
    from surogates.tools.builtin.media_gen import _generate_image_handler

    source = tmp_path / "ref.png"
    source.write_bytes(base64.b64decode(_PNG_B64))
    client = _FakeImageClient(
        images=[{"image_url": {"url": f"data:image/png;base64,{_PNG_B64}"}}],
    )
    result = json.loads(await _generate_image_handler(
        {"prompt": "same but blue", "input_images": ["ref.png"]},
        media_gen=_image_cfg(client),
        workspace_path=str(tmp_path),
    ))
    assert "error" not in result
    content = client.last_create_kwargs["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "same but blue"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_generate_image_errors_without_workspace_destination():
    from surogates.tools.builtin.media_gen import _generate_image_handler

    client = _FakeImageClient(
        images=[{"image_url": {"url": f"data:image/png;base64,{_PNG_B64}"}}],
    )
    result = json.loads(await _generate_image_handler(
        {"prompt": "a red square"},
        media_gen=_image_cfg(client),
    ))
    assert "workspace_unavailable" in result["error"]
