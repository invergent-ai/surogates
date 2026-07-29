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


def _patch_video_transport(monkeypatch, handler):
    """Route the module's httpx.AsyncClient through a MockTransport."""
    import httpx as _httpx

    real_client = _httpx.AsyncClient

    def _factory(**kwargs):
        kwargs["transport"] = _httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(
        "surogates.tools.builtin.media_gen.httpx.AsyncClient", _factory,
    )


def _video_cfg(**overrides):
    from surogates.tools.builtin.media_gen import MediaGenConfig

    defaults = dict(
        video_model="google/veo-3.1",
        video_base_url="https://openrouter.ai/api/v1",
        video_api_key="sk-vid",
        video_timeout=600,
        video_poll_interval=1,
    )
    defaults.update(overrides)
    return MediaGenConfig(**defaults)


@pytest.mark.asyncio
async def test_generate_video_submits_polls_downloads_and_saves(tmp_path, monkeypatch):
    import httpx as _httpx

    from surogates.tools.builtin.media_gen import _generate_video_handler

    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    poll_count = {"n": 0}

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/videos"):
            body = json.loads(request.content)
            assert body["model"] == "google/veo-3.1"
            assert body["prompt"] == "a rocket launch"
            assert body["resolution"] == "720p"
            assert request.headers["authorization"] == "Bearer sk-vid"
            return _httpx.Response(202, json={
                "id": "job-1",
                "polling_url": "https://openrouter.ai/api/v1/videos/job-1",
                "status": "pending",
            })
        if request.method == "GET" and request.url.path.endswith("/videos/job-1"):
            poll_count["n"] += 1
            if poll_count["n"] < 2:
                return _httpx.Response(200, json={"id": "job-1", "status": "in_progress"})
            return _httpx.Response(200, json={
                "id": "job-1",
                "status": "completed",
                "unsigned_urls": ["https://openrouter.ai/api/v1/videos/job-1/content?index=0"],
                "usage": {"cost": 0.25, "is_byok": False},
            })
        if request.method == "GET" and "content" in str(request.url):
            return _httpx.Response(200, content=b"mp4-bytes")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    _patch_video_transport(monkeypatch, handler)
    result = json.loads(await _generate_video_handler(
        {"prompt": "a rocket launch", "resolution": "720p"},
        media_gen=_video_cfg(),
        workspace_path=str(tmp_path),
    ))
    assert "error" not in result
    assert result["path"].startswith("media/videos/")
    assert result["path"].endswith(".mp4")
    assert result["job_id"] == "job-1"
    assert result["cost"] == 0.25
    assert (tmp_path / result["path"]).read_bytes() == b"mp4-bytes"


@pytest.mark.asyncio
async def test_generate_video_reports_failed_job(tmp_path, monkeypatch):
    import httpx as _httpx

    from surogates.tools.builtin.media_gen import _generate_video_handler

    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    def handler(request):
        if request.method == "POST":
            return _httpx.Response(202, json={"id": "job-2", "status": "pending"})
        return _httpx.Response(200, json={
            "id": "job-2", "status": "failed", "error": "content policy",
        })

    _patch_video_transport(monkeypatch, handler)
    result = json.loads(await _generate_video_handler(
        {"prompt": "x"},
        media_gen=_video_cfg(),
        workspace_path=str(tmp_path),
    ))
    assert "Video generation failed" in result["error"]
    assert "content policy" in result["error"]


@pytest.mark.asyncio
async def test_generate_video_times_out_and_surfaces_job_id(tmp_path, monkeypatch):
    import httpx as _httpx

    from surogates.tools.builtin.media_gen import _generate_video_handler

    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    def handler(request):
        if request.method == "POST":
            return _httpx.Response(202, json={"id": "job-3", "status": "pending"})
        return _httpx.Response(200, json={"id": "job-3", "status": "in_progress"})

    _patch_video_transport(monkeypatch, handler)
    result = json.loads(await _generate_video_handler(
        {"prompt": "x"},
        media_gen=_video_cfg(video_timeout=0),
        workspace_path=str(tmp_path),
    ))
    assert "timed out" in result["error"]
    assert "job-3" in result["error"]


