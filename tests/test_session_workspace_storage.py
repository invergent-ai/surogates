"""Session workspace storage shape tests."""

from __future__ import annotations

from types import SimpleNamespace
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from fastapi import UploadFile

from surogates.api.routes import workspace as workspace_route
from surogates.jobs.cleanup_sessions import cleanup_orphaned_session_prefixes
from surogates.api.routes import prompts as prompts_route
from surogates.api.routes import sessions as sessions_route
from surogates.artifacts.models import ArtifactKind
from surogates.artifacts.store import ArtifactStore
from surogates.config import Settings
from surogates.tenant.context import TenantContext

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

    async def delete(self, bucket: str, key: str) -> None:
        self.deleted_keys.append((bucket, key))
        self.objects.pop((bucket, key), None)

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
        return f"/bucket-root/{bucket}/sessions/{session_id}"


class _Redis:
    def __init__(self) -> None:
        self.zadds: list[tuple[str, dict[str, float]]] = []
        self.published: list[tuple[str, str]] = []

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.zadds.append((key, mapping))

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


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


def _request(store: _Store, storage: _RecordingStorage, redis: _Redis):
    settings = Settings(agent_id=store.agent_id)
    settings.llm.model = "gpt-test"
    settings.storage.bucket = "ops-agent-bucket"
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=settings,
                session_store=store,
                storage=storage,
                redis=redis,
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
        _tenant(org_id, user_id),
    )

    assert response.id == store.session.id
    assert storage.created_buckets == ["ops-agent-bucket"]
    assert store.session.config["storage_bucket"] == "ops-agent-bucket"
    assert store.session.config["workspace_path"] == (
        f"/bucket-root/ops-agent-bucket/sessions/{store.session.id}"
    )


async def test_submit_prompt_uses_agent_bucket_and_session_path():
    org_id = uuid4()
    service_account_id = uuid4()
    store = _Store(org_id)
    storage = _RecordingStorage()
    redis = _Redis()
    request = _request(store, storage, redis)
    tenant = _tenant(org_id)

    accepted = await prompts_route._submit_one(
        prompts_route.PromptRequest(prompt="run this"),
        request=request,
        tenant=tenant,
        service_account_id=service_account_id,
        store=store,
    )

    assert accepted.session_id == store.session.id
    assert storage.created_buckets == ["ops-agent-bucket"]
    assert store.session.config["storage_bucket"] == "ops-agent-bucket"
    assert store.session.config["workspace_path"] == (
        f"/bucket-root/ops-agent-bucket/sessions/{store.session.id}"
    )


async def test_delete_session_deletes_session_prefix_not_agent_bucket():
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
    storage.keys["ops-agent-bucket"] = [
        f"sessions/{session_id}/file.txt",
        f"sessions/{session_id}/sub/other.txt",
        "sessions/other-session/file.txt",
    ]
    request = _request(store, storage, _Redis())

    await sessions_route.delete_session(session_id, request, _tenant(org_id, uuid4()))

    assert storage.deleted_buckets == []
    assert storage.deleted_keys == [
        ("ops-agent-bucket", f"sessions/{session_id}/file.txt"),
        ("ops-agent-bucket", f"sessions/{session_id}/sub/other.txt"),
    ]


async def test_workspace_upload_read_tree_and_delete_use_session_prefix():
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
    assert storage.objects[
        ("ops-agent-bucket", f"sessions/{session_id}/src/app.py")
    ] == b"print('hi')"

    tree = await workspace_route.get_workspace_tree(session_id, request, tenant)
    assert tree.root == "ops-agent-bucket"
    assert tree.entries[0].path == "src"
    assert tree.entries[0].children[0].path == "src/app.py"

    content = await workspace_route.get_workspace_file(
        session_id, request, path="src/app.py", tenant=tenant,
    )
    assert content.content == "print('hi')"

    await workspace_route.delete_file(
        session_id, request, path="src/app.py", tenant=tenant,
    )
    assert storage.deleted_keys[-1] == (
        "ops-agent-bucket", f"sessions/{session_id}/src/app.py",
    )


async def test_artifact_store_writes_under_session_prefix():
    storage = _RecordingStorage()
    session_id = uuid4()
    store = ArtifactStore(
        storage,
        session_id=session_id,
        bucket="ops-agent-bucket",
        key_prefix=f"sessions/{session_id}/",
    )

    meta = await store.create(
        name="notes",
        kind=ArtifactKind.MARKDOWN,
        spec={"content": "# Notes"},
    )

    assert (
        "ops-agent-bucket",
        f"sessions/{session_id}/_artifacts/index.json",
    ) in storage.objects
    assert (
        "ops-agent-bucket",
        f"sessions/{session_id}/_artifacts/{meta.artifact_id}/v1.json",
    ) in storage.objects


async def test_cleanup_deletes_orphaned_session_prefixes_only():
    active_id = uuid4()
    orphan_id = uuid4()
    storage = _RecordingStorage()
    storage.keys["ops-agent-bucket"] = [
        f"sessions/{active_id}/keep.txt",
        f"sessions/{orphan_id}/delete.txt",
        f"sessions/{orphan_id}/sub/delete.txt",
    ]

    deleted = await cleanup_orphaned_session_prefixes(
        storage,
        bucket="ops-agent-bucket",
        active_session_ids={str(active_id)},
        dry_run=False,
    )

    assert deleted == 1
    assert storage.deleted_buckets == []
    assert storage.deleted_keys == [
        ("ops-agent-bucket", f"sessions/{orphan_id}/delete.txt"),
        ("ops-agent-bucket", f"sessions/{orphan_id}/sub/delete.txt"),
    ]
