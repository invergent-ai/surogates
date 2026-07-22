"""Unit tests for the operator-only user_reports builtin tool.

The ops-DB side (cohort overview) runs against in-memory SQLite via
the OpsBase metadata (the kb_tools/ops_credits convention).  The
surogates-DB side (org users + memory_summary) and the owner-scope
DB lookup are monkeypatched — their SQL runs against Postgres-only
JSONB in production and is exercised by the ops-side live checks.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from surogates.db import ops_engine
from surogates.db.ops_models import OpsAgent, OpsBase
from surogates.tools import owner_scope
from surogates.tools.builtin import user_reports as module

_ORG = UUID("11111111-1111-1111-1111-111111111111")
_AGENT = "agent-a"
_SA_ID = uuid4()

_OWNER_CONFIG = {"ops": {"user_id": "ops-user-1"}}


def _tenant(service_account_id=_SA_ID, org_id=_ORG):
    return SimpleNamespace(
        org_id=org_id,
        user_id=None,
        service_account_id=service_account_id,
    )


def _user_row(name, email, reports=None):
    return SimpleNamespace(
        id=uuid4(),
        display_name=name,
        email=email,
        memory_summary={"reports": reports} if reports is not None else {},
    )


@pytest.fixture
def owner_scoped(monkeypatch):
    monkeypatch.setattr(
        module, "is_owner_scoped", AsyncMock(return_value=True),
    )


@pytest.fixture
async def ops_sqlite(monkeypatch):
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


async def _call(arguments, **overrides):
    kwargs = {
        "session_store": SimpleNamespace(_sf=None),
        "tenant": _tenant(),
        "agent_id": _AGENT,
        "session_config": _OWNER_CONFIG,
    }
    kwargs.update(overrides)
    return json.loads(await module._user_reports_handler(arguments, **kwargs))


# ── owner-scope enforcement ────────────────────────────────────────


async def test_refuses_without_owner_scope(monkeypatch):
    monkeypatch.setattr(
        module, "is_owner_scoped", AsyncMock(return_value=False),
    )
    result = await _call({"action": "overview"})
    assert "operator" in result["error"]


async def test_owner_scope_receives_principal_and_config(monkeypatch):
    gate = AsyncMock(return_value=False)
    monkeypatch.setattr(module, "is_owner_scoped", gate)
    store = SimpleNamespace(_sf="factory-sentinel")
    await _call({"action": "overview"}, session_store=store)
    gate.assert_awaited_once_with(store, _SA_ID, _OWNER_CONFIG)


# ── overview (ops DB) ──────────────────────────────────────────────


async def test_overview_reads_ops_cohort_cache(owner_scoped, ops_sqlite):
    async with ops_sqlite() as db:
        db.add(
            OpsAgent(
                id=_AGENT,
                cohort_report={
                    "report_md": "## Cohort",
                    "generated_at": "2026-07-22T00:00:00Z",
                    "user_count": 5,
                },
            ),
        )
        await db.commit()
    result = await _call({"action": "overview"})
    assert result["overview"] == {
        "report_md": "## Cohort",
        "updated_at": "2026-07-22T00:00:00Z",
        "user_count": 5,
    }


async def test_overview_absent_gives_hint(owner_scoped, ops_sqlite):
    result = await _call({"action": "overview"})
    assert result["overview"] is None
    assert "Users page" in result["hint"]


# ── list / get (surogates DB, patched fetch) ───────────────────────


@pytest.fixture
def org_users(monkeypatch):
    maria = _user_row(
        "Maria Dumitrescu",
        "maria@example.com",
        reports={
            _AGENT: {
                "report_md": "## Maria",
                "generated_at": "2026-07-21T00:00:00Z",
            },
        },
    )
    tudor = _user_row("Tudor Enache", "tudor@example.com")
    monkeypatch.setattr(
        module, "_fetch_org_users", AsyncMock(return_value=[maria, tudor]),
    )
    return {"maria": maria, "tudor": tudor}


async def test_list_shows_only_users_with_reports(owner_scoped, org_users):
    result = await _call({"action": "list"})
    assert result["users_with_reports"] == [
        {
            "display_name": "Maria Dumitrescu",
            "email": "maria@example.com",
            "updated_at": "2026-07-21T00:00:00Z",
        },
    ]


async def test_get_by_partial_name(owner_scoped, org_users):
    result = await _call({"action": "get", "user": "maria"})
    assert result["display_name"] == "Maria Dumitrescu"
    assert result["report_md"] == "## Maria"


async def test_get_without_report_gives_hint(owner_scoped, org_users):
    result = await _call({"action": "get", "user": "tudor@example.com"})
    assert result["report"] is None
    assert "Users page" in result["hint"]


async def test_get_unknown_or_missing_user(owner_scoped, org_users):
    result = await _call({"action": "get", "user": "nobody"})
    assert "no end-user matching" in result["error"]
    result = await _call({"action": "get"})
    assert "requires the 'user' argument" in result["error"]


async def test_unknown_action(owner_scoped):
    result = await _call({"action": "bogus"})
    assert "must be one of" in result["error"]


# ── owner_scope module ─────────────────────────────────────────────


def test_config_ok_conditions():
    ok = owner_scope.owner_scope_config_ok
    assert ok(_SA_ID, _OWNER_CONFIG)
    assert not ok(None, _OWNER_CONFIG)
    assert not ok(_SA_ID, None)
    assert not ok(_SA_ID, {"ops": {}})
    assert not ok(_SA_ID, {**_OWNER_CONFIG, "agent_type": "copilot"})


class _FakeResult:
    def __init__(self, name):
        self._name = name

    def scalar_one_or_none(self):
        return self._name


class _FakeDb:
    def __init__(self, name):
        self._name = name

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_args, **_kwargs):
        return _FakeResult(self._name)


def _store(sa_name):
    return SimpleNamespace(_sf=lambda: _FakeDb(sa_name))


async def test_is_owner_scoped_requires_ops_chat_prefix():
    assert await owner_scope.is_owner_scoped(
        _store("ops-chat-org-user"), _SA_ID, _OWNER_CONFIG,
    )
    assert not await owner_scope.is_owner_scoped(
        _store("agent:some-agent"), _SA_ID, _OWNER_CONFIG,
    )
    assert not await owner_scope.is_owner_scoped(
        _store(None), _SA_ID, _OWNER_CONFIG,
    )
    # Config short-circuit: no DB call path needed at all.
    assert not await owner_scope.is_owner_scoped(
        _store("ops-chat-org-user"), None, _OWNER_CONFIG,
    )
