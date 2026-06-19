"""Tests for the use-time browser-minutes gate (ops_credits).

Uses an in-memory SQLite stand-in for the ops DB wired through the
ops_engine module-level factory, mirroring test_worker_kb_loading.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from surogates.browser.base import BrowserCreditsExhaustedError
from surogates.db import ops_engine
from surogates.db.ops_credits import assert_browser_minutes_available
from surogates.db.ops_models import OpsBase, OpsCreditBalance

PROJECT = "p1"
FUTURE = datetime.now(timezone.utc) + timedelta(days=10)
PAST = datetime.now(timezone.utc) - timedelta(days=1)


@pytest.fixture
async def ops_factory(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(OpsBase.metadata.create_all)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )
    monkeypatch.setattr(ops_engine, "_session_factory", factory)
    yield factory
    await engine.dispose()


async def _seed_balance(factory, *, plan, topup, cycle_end=FUTURE):
    async with factory() as s:
        s.add(OpsCreditBalance(
            id="cb-1", project_id=PROJECT, resource="browser_minutes",
            plan_remaining=plan, topup_remaining=topup, cycle_end=cycle_end,
        ))
        await s.commit()


async def test_allows_when_balance_positive(ops_factory):
    await _seed_balance(ops_factory, plan=120, topup=0)
    await assert_browser_minutes_available(PROJECT)  # no raise


async def test_allows_when_only_topup_remains(ops_factory):
    await _seed_balance(ops_factory, plan=0, topup=15)
    await assert_browser_minutes_available(PROJECT)  # no raise


async def test_blocks_when_balance_zero(ops_factory):
    await _seed_balance(ops_factory, plan=0, topup=0)
    with pytest.raises(BrowserCreditsExhaustedError):
        await assert_browser_minutes_available(PROJECT)


async def test_blocks_when_balance_negative(ops_factory):
    await _seed_balance(ops_factory, plan=0, topup=-280)
    with pytest.raises(BrowserCreditsExhaustedError):
        await assert_browser_minutes_available(PROJECT)


async def test_lenient_at_expired_cycle(ops_factory):
    """Balance is empty but the cycle ended; the writer side will
    re-grant on its next touch, so the gate stays lenient."""
    await _seed_balance(ops_factory, plan=0, topup=0, cycle_end=PAST)
    await assert_browser_minutes_available(PROJECT)  # no raise


async def test_allows_when_no_row(ops_factory):
    # Different project with no seeded balance row.
    await assert_browser_minutes_available("project-without-a-row")


async def test_allows_when_org_id_empty(ops_factory):
    await assert_browser_minutes_available("")


async def test_allows_when_ops_db_not_configured(monkeypatch):
    monkeypatch.setattr(ops_engine, "_session_factory", None)
    await assert_browser_minutes_available(PROJECT)  # no raise
