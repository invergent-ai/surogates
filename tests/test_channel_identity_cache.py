"""Tests for the per-message identity-resolution cache.

The inbound pipeline resolves a sender's identity on every message; for an
already-known sender that is a pure DB read that never changes within a short
window.  ``make_cached_identity_resolver`` memoizes it (reusing
``ChannelRoutingCache``'s TTL + single-flight + negative cache) so a chatty
sender doesn't re-hit the DB each message, while a short TTL bounds staleness.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from surogates.channels.identity import (
    ResolvedIdentity,
    make_cached_identity_resolver,
)

SF = object()  # opaque session_factory sentinel — never called by the fakes


def _identity(platform: str, uid: str, org_id):
    return ResolvedIdentity(
        user_id=uuid4(), org_id=org_id, platform=platform, platform_user_id=uid
    )


@pytest.mark.asyncio
async def test_known_sender_served_from_cache_after_first_resolve():
    resolve_calls: list = []
    org = uuid4()
    ident = _identity("slack", "U1", org)

    async def fake_resolve(sf, platform, uid, *, org_id=None):
        resolve_calls.append((platform, uid, org_id))
        return ident

    async def fake_provision(*a, **k):
        raise AssertionError("known sender must not be provisioned")

    resolver = make_cached_identity_resolver(
        SF, resolve=fake_resolve, provision=fake_provision, ttl_seconds=100.0
    )

    r1 = await resolver(SF, "slack", "U1", org_id=org, display_name="A")
    r2 = await resolver(SF, "slack", "U1", org_id=org, display_name="A")

    assert r1.user_id == r2.user_id == ident.user_id
    assert len(resolve_calls) == 1, "second message served from cache, no DB re-read"


@pytest.mark.asyncio
async def test_unknown_sender_is_provisioned_once_then_cached():
    org = uuid4()
    provision_calls: list = []
    provisioned = _identity("slack", "U2", org)

    async def fake_resolve(sf, platform, uid, *, org_id=None):
        # Unknown until provisioned.
        return provisioned if provision_calls else None

    async def fake_provision(sf, *, platform, platform_user_id, org_id, display_name=""):
        provision_calls.append((platform, platform_user_id, org_id))
        return provisioned

    resolver = make_cached_identity_resolver(
        SF, resolve=fake_resolve, provision=fake_provision, ttl_seconds=100.0
    )

    r1 = await resolver(SF, "slack", "U2", org_id=org)
    r2 = await resolver(SF, "slack", "U2", org_id=org)

    assert r1.user_id == r2.user_id == provisioned.user_id
    assert len(provision_calls) == 1, "provisioned once; cache invalidated then re-loads the row"


@pytest.mark.asyncio
async def test_same_user_distinct_orgs_are_cached_separately():
    org_a, org_b = uuid4(), uuid4()
    ia = _identity("slack", "U3", org_a)
    ib = _identity("slack", "U3", org_b)

    async def fake_resolve(sf, platform, uid, *, org_id=None):
        # The cache round-trips org_id through the key as a string; the real
        # resolve_identity tolerates that against the UUID column.
        return ia if str(org_id) == str(org_a) else ib

    async def fake_provision(*a, **k):
        raise AssertionError("both senders are known")

    resolver = make_cached_identity_resolver(
        SF, resolve=fake_resolve, provision=fake_provision, ttl_seconds=100.0
    )

    ra = await resolver(SF, "slack", "U3", org_id=org_a)
    rb = await resolver(SF, "slack", "U3", org_id=org_b)

    assert ra.org_id == org_a
    assert rb.org_id == org_b
    assert ra.user_id != rb.user_id
