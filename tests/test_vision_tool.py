"""The vision tool reads workspace images, routes model requests and rejects unsafe paths."""

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
async def test_vision_analyze_prefers_vision_client_over_primary(
    tmp_path: Path,
) -> None:
    from surogates.tools.builtin.vision import _vision_analyze_handler

    image_path = tmp_path / "sample.png"
    _png(image_path)
    primary_create = AsyncMock(return_value=_fake_response("primary"))
    primary_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=primary_create)),
    )
    vision_create = AsyncMock(return_value=_fake_response("vision-aux"))
    vision_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=vision_create)),
    )

    result = await _vision_analyze_handler(
        {"image": "sample.png", "question": "What is in this image?"},
        workspace_path=str(tmp_path),
        llm_client=primary_client,
        model="active-chat-model",
        vision_llm_client=vision_client,
        vision_model="configured-vision-model",
    )

    payload = json.loads(result)
    assert payload["analysis"] == "vision-aux"
    assert vision_create.await_args.kwargs["model"] == "configured-vision-model"
    primary_create.assert_not_called()


@pytest.mark.asyncio
async def test_vision_analyze_falls_back_to_primary_when_no_vision_client(
    tmp_path: Path,
) -> None:
    from surogates.tools.builtin.vision import _vision_analyze_handler

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
    assert create.await_args.kwargs["model"] == "active-chat-model"


@pytest.mark.asyncio
async def test_vision_analyze_errors_when_nothing_configured(
    tmp_path: Path,
) -> None:
    from surogates.tools.builtin.vision import _vision_analyze_handler

    image_path = tmp_path / "sample.png"
    _png(image_path)

    result = await _vision_analyze_handler(
        {"image": "sample.png", "question": "What is in this image?"},
        workspace_path=str(tmp_path),
    )

    payload = json.loads(result)
    assert "error" in payload
    assert "not available" in payload["error"]


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


@pytest.mark.asyncio
async def test_vision_analyze_reads_a_local_folders_image_through_its_tool_call(tmp_path: Path) -> None:
    from surogates.devices.workspace import DeviceWorkspaceIO
    from surogates.tools.builtin.vision import _vision_analyze_handler
    from surogates.tools.workspace_io import LocalWorkspaceIO
    from tests.fake_laptop import InProcessRunner

    folder = tmp_path.resolve()
    (folder / "shots").mkdir()
    _png(folder / "shots" / "a.png")
    files = DeviceWorkspaceIO(InProcessRunner(LocalWorkspaceIO(workspace_path=str(folder))), root=str(folder))
    create = AsyncMock(return_value=_fake_response())
    kwargs = {
        "workspace_io": files,
        # The cloud's storage holds nothing of a local folder's.
        "storage": FakeStorage({}),
        "session_id": UUID(int=1),
        "session_config": {"storage_bucket": "agent-bucket", "workspace_path": str(folder)},
        "llm_client": SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        "model": "surogate",
    }

    for image in ("shots/a.png", f"{folder}/shots/a.png"):
        payload = json.loads(await _vision_analyze_handler({"image": image}, **kwargs))
        assert payload["analysis"] == "a small red-orange square", payload
    assert create.await_args.kwargs["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    missing = json.loads(await _vision_analyze_handler({"image": "shots/none.png"}, **kwargs))
    assert missing["error"] == "Image file not found: shots/none.png"
    outside = json.loads(await _vision_analyze_handler({"image": "../elsewhere.png"}, **kwargs))
    assert "error" in outside


@pytest.mark.asyncio
async def test_a_local_folders_image_over_the_cap_is_refused_before_it_is_read(tmp_path: Path, monkeypatch) -> None:
    from surogates.devices.workspace import DeviceWorkspaceIO
    from surogates.tools.builtin import vision
    from surogates.tools.workspace_io import LocalWorkspaceIO
    from tests.fake_laptop import InProcessRunner

    folder = tmp_path.resolve()
    _png(folder / "big.png")
    monkeypatch.setattr(vision, "_MAX_IMAGE_BYTES", 10)
    runner = InProcessRunner(LocalWorkspaceIO(workspace_path=str(folder)))
    create = AsyncMock(return_value=_fake_response())
    payload = json.loads(await vision._vision_analyze_handler(
        {"image": "big.png"},
        workspace_io=DeviceWorkspaceIO(runner, root=str(folder)),
        llm_client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        model="surogate",
    ))
    assert payload["error"].startswith("Image file is too large:")
    assert runner.kinds == ["resolve", "stat"]
    create.assert_not_called()


@pytest.mark.asyncio
async def test_a_local_folders_image_its_computer_will_not_read_is_said_in_its_words(tmp_path: Path) -> None:
    from surogates.devices.workspace import DeviceWorkspaceIO
    from surogates.tools.builtin.vision import _vision_analyze_handler
    from surogates.tools.workspace_io import LocalWorkspaceIO
    from tests.fake_laptop import InProcessRunner

    class Revoked(InProcessRunner):
        async def run(self, kind, args, payload=None):
            if kind == "read":
                return {"error": {"type": "revoked", "message": "Local access to this computer was revoked"}}
            return await super().run(kind, args, payload)

    folder = tmp_path.resolve()
    _png(folder / "a.png")
    create = AsyncMock(return_value=_fake_response())
    payload = json.loads(await _vision_analyze_handler(
        {"image": "a.png"},
        workspace_io=DeviceWorkspaceIO(Revoked(LocalWorkspaceIO(workspace_path=str(folder))), root=str(folder)),
        llm_client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        model="surogate",
    ))
    assert payload == {"error": "Could not read image a.png: Local access to this computer was revoked"}
    create.assert_not_called()
