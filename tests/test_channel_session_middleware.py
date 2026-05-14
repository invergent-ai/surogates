"""Tests for the channel_session branch of the auth middleware.

Covers :func:`surogates.tenant.auth.middleware._build_channel_session_context`
in isolation — happy path plus the four 401 invariants (missing session
row, org mismatch, agent mismatch, channel mismatch).  Integration with
``_tenant_context_from_token`` (the dispatch) is exercised by the JWT +
context unit suites; this file stays focused on the new branch.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from surogates.tenant.auth.middleware import _build_channel_session_context


def _channel_session_payload(*, org_id, agent_id, session_id, channel):
    """Return a payload shaped like a decoded channel_session JWT."""
    return {
        "sub": str(session_id),
        "type": "channel_session",
        "org_id": str(org_id),
        "agent_id": agent_id,
        "session_id": str(session_id),
        "channel": channel,
        "permissions": [],
    }


def _stub_session_factory(*, session_row, org_row):
    """Build an ``async_sessionmaker``-shaped stub.

    The middleware does two operations on the yielded session:

    1. ``await db_session.get(SessionModel, session_id)`` — returns the
       row (or ``None``).
    2. ``await db_session.execute(stmt)`` (via ``_load_org_or_401`` →
       ``_load_org``) — returns a result object whose
       ``scalar_one_or_none()`` produces the org row.

    Both ``async with`` and the two coroutines are stubbed so the code
    under test runs unmodified.
    """
    fake_session = AsyncMock()
    fake_session.get = AsyncMock(return_value=session_row)

    fake_result = MagicMock()
    fake_result.scalar_one_or_none = MagicMock(return_value=org_row)
    fake_session.execute = AsyncMock(return_value=fake_result)

    @asynccontextmanager
    async def _cm():
        yield fake_session

    factory = MagicMock(side_effect=lambda: _cm())
    return factory


def _make_session_row(*, session_id, org_id, agent_id, channel):
    return SimpleNamespace(
        id=session_id,
        org_id=org_id,
        agent_id=agent_id,
        channel=channel,
    )


def _make_org_row(*, config=None):
    return SimpleNamespace(config=config or {})


@pytest.mark.asyncio
class TestBuildChannelSessionContext:
    """Happy + 401 invariants for ``_build_channel_session_context``."""

    async def test_happy_path_returns_channel_context(self, tmp_path):
        org_id = uuid4()
        agent_id = "support-bot"
        session_id = uuid4()
        payload = _channel_session_payload(
            org_id=org_id,
            agent_id=agent_id,
            session_id=session_id,
            channel="website",
        )
        session_row = _make_session_row(
            session_id=session_id,
            org_id=org_id,
            agent_id=agent_id,
            channel="website",
        )
        factory = _stub_session_factory(
            session_row=session_row,
            org_row=_make_org_row(config={"agent_name": "Bot"}),
        )

        ctx = await _build_channel_session_context(
            factory, payload, str(tmp_path),
        )

        assert ctx.org_id == org_id
        assert ctx.user_id is None
        assert ctx.service_account_id is None
        assert ctx.session_scope_id == session_id
        assert ctx.asset_root == f"{tmp_path}/{org_id}"
        assert ctx.org_config == {"agent_name": "Bot"}
        assert ctx.permissions == frozenset()

    async def test_missing_session_row_401s(self, tmp_path):
        payload = _channel_session_payload(
            org_id=uuid4(),
            agent_id="x",
            session_id=uuid4(),
            channel="website",
        )
        factory = _stub_session_factory(
            session_row=None,
            org_row=_make_org_row(),
        )
        with pytest.raises(HTTPException) as exc:
            await _build_channel_session_context(
                factory, payload, str(tmp_path),
            )
        assert exc.value.status_code == 401
        assert "Session not found" in exc.value.detail

    async def test_org_mismatch_401s(self, tmp_path):
        session_id = uuid4()
        payload = _channel_session_payload(
            org_id=uuid4(),  # JWT claims this org…
            agent_id="x",
            session_id=session_id,
            channel="website",
        )
        # …but the row belongs to a different org.
        session_row = _make_session_row(
            session_id=session_id,
            org_id=uuid4(),
            agent_id="x",
            channel="website",
        )
        factory = _stub_session_factory(
            session_row=session_row,
            org_row=_make_org_row(),
        )
        with pytest.raises(HTTPException) as exc:
            await _build_channel_session_context(
                factory, payload, str(tmp_path),
            )
        assert exc.value.status_code == 401
        assert "org" in exc.value.detail.lower()

    async def test_agent_mismatch_401s(self, tmp_path):
        org_id = uuid4()
        session_id = uuid4()
        payload = _channel_session_payload(
            org_id=org_id,
            agent_id="claimed-agent",
            session_id=session_id,
            channel="website",
        )
        session_row = _make_session_row(
            session_id=session_id,
            org_id=org_id,
            agent_id="actual-agent",
            channel="website",
        )
        factory = _stub_session_factory(
            session_row=session_row,
            org_row=_make_org_row(),
        )
        with pytest.raises(HTTPException) as exc:
            await _build_channel_session_context(
                factory, payload, str(tmp_path),
            )
        assert exc.value.status_code == 401
        assert "agent" in exc.value.detail.lower()

    async def test_channel_mismatch_401s(self, tmp_path):
        """JWT says ``website``; session row says ``slack``."""
        org_id = uuid4()
        session_id = uuid4()
        payload = _channel_session_payload(
            org_id=org_id,
            agent_id="x",
            session_id=session_id,
            channel="website",
        )
        session_row = _make_session_row(
            session_id=session_id,
            org_id=org_id,
            agent_id="x",
            channel="slack",
        )
        factory = _stub_session_factory(
            session_row=session_row,
            org_row=_make_org_row(),
        )
        with pytest.raises(HTTPException) as exc:
            await _build_channel_session_context(
                factory, payload, str(tmp_path),
            )
        assert exc.value.status_code == 401
        assert "channel" in exc.value.detail.lower()

    async def test_org_row_missing_propagates_401(self, tmp_path):
        """``_load_org_or_401`` 401s when the org row has been deleted."""
        org_id = uuid4()
        session_id = uuid4()
        payload = _channel_session_payload(
            org_id=org_id,
            agent_id="x",
            session_id=session_id,
            channel="website",
        )
        session_row = _make_session_row(
            session_id=session_id,
            org_id=org_id,
            agent_id="x",
            channel="website",
        )
        # session_row found, but org row missing → `_load_org_or_401` 401s.
        factory = _stub_session_factory(
            session_row=session_row,
            org_row=None,
        )
        with pytest.raises(HTTPException) as exc:
            await _build_channel_session_context(
                factory, payload, str(tmp_path),
            )
        assert exc.value.status_code == 401
