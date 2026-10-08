"""Session and workspace APIs persist and clean up files within the session prefix."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from types import SimpleNamespace
from io import BytesIO
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException, Response, UploadFile

from surogates.api.routes import workspace as workspace_route
from surogates.api.routes import sessions as sessions_route
from surogates.artifacts.models import ArtifactKind
from surogates.artifacts.store import ArtifactStore
from surogates.config import Settings
from surogates.devices.operations import DeviceOperations
from surogates.tenant.context import TenantContext
from surogates.tools.workspace_io import StorageWorkspaceIO

pytestmark = pytest.mark.asyncio


class _RecordingStorage:
    def __init__(self) -> None:
        self.created_buckets: list[str] = []
        self.deleted_buckets: list[str] = []
        self.deleted_keys: list[tuple[str, str]] = []
        self.keys: dict[str, list[str]] = {}
        self.objects: dict[tuple[str, str], bytes] = {}

    async def create_bucket(self, bucket: str) -> None:
        self.created_buckets.append(bucket)

    async def delete_bucket(self, bucket: str) -> None:
        self.deleted_buckets.append(bucket)

    async def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        keys = set(self.keys.get(bucket, []))
        keys.update(k for b, k in self.objects if b == bucket)
        return sorted(k for k in keys if k.startswith(prefix))

    async def list_entries(self, bucket: str, prefix: str = "") -> list[dict]:
        return [
            {"key": key, "size": len(self.objects.get((bucket, key), b""))}
            for key in await self.list_keys(bucket, prefix)
        ]

    async def delete(self, bucket: str, key: str) -> None:
        self.deleted_keys.append((bucket, key))
        self.objects.pop((bucket, key), None)

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        listed = set(self.keys.get(bucket, []))
        listed.update(k for b, k in self.objects if b == bucket)
        matched = sorted(k for k in listed if k.startswith(prefix))
        for key in matched:
            self.deleted_keys.append((bucket, key))
            self.objects.pop((bucket, key), None)
        if bucket in self.keys:
            self.keys[bucket] = [k for k in self.keys[bucket] if not k.startswith(prefix)]
        return len(matched)

    async def write(self, bucket: str, key: str, data: bytes) -> None:
        self.objects[(bucket, key)] = data

    async def write_text(self, bucket: str, key: str, text: str) -> None:
        self.objects[(bucket, key)] = text.encode("utf-8")

    async def read(self, bucket: str, key: str) -> bytes:
        try:
            return self.objects[(bucket, key)]
        except KeyError:
            raise KeyError(f"{bucket}/{key}") from None

    async def read_text(self, bucket: str, key: str) -> str:
        return (await self.read(bucket, key)).decode("utf-8")

    async def exists(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self.objects

    async def stat(self, bucket: str, key: str) -> dict:
        return {"size": len(await self.read(bucket, key))}

    def resolve_bucket_path(self, bucket: str) -> str:
        return f"/bucket-root/{bucket}"

    def resolve_workspace_path(self, bucket: str, session_id: UUID | str) -> str:
        return f"/bucket-root/{bucket}/{session_id}"


class _Redis:
    def __init__(self) -> None:
        self.zadds: list[tuple[str, dict[str, float]]] = []
        self.published: list[tuple[str, str]] = []

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.zadds.append((key, mapping))

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


class _BrowserPool:
    def __init__(self) -> None:
        self.destroyed_sessions: list[str] = []

    async def destroy_for_session(self, session_id: str) -> None:
        self.destroyed_sessions.append(session_id)


class _BrowserBackend(_BrowserPool):
    pass


class _BrowserRegistry:
    def __init__(self) -> None:
        self.deleted_sessions: list[str] = []

    async def delete(self, session_id: str) -> None:
        self.deleted_sessions.append(session_id)


class _Store:
    def __init__(self, org_id: UUID, agent_id: str = "support-bot") -> None:
        self.org_id = org_id
        self.agent_id = agent_id
        self.created: list[dict] = []
        self.events: list[tuple[UUID, object, dict]] = []
        self.status_updates: list[tuple[UUID, str]] = []
        self.session = SimpleNamespace(
            id=uuid4(),
            org_id=org_id,
            agent_id=agent_id,
            status="active",
            channel="web",
            model="gpt-test",
            config={},
        )

    async def create_session(self, **kwargs):
        self.created.append(kwargs)
        self.session = SimpleNamespace(
            id=kwargs["session_id"],
            org_id=kwargs["org_id"],
            agent_id=kwargs["agent_id"],
            status="active",
            channel=kwargs["channel"],
            model=kwargs["model"],
            config=kwargs["config"],
        )
        return self.session

    async def get_session(self, session_id: UUID):
        self.session.id = session_id
        return self.session

    async def get_session_by_idempotency_key(self, org_id: UUID, key: str):
        return None

    async def emit_event(self, session_id: UUID, event_type, data: dict) -> int:
        self.events.append((session_id, event_type, data))
        return 123

    async def update_session_status(self, session_id: UUID, status: str) -> None:
        self.status_updates.append((session_id, status))

    async def archive_session_tree_and_delete_schedules(
        self,
        session_id: UUID,
        *,
        org_id: UUID,
        agent_id: str,
    ) -> list[SimpleNamespace]:
        self.status_updates.append((session_id, "archived"))
        self.session.status = "archived"
        return [self.session]


def _runtime(agent_id: str = "support-bot", org_id: UUID | None = None):
    """Build the per-request runtime context the routes now depend on.

    ``agent_id`` used to come from process-wide ``settings.agent_id``; it is
    now resolved per request via ``agent_runtime_context_dep`` and passed
    into the route as an ``AgentRuntimeContext``.
    """
    from surogates.runtime import build_agent_runtime_context

    return build_agent_runtime_context(
        {
            "agent_id": agent_id,
            "org_id": str(org_id or uuid4()),
            "project_id": "test-project",
            "enabled": True,
            "version": 1,
            "storage_key_prefix": "",
        }
    )


def _tenant(org_id: UUID, user_id: UUID | None = None) -> TenantContext:
    return TenantContext(
        org_id=org_id,
        user_id=user_id,
        org_config={},
        user_preferences={},
        permissions=frozenset({"sessions:read", "sessions:write"}),
        asset_root="/tmp/assets",
        service_account_id=None if user_id is not None else uuid4(),
    )


def _request(
    store: _Store,
    storage: _RecordingStorage,
    redis: _Redis,
    browser_pool: _BrowserPool | None = None,
    browser_backend: _BrowserBackend | None = None,
    browser_registry: _BrowserRegistry | None = None,
    path: str = "/v1/sessions",
):
    # ``Settings`` is no longer per-tenant — ``agent_id`` is resolved per
    # request via the runtime context, not from process-wide settings.
    settings = Settings()
    settings.storage.bucket = "ops-agent-bucket"
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=settings,
                session_store=store,
                session_factory=None,
                storage=storage,
                redis=redis,
                browser_pool=browser_pool,
                browser_backend=browser_backend,
                browser_registry=browser_registry,
            ),
        ),
    )


async def test_create_web_session_uses_agent_bucket_and_session_path():
    org_id = uuid4()
    user_id = uuid4()
    store = _Store(org_id)
    storage = _RecordingStorage()
    request = _request(store, storage, _Redis())

    response = await sessions_route.create_session(
        sessions_route.CreateSessionRequest(),
        request,
        Response(),
        _tenant(org_id, user_id),
        _runtime(store.agent_id, org_id),
    )

    assert response.id == store.session.id
    assert storage.created_buckets == ["ops-agent-bucket"]
    assert store.session.config["storage_bucket"] == "ops-agent-bucket"
    assert store.session.config["workspace_path"] == (
        f"/bucket-root/ops-agent-bucket/{store.session.id}"
    )


async def test_create_session_drops_client_supplied_sandbox_root():
    org_id = uuid4()
    user_id = uuid4()
    victim_session_id = uuid4()
    store = _Store(org_id)
    storage = _RecordingStorage()
    request = _request(store, storage, _Redis())

    await sessions_route.create_session(
        sessions_route.CreateSessionRequest(
            config={"sandbox_root_session_id": str(victim_session_id)},
        ),
        request,
        Response(),
        _tenant(org_id, user_id),
        _runtime(store.agent_id, org_id),
    )

    assert "sandbox_root_session_id" not in store.session.config
    assert store.session.config["workspace_path"] == (
        f"/bucket-root/ops-agent-bucket/{store.session.id}"
    )


async def test_delete_session_deletes_session_prefix_not_agent_bucket(monkeypatch):
    monkeypatch.setattr(DeviceOperations, "cancel", AsyncMock(return_value=0))
    org_id = uuid4()
    session_id = uuid4()
    store = _Store(org_id)
    store.session = SimpleNamespace(
        id=session_id,
        org_id=org_id,
        agent_id="support-bot",
        status="active",
        channel="web",
        config={"storage_bucket": "ops-agent-bucket"},
    )
    storage = _RecordingStorage()
    # Shared-bucket layout: session prefixes live directly under the
    # agent's storage_key_prefix (empty here, so at the bucket root).
    storage.keys["ops-agent-bucket"] = [
        f"{session_id}/file.txt",
        f"{session_id}/sub/other.txt",
        "other-session/file.txt",
    ]
    request = _request(store, storage, _Redis())
    background_tasks = BackgroundTasks()

    await sessions_route.delete_session(
        session_id, request, background_tasks, _tenant(org_id, uuid4()),
        _runtime("support-bot", org_id),
    )
    # Workspace cleanup runs after the response is sent; in tests we drive
    # the queued task manually so we can assert on its side effects.
    await background_tasks()

    assert storage.deleted_buckets == []
    assert storage.deleted_keys == [
        ("ops-agent-bucket", f"{session_id}/file.txt"),
        ("ops-agent-bucket", f"{session_id}/sub/other.txt"),
    ]


async def test_deleting_a_local_folder_chat_deletes_nothing_in_storage(monkeypatch):
    monkeypatch.setattr(DeviceOperations, "cancel", AsyncMock(return_value=0))
    monkeypatch.setattr(DeviceOperations, "retire", AsyncMock(return_value=None))
    org_id = uuid4()
    session_id = uuid4()
    user_id = uuid4()
    store = _Store(org_id)
    store.session = SimpleNamespace(
        id=session_id,
        org_id=org_id,
        user_id=user_id,
        service_account_id=None,
        agent_id="support-bot",
        status="active",
        channel="web",
        # The storage fields a local-folder chat keeps for create_child_session only.
        config={
            "storage_bucket": "ops-agent-bucket",
            "execution": {"kind": "device", "device_id": str(uuid4())},
            "workspace_path": "/home/me/notes",
        },
    )
    storage = _RecordingStorage()
    storage.keys["ops-agent-bucket"] = [f"{session_id}/file.txt"]
    request = _request(store, storage, _Redis())
    background_tasks = BackgroundTasks()

    await sessions_route.delete_session(
        session_id, request, background_tasks, _tenant(org_id, user_id),
        _runtime("support-bot", org_id),
    )
    await background_tasks()

    assert storage.deleted_keys == []


async def test_delete_session_destroys_browser_sandbox(monkeypatch):
    monkeypatch.setattr(DeviceOperations, "cancel", AsyncMock(return_value=0))
    org_id = uuid4()
    session_id = uuid4()
    store = _Store(org_id)
    store.session = SimpleNamespace(
        id=session_id,
        org_id=org_id,
        agent_id="support-bot",
        status="active",
        channel="web",
        config={"storage_bucket": "ops-agent-bucket"},
    )
    storage = _RecordingStorage()
    browser_pool = _BrowserPool()
    browser_backend = _BrowserBackend()
    browser_registry = _BrowserRegistry()
    request = _request(
        store,
        storage,
        _Redis(),
        browser_pool=browser_pool,
        browser_backend=browser_backend,
        browser_registry=browser_registry,
    )

    background_tasks = BackgroundTasks()
    await sessions_route.delete_session(
        session_id, request, background_tasks, _tenant(org_id, uuid4()),
        _runtime("support-bot", org_id),
    )

    assert browser_pool.destroyed_sessions == [str(session_id)]
    assert browser_backend.destroyed_sessions == [str(session_id)]
    assert browser_registry.deleted_sessions == [str(session_id)]


async def test_workspace_upload_read_tree_and_delete_use_session_prefix():
    org_id = uuid4()
    session_id = uuid4()
    store = _Store(org_id)
    store.session = SimpleNamespace(
        id=session_id,
        org_id=org_id,
        agent_id="support-bot",
        status="active",
        channel="web",
        config={"storage_bucket": "ops-agent-bucket"},
    )
    storage = _RecordingStorage()
    request = _request(store, storage, _Redis())
    tenant = _tenant(org_id, uuid4())

    uploaded = await workspace_route.upload_file(
        session_id,
        request,
        UploadFile(file=BytesIO(b"print('hi')"), filename="app.py"),
        path="src",
        tenant=tenant,
    )

    assert uploaded.path == "src/app.py"
    assert (
        storage.objects[("ops-agent-bucket", f"{session_id}/src/app.py")]
        == b"print('hi')"
    )

    tree = await workspace_route.get_workspace_tree(session_id, request, tenant)
    assert tree.root == "ops-agent-bucket"
    assert tree.entries[0].path == "src"
    assert tree.entries[0].children[0].path == "src/app.py"

    content = await workspace_route.get_workspace_file(
        session_id,
        request,
        path="src/app.py",
        tenant=tenant,
    )
    assert content.content == "print('hi')"

    await workspace_route.delete_file(
        session_id,
        request,
        path="src/app.py",
        tenant=tenant,
    )
    assert storage.deleted_keys[-1] == (
        "ops-agent-bucket",
        f"{session_id}/src/app.py",
    )


async def test_workspace_pdf_files_are_read_as_base64_previews():
    org_id = uuid4()
    session_id = uuid4()
    store = _Store(org_id)
    store.session = SimpleNamespace(
        id=session_id,
        org_id=org_id,
        agent_id="support-bot",
        status="active",
        config={"storage_bucket": "ops-agent-bucket"},
    )
    storage = _RecordingStorage()
    storage.objects[("ops-agent-bucket", f"{session_id}/docs/report.pdf")] = (
        b"%PDF-1.4\n"
    )
    request = _request(store, storage, _Redis())
    tenant = _tenant(org_id, uuid4())

    content = await workspace_route.get_workspace_file(
        session_id,
        request,
        path="docs/report.pdf",
        tenant=tenant,
    )

    assert content.path == "docs/report.pdf"
    assert content.content == "JVBERi0xLjQK"
    assert content.mime_type == "application/pdf"
    assert content.encoding == "base64"
    assert content.truncated is False


async def test_artifact_store_writes_under_session_prefix():
    storage = _RecordingStorage()
    session_id = uuid4()
    store = ArtifactStore(StorageWorkspaceIO(storage, bucket="ops-agent-bucket", prefix=f"{session_id}/"), session_id=session_id)

    meta = await store.create(
        name="notes",
        kind=ArtifactKind.MARKDOWN,
        spec={"content": "# Notes"},
    )

    assert (
        "ops-agent-bucket",
        f"{session_id}/_artifacts/index.json",
    ) in storage.objects
    assert (
        "ops-agent-bucket",
        f"{session_id}/_artifacts/{meta.artifact_id}/v1.json",
    ) in storage.objects


class _SlowStorage(_RecordingStorage):
    """Object storage that takes its time, as a slow bucket does."""

    async def read(self, bucket: str, key: str) -> bytes:
        await asyncio.sleep(0.3)
        return await super().read(bucket, key)

    async def write(self, bucket: str, key: str, data: bytes) -> None:
        await asyncio.sleep(0.3)
        await super().write(bucket, key, data)


def _slow_chat():
    org_id = uuid4()
    session_id = uuid4()
    store = _Store(org_id)
    store.session = SimpleNamespace(
        id=session_id, org_id=org_id, agent_id="support-bot", status="active", channel="web",
        config={"storage_bucket": "ops-agent-bucket"},
    )
    storage = _SlowStorage()
    return session_id, storage, _request(store, storage, _Redis()), _tenant(org_id, uuid4())


async def test_a_cloud_chats_slow_storage_is_read_as_before(monkeypatch):
    # The deadlines are a computer's: object storage keeps no request to join.
    monkeypatch.setattr(workspace_route, "READ_WITHIN_S", 0.05)
    session_id, storage, request, tenant = _slow_chat()
    storage.objects[("ops-agent-bucket", f"{session_id}/a.txt")] = b"slow"
    content = await workspace_route.get_workspace_file(session_id, request, path="a.txt", tenant=tenant)
    assert content.content == "slow"


async def test_a_cloud_chats_slow_storage_is_written_as_before(monkeypatch):
    monkeypatch.setattr(workspace_route, "CHANGE_WITHIN_S", 0.05)
    session_id, storage, request, tenant = _slow_chat()
    uploaded = await workspace_route.upload_file(
        session_id, request, UploadFile(file=BytesIO(b"slow"), filename="a.txt"), path="", tenant=tenant,
    )
    assert uploaded.path == "a.txt"
    assert storage.objects[("ops-agent-bucket", f"{session_id}/a.txt")] == b"slow"


class _ReadOnlyStorage(_RecordingStorage):
    """Object storage that answers a read alone, as the file panel's open asks it: one GET, no HEAD."""

    async def stat(self, bucket: str, key: str) -> dict:
        raise AssertionError("a cloud chat's open reads the object directly")

    async def read(self, bucket: str, key: str) -> bytes:
        if "\x00" in key:
            raise ValueError("embedded null byte")
        return await super().read(bucket, key)


