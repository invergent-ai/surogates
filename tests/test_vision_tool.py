"""Tests for the harness-local vision_analyze builtin."""

from __future__ import annotations

import json
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from PIL import Image

from surogates.session.models import Session, SessionLease
from surogates.storage.tenant import session_workspace_key
from surogates.tools.registry import ToolRegistry


def _png(path: Path) -> None:
    Image.new("RGB", (2, 2), (200, 40, 10)).save(path, format="PNG")


def _png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (2, 2), (200, 40, 10)).save(buf, format="PNG")
    return buf.getvalue()


def _fake_response(content: str = "a small red-orange square") -> SimpleNamespace:
    return SimpleNamespace(
        model="surogate",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    model_dump=lambda **_kwargs: {
                        "role": "assistant",
                        "content": content,
                    }
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=7,
            completion_tokens=5,
            total_tokens=12,
        ),
    )


class FakeStorage:
    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        self.objects = objects

    async def read(self, bucket: str, key: str) -> bytes:
        try:
            return self.objects[(bucket, key)]
        except KeyError as exc:
            raise KeyError(f"{bucket}/{key}") from exc


def test_tool_runtime_registers_vision_analyze() -> None:
    from surogates.tools.runtime import ToolRuntime
    from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

    registry = ToolRegistry()

    ToolRuntime(registry).register_builtins()

    assert registry.has("vision_analyze")
    assert TOOL_LOCATIONS["vision_analyze"] == ToolLocation.HARNESS