@pytest.mark.asyncio
async def test_generate_video_includes_first_frame_image(tmp_path, monkeypatch):
    import httpx as _httpx

    from surogates.tools.builtin.media_gen import _generate_video_handler

    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    source = tmp_path / "frame.png"
    source.write_bytes(base64.b64decode(_PNG_B64))
    captured = {}

    def handler(request):
        if request.method == "POST":
            captured["body"] = json.loads(request.content)
            return _httpx.Response(202, json={"id": "job-4", "status": "pending"})
        if request.url.path.endswith("/videos/job-4"):
            return _httpx.Response(200, json={
                "id": "job-4", "status": "completed",
                "unsigned_urls": ["https://openrouter.ai/api/v1/videos/job-4/content"],
            })
        return _httpx.Response(200, content=b"mp4")

    _patch_video_transport(monkeypatch, handler)
    result = json.loads(await _generate_video_handler(
        {"prompt": "animate this", "first_frame_image": "frame.png"},
        media_gen=_video_cfg(),
        workspace_path=str(tmp_path),
    ))
    assert "error" not in result
    frame = captured["body"]["frame_images"][0]
    assert frame["frame_type"] == "first_frame"
    assert frame["image_url"]["url"].startswith("data:image/png;base64,")


def test_media_gen_tools_route_to_harness():
    """Regression: tools absent from TOOL_LOCATIONS fall back to SANDBOX
    routing and die there as 'Unknown tool' (the sandbox tool-executor
    has no media_gen config, LLM client, or storage) — exactly how
    generate_image failed in DEV. Both media tools must be explicitly
    HARNESS-routed, like vision_analyze.
    """
    from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

    for name in ("generate_image", "generate_video"):
        assert TOOL_LOCATIONS.get(name) is ToolLocation.HARNESS, (
            f"{name} is not HARNESS-routed; sandbox fallback surfaces "
            f"it as 'Unknown tool'"
        )