async def test_a_cloud_chats_file_is_opened_with_one_read_as_before():
    org_id = uuid4()
    session_id = uuid4()
    store = _Store(org_id)
    store.session = SimpleNamespace(
        id=session_id, org_id=org_id, agent_id="support-bot", status="active", channel="web",
        config={"storage_bucket": "ops-agent-bucket"},
    )
    storage = _ReadOnlyStorage()
    storage.objects[("ops-agent-bucket", f"{session_id}/a.txt")] = b"alpha"
    storage.objects[("ops-agent-bucket", f"{session_id}/dot.png")] = b"\x89PNG"
    request = _request(store, storage, _Redis())
    tenant = _tenant(org_id, uuid4())

    text = await workspace_route.get_workspace_file(session_id, request, path="a.txt", tenant=tenant)
    assert (text.content, text.size, text.truncated) == ("alpha", 5, False)
    image = await workspace_route.get_workspace_file(session_id, request, path="dot.png", tenant=tenant)
    assert (image.content, image.size) == ("iVBORw==", 4)
    with pytest.raises(HTTPException) as missing:
        await workspace_route.get_workspace_file(session_id, request, path="missing.txt", tenant=tenant)
    assert (missing.value.status_code, missing.value.detail) == (404, "File not found: missing.txt")
    # A path storage cannot take is the user's error, not the server's.
    with pytest.raises(HTTPException) as bad:
        await workspace_route.get_workspace_file(session_id, request, path="a\x00b.txt", tenant=tenant)
    assert bad.value.status_code == 400


