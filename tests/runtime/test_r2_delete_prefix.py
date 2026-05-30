"""Tests for delete_memory_prefix.

Plan 7 / Task 5.  R2 memory prefix delete -- drops every object
whose key starts with the agent's storage_key_prefix so a
delete_agent cascade leaves no orphan memory on R2.

Uses the same _FakeBackend pattern Plan 4 established for the
memory_io tests.
"""

from __future__ import annotations

import pytest


class _FakeBackend:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.delete_calls: list[tuple[str, str]] = []
        self.list_calls: list[tuple[str, str]] = []

    async def write(self, bucket, key, data):
        self.objects[f"{bucket}/{key}"] = data

    async def delete(self, bucket, key):
        self.delete_calls.append((bucket, key))
        self.objects.pop(f"{bucket}/{key}", None)

    async def list(self, bucket, prefix):
        self.list_calls.append((bucket, prefix))
        return [
            k.split("/", 1)[1] for k in self.objects
            if k.startswith(f"{bucket}/{prefix}")
        ]


@pytest.mark.asyncio
async def test_delete_prefix_drops_matching_keys():
    from surogates.runtime.memory_io import delete_memory_prefix

    backend = _FakeBackend()
    await backend.write("memory-bk", "p-1/a-1/users/u-1/memory.json", b"{}")
    await backend.write("memory-bk", "p-1/a-1/users/u-2/memory.json", b"{}")
    await backend.write("memory-bk", "p-1/a-2/users/u-3/memory.json", b"{}")

    deleted = await delete_memory_prefix(
        backend, bucket="memory-bk", prefix="p-1/a-1/",
    )

    assert deleted == 2
    remaining = sorted(backend.objects.keys())
    assert remaining == ["memory-bk/p-1/a-2/users/u-3/memory.json"]


@pytest.mark.asyncio
async def test_delete_prefix_is_idempotent():
    """A second call (after the first emptied the prefix) must
    not raise and returns 0 -- delete_agent retry must work."""
    from surogates.runtime.memory_io import delete_memory_prefix

    backend = _FakeBackend()
    first = await delete_memory_prefix(
        backend, bucket="memory-bk", prefix="p-1/a-1/",
    )
    second = await delete_memory_prefix(
        backend, bucket="memory-bk", prefix="p-1/a-1/",
    )
    assert first == 0
    assert second == 0


@pytest.mark.asyncio
async def test_delete_prefix_does_not_touch_other_agents():
    """A delete for agent a-1 in project p-1 must NOT delete
    keys under a-1 in OTHER projects (different storage_key_prefix
    by design).  Cross-tenant safety regression."""
    from surogates.runtime.memory_io import delete_memory_prefix

    backend = _FakeBackend()
    await backend.write("memory-bk", "p-1/a-1/users/u/memory.json", b"{}")
    await backend.write("memory-bk", "p-2/a-1/users/u/memory.json", b"{}")

    await delete_memory_prefix(
        backend, bucket="memory-bk", prefix="p-1/a-1/",
    )

    assert "memory-bk/p-2/a-1/users/u/memory.json" in backend.objects
