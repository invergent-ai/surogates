"""Unit tests for :func:`surogates.api.routes._shared.resolve_system_bundle`.

The resolver is the catalogue-route hop that pulls the shared
``platform/system-skills`` bundle off ``app.state.system_bundle_cache``
and hands it to :meth:`ResourceLoader.load_skills`.

Contract:

* returns the bundle the cache yields,
* returns ``None`` when the cache attribute is missing entirely
  (older deploys / tests with a minimal ``app.state``),
* returns ``None`` when the cache raises :class:`LookupError`
  (no ``v*`` tag yet — operator hasn't seeded),
* returns ``None`` on any other cache exception (Hub network blip
  must not 500 the catalogue route).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from surogates.api.routes._shared import resolve_system_bundle


def _request(cache: object | None) -> SimpleNamespace:
    """Build a minimal ``Request`` stand-in.

    ``resolve_system_bundle`` only reads ``request.app.state``; nothing
    else on the FastAPI Request surface is touched.
    """
    state = SimpleNamespace()
    if cache is not None:
        state.system_bundle_cache = cache
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.mark.asyncio
async def test_returns_bundle_from_cache() -> None:
    bundle = object()
    cache = SimpleNamespace(get=AsyncMock(return_value=bundle))

    result = await resolve_system_bundle(_request(cache))

    assert result is bundle
    cache.get.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_returns_none_when_cache_missing() -> None:
    """Older api deploys may not have wired the cache yet."""

    result = await resolve_system_bundle(_request(None))

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_lookup_error() -> None:
    """No ``v*`` tag yet — operator hasn't run seed-builtin-skills."""

    cache = SimpleNamespace(get=AsyncMock(side_effect=LookupError("no tag")))

    result = await resolve_system_bundle(_request(cache))

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_arbitrary_exception() -> None:
    """Hub network blip must NOT propagate — the catalog route
    degrades to 'no Layer 1a' rather than 500."""

    cache = SimpleNamespace(get=AsyncMock(side_effect=RuntimeError("hub down")))

    result = await resolve_system_bundle(_request(cache))

    assert result is None
