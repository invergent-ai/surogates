"""Tests for surogates.harness.connection_health -- dead connection cleanup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from surogates.harness.connection_health import cleanup_dead_connections


class TestCleanupDeadConnections:
    @pytest.mark.asyncio()
    async def test_no_internal_client(self) -> None:
        """Client without _client attribute returns 0."""
        client = object()
        assert await cleanup_dead_connections(client) == 0

    @pytest.mark.asyncio()
    async def test_no_pool(self) -> None:
        """Client with _client but no pool returns 0."""
        http_client = SimpleNamespace()
        client = SimpleNamespace(_client=http_client)
        assert await cleanup_dead_connections(client) == 0

    @pytest.mark.asyncio()
    async def test_empty_pool(self) -> None:
        """Client with empty connection pool returns 0."""
        pool = SimpleNamespace(_pool=[])
        http_client = SimpleNamespace(_pool=pool)
        client = SimpleNamespace(_client=http_client)
        assert await cleanup_dead_connections(client) == 0

    @pytest.mark.asyncio()
    async def test_cleans_idle_connections(self) -> None:
        """Idle, non-closed connections should be closed."""
        conn1 = MagicMock(is_idle=True, is_closed=False)
        conn1.aclose = AsyncMock()
        conn2 = MagicMock(is_idle=True, is_closed=False)
        conn2.aclose = AsyncMock()

        pool = SimpleNamespace(_pool=[conn1, conn2])
        http_client = SimpleNamespace(_pool=pool)
        client = SimpleNamespace(_client=http_client)

        cleaned = await cleanup_dead_connections(client)
        assert cleaned == 2
        conn1.aclose.assert_awaited_once()
        conn2.aclose.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_skips_closed_connections(self) -> None:
        """Already-closed connections should not be touched."""
        conn = MagicMock(is_idle=True, is_closed=True)
        conn.aclose = AsyncMock()

        pool = SimpleNamespace(_pool=[conn])
        http_client = SimpleNamespace(_pool=pool)
        client = SimpleNamespace(_client=http_client)

        cleaned = await cleanup_dead_connections(client)
        assert cleaned == 0
        conn.aclose.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_skips_non_idle_connections(self) -> None:
        """Non-idle (active) connections should not be closed."""
        conn = MagicMock(is_idle=False, is_closed=False)
        conn.aclose = AsyncMock()

        pool = SimpleNamespace(_pool=[conn])
        http_client = SimpleNamespace(_pool=pool)
        client = SimpleNamespace(_client=http_client)

        cleaned = await cleanup_dead_connections(client)
        assert cleaned == 0

    @pytest.mark.asyncio()
    async def test_handles_close_exception_gracefully(self) -> None:
        """If aclose() raises, the exception is swallowed and we continue."""
        conn1 = MagicMock(is_idle=True, is_closed=False)
        conn1.aclose = AsyncMock(side_effect=OSError("connection reset"))
        conn2 = MagicMock(is_idle=True, is_closed=False)
        conn2.aclose = AsyncMock()

        pool = SimpleNamespace(_pool=[conn1, conn2])
        http_client = SimpleNamespace(_pool=pool)
        client = SimpleNamespace(_client=http_client)

        cleaned = await cleanup_dead_connections(client)
        # conn1 failed to close, conn2 succeeded.
        assert cleaned == 1

    @pytest.mark.asyncio()
    async def test_uses_transport_fallback(self) -> None:
        """Falls back to _transport if _pool is not present."""
        conn = MagicMock(is_idle=True, is_closed=False)
        conn.aclose = AsyncMock()

        transport = SimpleNamespace(_pool=[conn])
        http_client = SimpleNamespace(_transport=transport)
        client = SimpleNamespace(_client=http_client)

        cleaned = await cleanup_dead_connections(client)
        assert cleaned == 1

    @pytest.mark.asyncio()
    async def test_uses_connections_attribute(self) -> None:
        """Falls back to 'connections' attribute on the pool."""
        conn = MagicMock(is_idle=True, is_closed=False)
        conn.aclose = AsyncMock()

        pool = SimpleNamespace(connections=[conn])
        http_client = SimpleNamespace(_pool=pool)
        client = SimpleNamespace(_client=http_client)

        cleaned = await cleanup_dead_connections(client)
        assert cleaned == 1

    @pytest.mark.asyncio()
    async def test_handles_client_attribute_error(self) -> None:
        """Gracefully handles unexpected client structure."""
        # Simulate a client that raises on attribute access.
        class WeirdClient:
            @property
            def _client(self):
                raise AttributeError("nope")

        cleaned = await cleanup_dead_connections(WeirdClient())
        assert cleaned == 0

    @pytest.mark.asyncio()
    async def test_mixed_connections(self) -> None:
        """Mix of idle/closed/active connections."""
        idle_open = MagicMock(is_idle=True, is_closed=False)
        idle_open.aclose = AsyncMock()
        idle_closed = MagicMock(is_idle=True, is_closed=True)
        idle_closed.aclose = AsyncMock()
        active_open = MagicMock(is_idle=False, is_closed=False)
        active_open.aclose = AsyncMock()

        pool = SimpleNamespace(_pool=[idle_open, idle_closed, active_open])
        http_client = SimpleNamespace(_pool=pool)
        client = SimpleNamespace(_client=http_client)

        cleaned = await cleanup_dead_connections(client)
        assert cleaned == 1
        idle_open.aclose.assert_awaited_once()
        idle_closed.aclose.assert_not_awaited()
        active_open.aclose.assert_not_awaited()