@pytest.mark.asyncio
async def test_vision_analyze_sends_workspace_image_as_data_url(tmp_path: Path) -> None:
    from surogates.tools.builtin.vision import _vision_analyze_handler

    image_path = tmp_path / "sample.png"
    _png(image_path)
    create = AsyncMock(return_value=_fake_response())
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    result = await _vision_analyze_handler(
        {"image": "sample.png", "question": "What is in this image?"},
        workspace_path=str(tmp_path),
        llm_client=llm_client,
        model="surogate",
    )

    payload = json.loads(result)
    assert payload["analysis"] == "a small red-orange square"
    call_kwargs = create.await_args.kwargs
    assert call_kwargs["model"] == "surogate"
    content = call_kwargs["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_vision_analyze_uses_configured_vision_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from surogates.tools.builtin.vision import _vision_analyze_handler

    monkeypatch.setenv("SUROGATES_CONFIG", str(tmp_path / "missing-config.yaml"))
    monkeypatch.setenv("SUROGATES_LLM_VISION_MODEL", "configured-vision-model")
    image_path = tmp_path / "sample.png"
    _png(image_path)
    create = AsyncMock(return_value=_fake_response())
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    result = await _vision_analyze_handler(
        {"image": "sample.png", "question": "What is in this image?"},
        workspace_path=str(tmp_path),
        llm_client=llm_client,
        model="active-chat-model",
    )

    payload = json.loads(result)
    assert payload["analysis"] == "a small red-orange square"
    assert create.await_args.kwargs["model"] == "configured-vision-model"


@pytest.mark.asyncio
async def test_vision_analyze_reads_workspace_image_from_storage() -> None:
    from surogates.tools.builtin.vision import _vision_analyze_handler

    session_id = UUID("00000000-0000-0000-0000-000000000123")
    image_path = "browser-screenshots/screenshot.png"
    bucket = "agent-bucket"
    storage = FakeStorage(
        {
            (
                bucket,
                session_workspace_key(session_id, image_path),
            ): _png_bytes()
        }
    )
    create = AsyncMock(return_value=_fake_response("storage image"))
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    result = await _vision_analyze_handler(
        {"image": image_path, "question": "What is in this image?"},
        workspace_path="/workspace",
        storage=storage,
        session_id=session_id,
        session_config={"storage_bucket": bucket},
        llm_client=llm_client,
        model="surogate",
    )

    payload = json.loads(result)
    assert payload["analysis"] == "storage image"
    assert payload["source"] == "workspace_file"
    content = create.await_args.kwargs["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_vision_analyze_reads_absolute_workspace_image_from_storage() -> None:
    from surogates.tools.builtin.vision import _vision_analyze_handler

    session_id = UUID("00000000-0000-0000-0000-000000000124")
    relative_path = "browser-screenshots/screenshot.png"
    bucket = "agent-bucket"
    storage = FakeStorage(
        {
            (
                bucket,
                session_workspace_key(session_id, relative_path),
            ): _png_bytes()
        }
    )
    create = AsyncMock(return_value=_fake_response("absolute workspace image"))
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    result = await _vision_analyze_handler(
        {"image": f"/workspace/{relative_path}", "question": "What is in this image?"},
        workspace_path="/workspace",
        storage=storage,
        session_id=session_id,
        session_config={"storage_bucket": bucket},
        llm_client=llm_client,
        model="surogate",
    )

    payload = json.loads(result)
    assert payload["analysis"] == "absolute workspace image"
    assert payload["source"] == "workspace_file"


@pytest.mark.asyncio
async def test_vision_analyze_blocks_workspace_escape(tmp_path: Path) -> None:
    from surogates.tools.builtin.vision import _vision_analyze_handler

    outside = tmp_path.parent / "outside.png"
    _png(outside)
    create = AsyncMock(return_value=_fake_response())
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    result = await _vision_analyze_handler(
        {"image": "../outside.png", "question": "Inspect this"},
        workspace_path=str(tmp_path),
        llm_client=llm_client,
        model="surogate",
    )

    payload = json.loads(result)
    assert "error" in payload
    assert "Path traversal blocked" in payload["error"]
    create.assert_not_called()


@pytest.mark.asyncio
async def test_vision_analyze_blocks_unsafe_remote_url(monkeypatch) -> None:
    from surogates.tools.builtin import vision

    create = AsyncMock(return_value=_fake_response())
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(vision, "is_safe_url", lambda _url: False)

    result = await vision._vision_analyze_handler(
        {"image": "http://169.254.169.254/latest/meta-data/", "question": "Inspect this"},
        llm_client=llm_client,
        model="surogate",
    )

    payload = json.loads(result)
    assert payload == {"error": "Blocked unsafe image URL"}
    create.assert_not_called()


@pytest.mark.asyncio
async def test_execute_single_tool_passes_active_harness_model_and_client(tmp_path: Path) -> None:
    from surogates.harness.tool_exec import execute_single_tool
    from surogates.tools.builtin.vision import register

    image_path = tmp_path / "sample.png"
    _png(image_path)
    registry = ToolRegistry()
    register(registry)
    create = AsyncMock(return_value=_fake_response("vision result"))
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    emitted: list[tuple[UUID, str, dict]] = []

    class Store:
        async def emit_event(self, session_id: UUID, event_type: str, data: dict) -> int:
            emitted.append((session_id, event_type, data))
            return len(emitted)

        async def advance_harness_cursor(self, *_args, **_kwargs) -> None:
            return None

    now = datetime.now(timezone.utc)
    session = Session(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        org_id=UUID("00000000-0000-0000-0000-000000000002"),
        agent_id="agent",
        channel="api",
        status="running",
        model="surogate",
        config={"workspace_path": str(tmp_path)},
        created_at=now,
        updated_at=now,
    )
    lease = SessionLease(
        session_id=session.id,
        owner_id="worker",
        lease_token=UUID("00000000-0000-0000-0000-000000000003"),
        expires_at=now,
    )

    result = await execute_single_tool(
        {
            "id": "call-1",
            "function": {
                "name": "vision_analyze",
                "arguments": json.dumps({"image": "sample.png", "question": "Describe"}),
            },
        },
        session=session,
        lease=lease,
        store=Store(),
        tools=registry,
        tenant=SimpleNamespace(),
        llm_client=llm_client,
        model="surogate",
    )

    assert json.loads(result["content"])["analysis"] == "vision result"
    assert create.await_args.kwargs["model"] == "surogate"