class _FakeApiClient:
    def __init__(self, response='{"success": true}', raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    async def create_artifact(self, *, name, kind, spec):
        self.calls.append({"name": name, "kind": kind, "spec": spec})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


@pytest.mark.asyncio
async def test_generate_image_creates_inline_artifact(tmp_path):
    from surogates.tools.builtin.media_gen import _generate_image_handler

    client = _FakeImageClient(
        images=[{"image_url": {"url": f"data:image/png;base64,{_PNG_B64}"}}],
    )
    api_client = _FakeApiClient()
    result = json.loads(await _generate_image_handler(
        {"prompt": "a red square"},
        media_gen=_image_cfg(client),
        workspace_path=str(tmp_path),
        api_client=api_client,
    ))
    assert result["artifact"] is True
    call = api_client.calls[0]
    assert call["kind"] == "image"
    assert call["spec"]["path"] == result["path"]
    assert call["spec"]["mime_type"] == "image/png"
    assert call["spec"]["caption"] == "a red square"
    assert "/" not in call["name"]  # artifact names reject path separators


@pytest.mark.asyncio
async def test_generate_image_survives_artifact_failure(tmp_path):
    from surogates.tools.builtin.media_gen import _generate_image_handler

    client = _FakeImageClient(
        images=[{"image_url": {"url": f"data:image/png;base64,{_PNG_B64}"}}],
    )
    api_client = _FakeApiClient(raise_exc=RuntimeError("api down"))
    result = json.loads(await _generate_image_handler(
        {"prompt": "a red square"},
        media_gen=_image_cfg(client),
        workspace_path=str(tmp_path),
        api_client=api_client,
    ))
    assert "error" not in result
    assert "artifact" not in result  # file saved, artifact best-effort
    assert (tmp_path / result["path"]).is_file()


@pytest.mark.asyncio
async def test_generate_video_creates_inline_artifact(tmp_path, monkeypatch):
    import httpx as _httpx

    from surogates.tools.builtin.media_gen import _generate_video_handler

    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    def handler(request):
        if request.method == "POST":
            return _httpx.Response(202, json={"id": "job-a", "status": "pending"})
        if request.url.path.endswith("/videos/job-a"):
            return _httpx.Response(200, json={
                "id": "job-a", "status": "completed",
                "unsigned_urls": ["https://or.example/api/v1/videos/job-a/content"],
            })
        return _httpx.Response(200, content=b"mp4")

    _patch_video_transport(monkeypatch, handler)
    api_client = _FakeApiClient()
    result = json.loads(await _generate_video_handler(
        {"prompt": "waves at sunset"},
        media_gen=_video_cfg(),
        workspace_path=str(tmp_path),
        api_client=api_client,
    ))
    assert result["artifact"] is True
    call = api_client.calls[0]
    assert call["kind"] == "video"
    assert call["spec"]["path"] == result["path"]
    assert call["spec"]["mime_type"] == "video/mp4"


def test_media_artifact_specs_validate():
    import pytest as _pytest
    from pydantic import ValidationError

    from surogates.artifacts.models import (
        ArtifactKind, ArtifactSpec, ImageSpec, VideoSpec,
    )

    ImageSpec(path="media/images/x.png", mime_type="image/png", caption="c")
    VideoSpec(path="media/videos/x.mp4")
    ArtifactSpec(
        name="x.png", kind=ArtifactKind.IMAGE,
        spec={"path": "media/images/x.png"},
    ).validate_spec()
    for bad in ("", "/abs/x.png", "../escape.png", "a/../b.png"):
        with _pytest.raises(ValidationError):
            ImageSpec(path=bad)


@pytest.mark.asyncio
async def test_generate_video_surfaces_upstream_error_body(tmp_path, monkeypatch):
    import httpx as _httpx

    from surogates.tools.builtin.media_gen import _generate_video_handler

    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    def handler(request):
        return _httpx.Response(400, json={
            "error": {"message": "@preset/video is not a valid model ID"},
        })

    _patch_video_transport(monkeypatch, handler)
    result = json.loads(await _generate_video_handler(
        {"prompt": "x"},
        media_gen=_video_cfg(),
        workspace_path=str(tmp_path),
    ))
    assert "400 Bad Request" in result["error"]
    assert "not a valid model ID" in result["error"]  # upstream body surfaced


# ── budget-exhausted (HTTP 402) handling ────────────────────────────


class _Broke402Client:
    """Raises the shape the OpenAI SDK uses for a 402 from the proxy."""

    class _Error(Exception):
        status_code = 402

    def __init__(self):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    async def _create(self, **_kwargs):
        raise self._Error("Error code: 402 - insufficient_credits")


@pytest.mark.asyncio
async def test_generate_image_402_returns_budget_guidance(tmp_path):
    from surogates.tools.builtin.media_gen import _generate_image_handler

    result = json.loads(await _generate_image_handler(
        {"prompt": "a red square"},
        media_gen=_image_cfg(_Broke402Client()),
        workspace_path=str(tmp_path),
    ))
    assert result["error"].startswith("media_budget_exhausted")
    assert "top up" in result["error"]


@pytest.mark.asyncio
async def test_generate_image_non_402_keeps_generic_error(tmp_path):
    from surogates.tools.builtin.media_gen import _generate_image_handler

    class _Broke500Client(_Broke402Client):
        class _Error(Exception):
            status_code = 500

        async def _create(self, **_kwargs):
            raise self._Error("boom")

    result = json.loads(await _generate_image_handler(
        {"prompt": "a red square"},
        media_gen=_image_cfg(_Broke500Client()),
        workspace_path=str(tmp_path),
    ))
    assert result["error"].startswith("Image generation failed")


@pytest.mark.asyncio
async def test_generate_video_402_returns_budget_guidance(monkeypatch, tmp_path):
    import httpx

    from surogates.tools.builtin.media_gen import (
        MediaGenConfig,
        _generate_video_handler,
    )

    def _respond(request):
        return httpx.Response(
            402,
            json={"detail": {"error": "insufficient_credits",
                             "resource": "media_credits"}},
        )

    transport = httpx.MockTransport(_respond)
    real_client = httpx.AsyncClient

    def _patched_client(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _patched_client)
    cfg = MediaGenConfig(
        video_model="x-ai/grok-imagine-video",
        video_base_url="http://proxy.test/v1",
        video_api_key="k",
        video_poll_interval=1,
    )
    result = json.loads(await _generate_video_handler(
        {"prompt": "a rocket"},
        media_gen=cfg,
        workspace_path=str(tmp_path),
    ))
    assert result["error"].startswith("media_budget_exhausted")


# ── worker schema gating (_media_tool_exclusions) ───────────────────


def _cfg(**kwargs):
    from surogates.tools.builtin.media_gen import MediaGenConfig

    return MediaGenConfig(**kwargs)


def test_media_tool_exclusions_drop_both_when_nothing_configured():
    from surogates.orchestrator.worker import _media_tool_exclusions

    assert _media_tool_exclusions(_cfg()) == {
        "generate_image", "generate_video",
    }


def test_media_tool_exclusions_keep_image_when_wired():
    from surogates.orchestrator.worker import _media_tool_exclusions

    cfg = _cfg(image_client=object(), image_model="google/gemini-2.5-flash-image")
    assert _media_tool_exclusions(cfg) == {"generate_video"}


def test_media_tool_exclusions_keep_video_when_wired():
    from surogates.orchestrator.worker import _media_tool_exclusions

    cfg = _cfg(video_model="x-ai/grok-imagine-video",
               video_base_url="http://proxy.test/v1")
    assert _media_tool_exclusions(cfg) == {"generate_image"}


def test_media_tool_exclusions_empty_when_both_wired():
    from surogates.orchestrator.worker import _media_tool_exclusions

    cfg = _cfg(
        image_client=object(),
        image_model="google/gemini-2.5-flash-image",
        video_model="x-ai/grok-imagine-video",
        video_base_url="http://proxy.test/v1",
    )
    assert _media_tool_exclusions(cfg) == set()


def test_media_tool_exclusions_video_needs_both_model_and_url():
    from surogates.orchestrator.worker import _media_tool_exclusions

    assert "generate_video" in _media_tool_exclusions(
        _cfg(video_model="x-ai/grok-imagine-video"),
    )
    assert "generate_video" in _media_tool_exclusions(
        _cfg(video_base_url="http://proxy.test/v1"),
    )


# ── per-buyer budget hooks ──────────────────────────────────────────


class _HookRecorder:
    def __init__(self, receipt=None, authorize_exc=None):
        self.receipt = receipt
        self.authorize_exc = authorize_exc
        self.authorize_calls = []
        self.settle_calls = []

    async def authorize(self, requested_cents=None):
        self.authorize_calls.append(requested_cents)
        if self.authorize_exc is not None:
            raise self.authorize_exc
        return self.receipt

    async def settle(self, receipt, actual_cents, external_ref):
        self.settle_calls.append((receipt, actual_cents, external_ref))


def _billed_image_cfg(client, hooks, image_cents=4):
    from surogates.tools.builtin.media_gen import MediaGenConfig

    return MediaGenConfig(
        image_client=client,
        image_model="google/gemini-2.5-flash-image",
        budget_authorize=hooks.authorize,
        budget_settle=hooks.settle,
        image_cents=image_cents,
    )


@pytest.mark.asyncio
async def test_image_metered_holds_and_settles_flat_price(tmp_path):
    from surogates.tools.builtin.media_gen import _generate_image_handler

    hooks = _HookRecorder(receipt={"balance_id": "b1", "reserved_cents": 4})
    client = _FakeImageClient(
        images=[{"image_url": {"url": f"data:image/png;base64,{_PNG_B64}"}}],
    )
    result = json.loads(await _generate_image_handler(
        {"prompt": "a red square"},
        media_gen=_billed_image_cfg(client, hooks),
        workspace_path=str(tmp_path),
    ))
    assert "error" not in result
    assert hooks.authorize_calls == [4]
    assert len(hooks.settle_calls) == 1
    receipt, cents, ref = hooks.settle_calls[0]
    assert receipt == {"balance_id": "b1", "reserved_cents": 4}
    assert cents == 4
    assert ref.startswith("img-")


@pytest.mark.asyncio
async def test_image_budget_exhausted_blocks_before_provider(tmp_path):
    from surogates.tools.builtin.media_gen import (
        MediaBudgetExhaustedError,
        _generate_image_handler,
    )

    hooks = _HookRecorder(
        authorize_exc=MediaBudgetExhaustedError(
            "media_credits_exhausted", buy_url="https://buy.example/x",
        ),
    )
    client = _FakeImageClient(images=[])
    result = json.loads(await _generate_image_handler(
        {"prompt": "a red square"},
        media_gen=_billed_image_cfg(client, hooks),
        workspace_path=str(tmp_path),
    ))
    assert result["error"].startswith("media_credits_exhausted")
    assert "https://buy.example/x" in result["error"]
    assert client.last_create_kwargs is None  # provider never called
    assert hooks.settle_calls == []


@pytest.mark.asyncio
async def test_image_metering_plane_down_fails_closed(tmp_path):
    from surogates.tools.builtin.media_gen import _generate_image_handler

    hooks = _HookRecorder(authorize_exc=RuntimeError("ops unreachable"))
    client = _FakeImageClient(images=[])
    result = json.loads(await _generate_image_handler(
        {"prompt": "a red square"},
        media_gen=_billed_image_cfg(client, hooks),
        workspace_path=str(tmp_path),
    ))
    assert result["error"].startswith("media budget check unavailable")
    assert client.last_create_kwargs is None


@pytest.mark.asyncio
async def test_image_provider_failure_releases_hold(tmp_path):
    from surogates.tools.builtin.media_gen import (
        MediaGenConfig,
        _generate_image_handler,
    )

    hooks = _HookRecorder(receipt={"balance_id": "b1", "reserved_cents": 4})
    cfg = MediaGenConfig(
        image_client=_Broke402Client(),
        image_model="google/gemini-2.5-flash-image",
        budget_authorize=hooks.authorize,
        budget_settle=hooks.settle,
    )
    result = json.loads(await _generate_image_handler(
        {"prompt": "a red square"},
        media_gen=cfg,
        workspace_path=str(tmp_path),
    ))
    assert "error" in result
    assert len(hooks.settle_calls) == 1
    assert hooks.settle_calls[0][1] == 0  # released, not spent


@pytest.mark.asyncio
async def test_image_unmetered_without_hooks_stays_free(tmp_path):
    """No hooks configured (the default) = exactly the old behavior."""
    from surogates.tools.builtin.media_gen import _generate_image_handler

    client = _FakeImageClient(
        images=[{"image_url": {"url": f"data:image/png;base64,{_PNG_B64}"}}],
    )
    result = json.loads(await _generate_image_handler(
        {"prompt": "a red square"},
        media_gen=_image_cfg(client),
        workspace_path=str(tmp_path),
    ))
    assert "error" not in result


# ── worker media budget hook factory ────────────────────────────────


def _hook_ctx(metered=True, cents=4, buy_url="https://buy.example/a"):
    return SimpleNamespace(
        media_credits_metered=metered,
        media_image_cents=cents,
        commerce_buy_url=buy_url,
        agent_id="agent-1",
    )


def _hook_session(config=None, user_id="u-1", service_account_id=None):
    return SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        config=config or {},
        user_id=user_id,
        service_account_id=service_account_id,
    )


