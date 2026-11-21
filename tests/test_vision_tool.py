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
async def test_vision_analyze_sends_no_temperature_and_turns_thinking_off(
    tmp_path: Path,
) -> None:
    """Reading an image is perception: the calling agent does the reasoning,
    so the vision call skips thinking and leaves sampling to the model."""
    from surogates.tools.builtin.vision import _vision_analyze_handler

    _png(tmp_path / "sample.png")
    create = AsyncMock(return_value=_fake_response())
    llm_client = SimpleNamespace(
        base_url="https://ops.example/proxy/services/_model/m-1/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )

    await _vision_analyze_handler(
        {"image": "sample.png"},
        workspace_path=str(tmp_path),
        llm_client=llm_client,
        model="surogate3.7",
    )

    call_kwargs = create.await_args.kwargs
    assert "temperature" not in call_kwargs
    assert call_kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
    }


@pytest.mark.asyncio
async def test_vision_analyze_retries_without_a_rejected_thinking_switch(
    tmp_path: Path,
) -> None:
    from surogates.tools.builtin.vision import _vision_analyze_handler

    _png(tmp_path / "sample.png")
    create = AsyncMock(side_effect=[
        TypeError("unexpected keyword argument 'chat_template_kwargs'"),
        _fake_response("described"),
    ])
    llm_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )

    result = await _vision_analyze_handler(
        {"image": "sample.png"},
        workspace_path=str(tmp_path),
        llm_client=llm_client,
        model="surogate3.7",
    )

    assert json.loads(result)["analysis"] == "described"
    assert "extra_body" not in create.await_args_list[1].kwargs


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


@pytest.mark.asyncio
async def test_a_local_folders_image_is_read_no_further_than_the_cap_whatever_its_stat_said(
    tmp_path: Path, monkeypatch,
) -> None:
    from surogates.devices.workspace import DeviceWorkspaceIO
    from surogates.tools.builtin import vision
    from surogates.tools.workspace_io import LocalWorkspaceIO
    from tests.fake_laptop import InProcessRunner

    class Grown(InProcessRunner):
        """The file grew after its stat: the stat says one byte."""

        asked: list = []

        async def run(self, kind, args, payload=None):
            outcome = await super().run(kind, args, payload)
            if kind == "stat" and outcome.get("ok"):
                outcome["ok"]["size"] = 1
            if kind == "read":
                self.asked.append(args["max_bytes"])
            return outcome

    folder = tmp_path.resolve()
    _png(folder / "big.png")
    monkeypatch.setattr(vision, "_MAX_IMAGE_BYTES", 10)
    runner = Grown(LocalWorkspaceIO(workspace_path=str(folder)))
    create = AsyncMock(return_value=_fake_response())
    payload = json.loads(await vision._vision_analyze_handler(
        {"image": "big.png"},
        workspace_io=DeviceWorkspaceIO(runner, root=str(folder)),
        llm_client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        model="surogate",
    ))
    assert payload["error"].startswith("Image file is too large:"), payload
    assert runner.asked == [11]
    create.assert_not_called()


# ── /workspace paths on local storage ────────────────────────────────────

_HOST_ROOT = "/srv/stack/storage/agent/sessions/00000000-0000-0000-0000-000000000125"

_REFUSED_ANYWHERE = [
    "/tmp/x.png",
    "/tmp/preview/page-1.png",
    "/etc/passwd",
    "/workspace",
    "/workspace/",
    "/workspace/.",
    "/workspace/../etc/passwd",
    "/workspace/../../etc/passwd",
    "/workspace/a/../../etc/passwd",
    "/workspace/a/../b.png",
    "/workspace/../workspace/a.png",
    "/workspace//..//etc/passwd",
    "/workspace/./../etc/passwd",
    "/workspace/%2e%2e/etc/passwd",
    "/workspace/%2E%2E/etc/passwd",
    "/workspace/.%2e/etc/passwd",
    "/workspace/%2e./etc/passwd",
    "/workspace/%252e%252e/etc/passwd",
    "/workspace/%25252e%25252e/etc/passwd",
    "/workspace/..%2fetc%2fpasswd",
    "/workspace/..%2Fetc/passwd",
    "/workspace/a%2f..%2f..%2fetc%2fpasswd",
    "/workspace/..\\etc\\passwd",
    "/workspace/..%5cetc%5cpasswd",
    "/workspace-evil/x.png",
    "/workspace2/x.png",
    "/workspace.png",
    "//workspace/a.png",
    "..",
    "../x.png",
    "a/../../x.png",
    "%2e%2e/x.png",
    "..%2fx.png",
]


