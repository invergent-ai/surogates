"""Tests for R2MemoryStore.

Plan 4 / Task 10.  Async sibling of the legacy disk-based
MemoryStore.  load_from_r2() is async and called once at session
start (populates an in-memory working set); reads
(format_for_system_prompt / get_entries) are sync so PromptBuilder
stays sync; writes (add / replace / remove) are async because they
hit R2 and may emit MEMORY_CONFLICT audits.
"""

from __future__ import annotations

import pytest


class _FakeBackend:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.write_calls: list[tuple[str, str, bytes]] = []

    async def read(self, bucket, key):
        full = f"{bucket}/{key}"
        if full not in self.objects:
            raise KeyError(full)
        return self.objects[full]

    async def write(self, bucket, key, data):
        self.write_calls.append((bucket, key, data))
        self.objects[f"{bucket}/{key}"] = data


@pytest.mark.asyncio
async def test_r2_memory_store_load_empty_returns_zero_version():
    """A fresh user with no prior memory loads as version=0 with
    no entries; the PromptBuilder's memory section is empty."""
    from surogates.memory.r2_store import R2MemoryStore

    store = R2MemoryStore(
        backend=_FakeBackend(), bucket="bk", key="p/users/u/memory.json",
    )
    await store.load_from_r2()
    assert store.last_seen_version == 0
    assert store.get_entries("memory") == []


@pytest.mark.asyncio
async def test_r2_memory_store_add_writes_envelope():
    from surogates.memory.r2_store import R2MemoryStore
    from surogates.runtime.memory_protocol import decode_envelope

    backend = _FakeBackend()
    store = R2MemoryStore(
        backend=backend, bucket="bk", key="p/users/u/memory.json",
    )
    await store.load_from_r2()
    await store.add("memory", "remember the API base URL is foo.com")
    assert len(backend.write_calls) == 1
    raw = backend.write_calls[0][2]
    env = decode_envelope(raw)
    assert "remember the API" in env.content
    assert env.version == 1


@pytest.mark.asyncio
async def test_r2_memory_store_format_for_system_prompt_includes_entries():
    """The sync read API returns the same shape as the legacy
    disk-based MemoryStore so PromptBuilder.build() stays sync."""
    from surogates.memory.r2_store import R2MemoryStore

    store = R2MemoryStore(
        backend=_FakeBackend(), bucket="bk", key="p/users/u/memory.json",
    )
    await store.load_from_r2()
    await store.add("memory", "first remembered fact")
    formatted = store.format_for_system_prompt("memory")
    assert formatted is not None
    assert "first remembered fact" in formatted