async def test_an_uploads_change_is_named_off_the_event_loop(monkeypatch):
    # Up to 50 MB hashed: the api's other requests share its loop.
    named: list[bool] = []
    real = workspace_route._change

    def change(*parts):
        named.append(threading.current_thread() is threading.main_thread())
        return real(*parts)

    monkeypatch.setattr(workspace_route, "_change", change)
    session_id, storage, request, tenant = _slow_chat()
    await workspace_route.upload_file(
        session_id, request, UploadFile(file=BytesIO(b"data"), filename="a.txt"), path="", tenant=tenant,
    )
    assert named == [False]
    # Each part length-prefixed, as before.
    framed = b"".join(len(part).to_bytes(8, "big") + part for part in (b"upload", b"a.txt", b"data"))
    assert real("upload", "a.txt", b"data") == hashlib.sha256(framed).hexdigest()


async def test_a_cloud_chat_keeps_its_artifacts_folder_hidden_and_closed():
    session_id, storage, request, tenant = _slow_chat()
    storage.objects[("ops-agent-bucket", f"{session_id}/_artifacts/index.json")] = b"[]"
    storage.objects[("ops-agent-bucket", f"{session_id}/notes.md")] = b"n"
    tree = await workspace_route.get_workspace_tree(session_id, request, tenant=tenant)
    assert [entry.name for entry in tree.entries] == ["notes.md"]
    for opening in (workspace_route.get_workspace_file, workspace_route.download_file):
        with pytest.raises(HTTPException) as refused:
            await opening(session_id, request, path="_artifacts/index.json", tenant=tenant)
        assert refused.value.status_code == 403


async def test_a_cloud_chat_without_its_bucket_still_refuses_its_reserved_paths_first():
    org_id, session_id = uuid4(), uuid4()
    store = _Store(org_id)
    store.session = SimpleNamespace(
        id=session_id, org_id=org_id, agent_id="support-bot", status="active", channel="web", config={},
    )
    request, tenant = _request(store, _RecordingStorage(), _Redis()), _tenant(org_id, uuid4())
    # Refused as they always were, before its storage is looked for.
    for route, path in (
        (workspace_route.get_workspace_file, "_artifacts/index.json"),
        (workspace_route.download_file, "_artifacts/index.json"),
        (workspace_route.delete_file, "_artifacts/index.json"),
        (workspace_route.delete_file, "_history/x.json"),
    ):
        with pytest.raises(HTTPException) as refused:
            await route(session_id, request, path=path, tenant=tenant)
        assert refused.value.status_code == 403, (route.__name__, path, refused.value.detail)