@pytest.mark.parametrize("workspace_path", [_HOST_ROOT, _HOST_ROOT + "/", "/workspace"])
@pytest.mark.parametrize(
    ("image_ref", "key"),
    [
        ("/workspace/a.png", "a.png"),
        ("/workspace/scratch/page-1.jpg", "scratch/page-1.jpg"),
        ("/workspace/.check/page-1.png", ".check/page-1.png"),
        ("/workspace//a.png", "a.png"),
        ("/workspace/./a.png", "a.png"),
        ("/workspace/a..b.png", "a..b.png"),
        ("/workspace/100%25.png", "100%25.png"),
        ("a.png", "a.png"),
        ("scratch/page-1.jpg", "scratch/page-1.jpg"),
    ],
)
def test_storage_path_reads_the_sandbox_path_as_the_workspace(
    workspace_path: str, image_ref: str, key: str,
) -> None:
    from surogates.tools.builtin.vision import _validate_workspace_storage_path

    assert _validate_workspace_storage_path(image_ref, workspace_path=workspace_path) == key


def test_storage_path_accepts_the_workspace_own_host_path() -> None:
    from surogates.tools.builtin.vision import _validate_workspace_storage_path

    ref = f"{_HOST_ROOT}/scratch/a.png"
    assert _validate_workspace_storage_path(ref, workspace_path=_HOST_ROOT) == "scratch/a.png"


@pytest.mark.parametrize("workspace_path", [_HOST_ROOT, "/workspace"])
@pytest.mark.parametrize("image_ref", _REFUSED_ANYWHERE)
def test_storage_path_refuses_what_is_not_in_the_workspace(
    workspace_path: str, image_ref: str,
) -> None:
    from surogates.tools.builtin.vision import _validate_workspace_storage_path
    from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError

    with pytest.raises(WorkspaceSandboxError, match="Path traversal blocked"):
        _validate_workspace_storage_path(image_ref, workspace_path=workspace_path)


@pytest.mark.parametrize(
    "image_ref",
    [
        f"{_HOST_ROOT}-evil/x.png",
        f"{_HOST_ROOT}/../other-session/x.png",
        f"{_HOST_ROOT}/a/../../other-session/x.png",
        "/srv/stack/storage/agent/sessions/other-session/x.png",
        "/srv/stack/storage/agent/secret.png",
    ],
)
def test_storage_path_refuses_host_paths_out_of_the_workspace(image_ref: str) -> None:
    from surogates.tools.builtin.vision import _validate_workspace_storage_path
    from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError

    with pytest.raises(WorkspaceSandboxError, match="Path traversal blocked"):
        _validate_workspace_storage_path(image_ref, workspace_path=_HOST_ROOT)


def test_storage_path_refusal_does_not_show_the_host_path() -> None:
    from surogates.tools.builtin.vision import _validate_workspace_storage_path
    from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError

    with pytest.raises(WorkspaceSandboxError) as refused:
        _validate_workspace_storage_path("/workspace/a/../b.png", workspace_path=_HOST_ROOT)
    assert "/workspace/a/../b.png" in str(refused.value)
    assert _HOST_ROOT not in str(refused.value)


def test_storage_path_refuses_a_key_encoded_past_the_decode_limit() -> None:
    from surogates.tools.builtin.vision import _validate_workspace_storage_path
    from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError

    ref = "/workspace/%" + "25" * 200_000 + "2e%2e/x.png"
    with pytest.raises(WorkspaceSandboxError):
        _validate_workspace_storage_path(ref, workspace_path=_HOST_ROOT)


def _local_session(tmp_path: Path) -> tuple[Path, dict]:
    """A workspace on real local storage at the path its keys name, and its tool kwargs."""
    from surogates.storage.backend import LocalBackend

    session_id = UUID("00000000-0000-0000-0000-000000000126")
    bucket = "agent-bucket"
    base = tmp_path / "storage"
    workspace = base / bucket / session_workspace_key(session_id, "").rstrip("/")
    workspace.mkdir(parents=True)
    return workspace, {
        "workspace_path": str(workspace),
        "storage": LocalBackend(str(base)),
        "session_id": session_id,
        "session_config": {"storage_bucket": bucket},
        "model": "surogate",
    }