def test_media_hooks_none_when_unmetered():
    from surogates.orchestrator.worker import _build_media_budget_hooks

    a, s = _build_media_budget_hooks(
        ctx=_hook_ctx(metered=False),
        session=_hook_session(),
        platform_client=object(),
    )
    assert a is None and s is None


def test_media_hooks_none_for_service_account_sessions():
    """Exemption keys on the SESSION row's service_account_id — the
    credential principal is the agent's own SA on managed channels
    and must not exempt Slack/Telegram buyers."""
    from surogates.orchestrator.worker import _build_media_budget_hooks

    a, s = _build_media_budget_hooks(
        ctx=_hook_ctx(),
        session=_hook_session(user_id=None, service_account_id="sa-1"),
        platform_client=object(),
    )
    assert a is None and s is None


def test_media_hooks_built_for_managed_channel_end_users():
    """A slack/telegram end-user session (human user_id, no session
    SA) gets metering hooks even though the CREDENTIAL principal for
    such sessions is the agent's own service account."""
    from surogates.orchestrator.worker import _build_media_budget_hooks

    a, s = _build_media_budget_hooks(
        ctx=_hook_ctx(),
        session=_hook_session(user_id="u-slack-1"),
        platform_client=object(),
    )
    assert a is not None and s is not None


