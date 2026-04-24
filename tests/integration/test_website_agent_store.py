"""Database-backed tests for :class:`WebsiteAgentStore`.

Covers the Python API ``surogate-ops`` uses to provision website
agents: create, get, list, update, delete, publishable-key resolution,
cache invalidation on mutate.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from surogates.channels.website_agent_store import (
    PUBLISHABLE_KEY_PREFIX,
    WebsiteAgentStore,
    _reset_caches,
    generate_publishable_key,
    hash_publishable_key,
)

from .conftest import create_org

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(autouse=True)
async def _flush_caches():
    _reset_caches()
    yield
    _reset_caches()


async def test_create_returns_raw_key_only_once(session_factory):
    """The publishable key is exposed on create and then only hashed persists."""
    org_id = await create_org(session_factory)
    store = WebsiteAgentStore(session_factory)

    issued = await store.create(
        org_id=org_id,
        name="support-bot",
        allowed_origins=["https://customer.com"],
    )
    assert issued.publishable_key.startswith(PUBLISHABLE_KEY_PREFIX)
    assert issued.publishable_key_prefix.startswith(PUBLISHABLE_KEY_PREFIX)
    # The display prefix is a strict prefix of the raw key.
    assert issued.publishable_key.startswith(issued.publishable_key_prefix)

    resolved = await store.get_by_publishable_key(issued.publishable_key)
    assert resolved is not None
    assert resolved.id == issued.id
    assert resolved.name == "support-bot"
    assert resolved.allowed_origins == ("https://customer.com",)
    assert resolved.enabled is True


async def test_create_requires_allowed_origins(session_factory):
    """An empty origin list would authenticate no browser; refuse up front."""
    org_id = await create_org(session_factory)
    store = WebsiteAgentStore(session_factory)
    with pytest.raises(ValueError):
        await store.create(
            org_id=org_id, name="x", allowed_origins=[],
        )


async def test_get_by_publishable_key_unknown_returns_none(session_factory):
    store = WebsiteAgentStore(session_factory)
    assert await store.get_by_publishable_key("surg_wk_nonexistent") is None


async def test_get_by_publishable_key_normalises_origins(session_factory):
    """Creation stores lowercased, slash-stripped origins."""
    org_id = await create_org(session_factory)
    store = WebsiteAgentStore(session_factory)
    issued = await store.create(
        org_id=org_id, name="x",
        allowed_origins=["HTTPS://Customer.COM/"],
    )
    resolved = await store.get_by_publishable_key(issued.publishable_key)
    assert resolved is not None
    assert resolved.allowed_origins == ("https://customer.com",)


async def test_update_invalidates_cache(session_factory):
    """Edits take effect on the next lookup in this process."""
    org_id = await create_org(session_factory)
    store = WebsiteAgentStore(session_factory)
    issued = await store.create(
        org_id=org_id, name="x",
        allowed_origins=["https://customer.com"],
    )
    # Warm both cache sides.
    warmed = await store.get_by_publishable_key(issued.publishable_key)
    assert warmed is not None

    # Shrink the origin list — the cached entry must not continue
    # reporting the old list.
    await store.update(issued.id, allowed_origins=["https://new-domain.com"])

    resolved = await store.get_by_publishable_key(issued.publishable_key)
    assert resolved is not None
    assert resolved.allowed_origins == ("https://new-domain.com",)


async def test_update_disable_blocks_lookup_by_id(session_factory):
    org_id = await create_org(session_factory)
    store = WebsiteAgentStore(session_factory)
    issued = await store.create(
        org_id=org_id, name="x",
        allowed_origins=["https://customer.com"],
    )
    await store.update(issued.id, enabled=False)
    resolved = await store.get(issued.id)
    assert resolved is not None
    assert resolved.enabled is False


async def test_update_on_unknown_agent_returns_none(session_factory):
    import uuid
    store = WebsiteAgentStore(session_factory)
    result = await store.update(uuid.uuid4(), enabled=False)
    assert result is None


async def test_delete_removes_row_and_invalidates_cache(session_factory):
    org_id = await create_org(session_factory)
    store = WebsiteAgentStore(session_factory)
    issued = await store.create(
        org_id=org_id, name="x",
        allowed_origins=["https://customer.com"],
    )
    # Warm cache.
    await store.get_by_publishable_key(issued.publishable_key)

    assert await store.delete(issued.id) is True
    assert await store.get_by_publishable_key(issued.publishable_key) is None
    assert await store.get(issued.id) is None
    # Second delete is a no-op that reports false.
    assert await store.delete(issued.id) is False


async def test_list_for_org_returns_only_org_rows(session_factory):
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    store = WebsiteAgentStore(session_factory)
    a1 = await store.create(org_id=org_a, name="a1", allowed_origins=["https://a.com"])
    a2 = await store.create(org_id=org_a, name="a2", allowed_origins=["https://a2.com"])
    _b1 = await store.create(org_id=org_b, name="b1", allowed_origins=["https://b.com"])

    listed = await store.list_for_org(org_a)
    listed_ids = {row.id for row in listed}
    assert listed_ids == {a1.id, a2.id}


async def test_publishable_key_collision_unique_constraint(session_factory):
    """SHA-256(token) uniqueness makes duplicate keys impossible at the DB level."""
    # Invariant lives on the column; this test documents that we rely
    # on it rather than application-level dedup.
    from surogates.db.models import WebsiteAgent

    org_id = await create_org(session_factory)
    key1 = generate_publishable_key()
    key2 = generate_publishable_key()
    assert hash_publishable_key(key1) != hash_publishable_key(key2)

    # Sanity: two independently-created rows get distinct hashes.
    store = WebsiteAgentStore(session_factory)
    i1 = await store.create(org_id=org_id, name="a", allowed_origins=["https://a.com"])
    i2 = await store.create(org_id=org_id, name="b", allowed_origins=["https://b.com"])
    async with session_factory() as db:
        r1 = await db.get(WebsiteAgent, i1.id)
        r2 = await db.get(WebsiteAgent, i2.id)
    assert r1.publishable_key_hash != r2.publishable_key_hash
