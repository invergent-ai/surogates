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