def _client(create: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.mark.asyncio
async def test_vision_analyze_reads_a_sandbox_path_on_local_storage(tmp_path: Path) -> None:
    from surogates.tools.builtin.vision import _vision_analyze_handler

    workspace, kwargs = _local_session(tmp_path)
    (workspace / "scratch").mkdir()
    _png(workspace / "scratch" / "page-1.png")
    create = AsyncMock(return_value=_fake_response("the page"))

    result = await _vision_analyze_handler(
        {"image": "/workspace/scratch/page-1.png"}, llm_client=_client(create), **kwargs,
    )

    payload = json.loads(result)
    assert payload["analysis"] == "the page"
    assert payload["source"] == "workspace_file"
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_file_on_an_image_reads_a_sandbox_path_on_local_storage(
    tmp_path: Path,
) -> None:
    from surogates.harness.image_read import handle_image_read
    from surogates.tools.builtin.vision import _vision_analyze_handler

    workspace, kwargs = _local_session(tmp_path)
    _png(workspace / "slide-1.png")
    create = AsyncMock(return_value=_fake_response("the slide"))
    kwargs["llm_client"] = _client(create)

    async def dispatch(_name: str, arguments: dict, **call_kwargs) -> str:
        return await _vision_analyze_handler(arguments, **call_kwargs)

    result = await handle_image_read("/workspace/slide-1.png", {}, dispatch, kwargs)

    assert "the slide" in json.loads(result)["content"]
    create.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", ["file", "folder", "chain"])
async def test_vision_analyze_refuses_a_link_out_of_the_workspace(
    tmp_path: Path, linked: str,
) -> None:
    from surogates.tools.builtin.vision import _vision_analyze_handler

    workspace, kwargs = _local_session(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _png(outside / "secret.png")
    if linked == "file":
        (workspace / "x.png").symlink_to(outside / "secret.png")
        ref = "/workspace/x.png"
    elif linked == "folder":
        (workspace / "dir").symlink_to(outside)
        ref = "/workspace/dir/secret.png"
    else:
        (workspace / "hop.png").symlink_to(outside / "secret.png")
        (workspace / "x.png").symlink_to(workspace / "hop.png")
        ref = "/workspace/x.png"
    create = AsyncMock(return_value=_fake_response("leaked"))

    result = await _vision_analyze_handler({"image": ref}, llm_client=_client(create), **kwargs)

    assert "not found" in json.loads(result)["error"]
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "image_ref",
    ["/tmp/x.png", "/workspace/../secret.png", "/workspace/%2e%2e/secret.png", "/workspace-evil/x.png"],
)
async def test_vision_analyze_refuses_paths_out_of_a_local_workspace(
    tmp_path: Path, image_ref: str,
) -> None:
    from surogates.tools.builtin.vision import _vision_analyze_handler

    _workspace, kwargs = _local_session(tmp_path)
    create = AsyncMock(return_value=_fake_response("leaked"))

    result = await _vision_analyze_handler(
        {"image": image_ref}, llm_client=_client(create), **kwargs,
    )

    assert "Path traversal blocked" in json.loads(result)["error"]
    create.assert_not_awaited()


def test_storage_path_caps_a_long_path_of_many_segments() -> None:
    from surogates.tools.builtin.vision import (
        _MAX_IMAGE_PATH_CHARS,
        _validate_workspace_storage_path,
    )
    from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError

    head = "/workspace/"
    at_limit = head + "a" * (_MAX_IMAGE_PATH_CHARS - len(head))
    assert _validate_workspace_storage_path(at_limit, workspace_path=_HOST_ROOT) == at_limit[len(head):]

    with pytest.raises(WorkspaceSandboxError, match="too long") as refused:
        _validate_workspace_storage_path(head + "a/" * 20_000 + "x.png", workspace_path=_HOST_ROOT)
    assert "a/a/" not in str(refused.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("image_ref", ["/workspace/a\0.png", "a\0.png"])
async def test_vision_analyze_refuses_a_nul_without_naming_the_key(
    tmp_path: Path, image_ref: str,
) -> None:
    from surogates.tools.builtin.vision import _vision_analyze_handler
    from surogates.tools.utils.workspace_sandbox import NUL_REFUSED

    _workspace, kwargs = _local_session(tmp_path)
    create = AsyncMock(return_value=_fake_response("leaked"))

    result = await _vision_analyze_handler(
        {"image": image_ref}, llm_client=_client(create), **kwargs,
    )

    assert json.loads(result)["error"] == NUL_REFUSED
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_cache_key_is_one_for_the_sandbox_and_relative_path(tmp_path: Path) -> None:
    from surogates.harness.image_read import _build_key

    workspace, kwargs = _local_session(tmp_path)
    _png(workspace / "a.png")

    key = await _build_key("/workspace/a.png", kwargs)

    assert key is not None
    assert key == await _build_key("a.png", kwargs)
    assert await _build_key("/workspace/../a.png", kwargs) is None
