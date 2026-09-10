from __future__ import annotations

from types import SimpleNamespace

import pytest

from surogates.session.attachment_ingest import ingest_attachment_bytes, workspace_root_id


class _Storage:
    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = objects or {}
        self.writes: list[tuple[str, str, bytes]] = []
        self.deleted_prefixes: list[tuple[str, str]] = []

    async def write(self, bucket: str, key: str, data: bytes) -> None:
        self.objects[key] = data
        self.writes.append((bucket, key, data))

    async def stat(self, bucket: str, key: str) -> dict:
        if key not in self.objects:
            raise KeyError(key)
        return {"size": len(self.objects[key])}

    async def read(self, bucket: str, key: str) -> bytes:
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]

    async def list_keys(self, bucket: str, prefix: str) -> list[str]:
        return [key for key in self.objects if key.startswith(prefix)]

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        self.deleted_prefixes.append((bucket, prefix))
        keys = [key for key in self.objects if key.startswith(prefix)]
        for key in keys:
            del self.objects[key]
        return len(keys)


def _session(boundary: str = "slack:c:G1"):
    return SimpleNamespace(
        id="session-1",
        channel="slack",
        config={
            "storage_bucket": "agent-bucket",
            "storage_key_prefix": "project/agent",
            "memory_boundary": boundary,
            "workspace_boundary": boundary,
        },
    )


@pytest.mark.asyncio
async def test_attachment_ingest_writes_to_boundary_workspace():
    session = _session()
    storage = _Storage()

    await ingest_attachment_bytes(
        storage,
        session=session,
        root_id=workspace_root_id(session),
        bucket="agent-bucket",
        path="uploads/report.pdf",
        filename="report.pdf",
        mime_type="application/pdf",
        data=b"%PDF",
    )

    assert storage.writes == [
        (
            "agent-bucket",
            "project/agent/boundaries/slack:c:G1/workspace/uploads/report.pdf",
            b"%PDF",
        )
    ]