@pytest.mark.asyncio
async def test_media_hooks_anonymous_sender_raises_buy_prompt():
    from surogates.orchestrator.worker import _build_media_budget_hooks
    from surogates.tools.builtin.media_gen import MediaBudgetExhaustedError

    authorize, _ = _build_media_budget_hooks(
        ctx=_hook_ctx(),
        session=_hook_session(user_id=None),
        platform_client=object(),
    )
    with pytest.raises(MediaBudgetExhaustedError) as exc_info:
        await authorize(4)
    assert exc_info.value.buy_url == "https://buy.example/a"


@pytest.mark.asyncio
async def test_media_hooks_authorize_and_settle_call_platform():
    from surogates.orchestrator.worker import _build_media_budget_hooks

    class _Client:
        def __init__(self):
            self.authorize_kwargs = None
            self.settle_kwargs = None

        async def media_authorize(self, agent_id, **kwargs):
            self.authorize_kwargs = {"agent_id": agent_id, **kwargs}
            return {
                "metered": True,
                "reservation_id": "r1",
                "balance_id": "b1",
                "reserved_cents": 4,
            }

        async def media_settle(self, agent_id, **kwargs):
            self.settle_kwargs = {"agent_id": agent_id, **kwargs}
            return {"settled": True}

    client = _Client()
    authorize, settle = _build_media_budget_hooks(
        ctx=_hook_ctx(),
        session=_hook_session(
            config={"commerce_buyer": {"firebase_uid": "fb-1"}},
        ),
        platform_client=client,
    )
    receipt = await authorize(4)
    assert client.authorize_kwargs["firebase_uid"] == "fb-1"
    assert client.authorize_kwargs["requested_cents"] == 4
    assert receipt["balance_id"] == "b1"
    await settle(receipt, 4, "img-x")
    assert client.settle_kwargs == {
        "agent_id": "agent-1",
        "balance_id": "b1",
        "reserved_cents": 4,
        "actual_cents": 4,
        "external_ref": "img-x",
        "reservation_id": "r1",
    }


