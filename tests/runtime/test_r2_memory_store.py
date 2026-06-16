"""Tests for R2MemoryStore.

Async sibling of the legacy disk-based
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
        backend=_FakeBackend(), bucket="bk",
        keys={
            "memory": "p/users/u/memory.json",
            "user": "p/users/u/user.json",
        },
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
        backend=backend, bucket="bk",
        keys={
            "memory": "p/users/u/memory.json",
            "user": "p/users/u/user.json",
        },
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
        backend=_FakeBackend(), bucket="bk",
        keys={
            "memory": "p/users/u/memory.json",
            "user": "p/users/u/user.json",
        },
    )
    await store.load_from_r2()
    await store.add("memory", "first remembered fact")
    formatted = store.format_for_system_prompt("memory")
    assert formatted is not None
    assert "first remembered fact" in formatted


@pytest.mark.asyncio
async def test_r2_memory_store_calls_on_write_after_persist():
    """The on_write callback fires after a
    successful persist so the harness can publish the invalidation
    message AND emit the audit event in one place."""
    from surogates.memory.r2_store import R2MemoryStore

    backend = _FakeBackend()
    write_calls: list[dict] = []

    async def on_write(action, *, target, new_version, conflict_detected):
        write_calls.append({
            "action": action,
            "new_version": new_version,
            "conflict_detected": conflict_detected,
        })

    store = R2MemoryStore(
        backend=backend, bucket="bk",
        keys={
            "memory": "p/users/u/memory.json",
            "user": "p/users/u/user.json",
        },
        on_write=on_write,
    )
    await store.load_from_r2()
    await store.add("memory", "x")
    assert write_calls == [
        {"action": "add", "new_version": 1, "conflict_detected": False},
    ]


@pytest.mark.asyncio
async def test_r2_memory_store_detects_conflict_in_callback():
    """When another writer lands between load_from_r2 and the
    next add, the callback fires with conflict_detected=True so
    the harness can emit MEMORY_CONFLICT audit."""
    from surogates.memory.r2_store import R2MemoryStore
    from surogates.runtime.memory_io import write_user_memory

    backend = _FakeBackend()
    seen: list[bool] = []

    async def on_write(action, *, target, new_version, conflict_detected):
        seen.append(conflict_detected)

    store = R2MemoryStore(
        backend=backend, bucket="bk",
        keys={
            "memory": "p/users/u/memory.json",
            "user": "p/users/u/user.json",
        },
        on_write=on_write,
    )
    await store.load_from_r2()  # version 0

    # Simulate another writer landing version 5 on R2 while our
    # session was working in-memory.
    await write_user_memory(
        backend, bucket="bk", key="p/users/u/memory.json",
        content="other-writer", expected_version=4,
    )

    await store.add("memory", "ours")
    assert seen == [True]