@pytest.mark.asyncio
async def test_media_hooks_exhausted_maps_to_buy_prompt_error():
    from surogates.orchestrator.worker import _build_media_budget_hooks
    from surogates.runtime.platform_client import MediaCreditsExhaustedError
    from surogates.tools.builtin.media_gen import MediaBudgetExhaustedError

    class _BrokeClient:
        async def media_authorize(self, agent_id, **kwargs):
            raise MediaCreditsExhaustedError("media_credits_exhausted")

    authorize, _ = _build_media_budget_hooks(
        ctx=_hook_ctx(),
        session=_hook_session(),
        platform_client=_BrokeClient(),
    )
    with pytest.raises(MediaBudgetExhaustedError) as exc_info:
        await authorize(None)
    assert exc_info.value.buy_url == "https://buy.example/a"


# ── package capability exclusions for media tools ───────────────────


def test_entitlement_exclusions_drop_media_tools():
    from surogates.orchestrator.worker import _entitlement_tool_exclusions

    class _Registry:
        tool_names = ("generate_image", "generate_video")

        def get_all(self):
            return []

    session_config = {
        "entitlements": {"capabilities": ["code", "browser"]},
    }
    excluded = _entitlement_tool_exclusions(
        session_config=session_config,
        tool_registry=_Registry(),
        mcp_scope=None,
    )
    assert "generate_image" in excluded
    assert "generate_video" in excluded

    session_config = {
        "entitlements": {"capabilities": ["image", "video"]},
    }
    excluded = _entitlement_tool_exclusions(
        session_config=session_config,
        tool_registry=_Registry(),
        mcp_scope=None,
    )
    assert "generate_image" not in excluded
    assert "generate_video" not in excluded
