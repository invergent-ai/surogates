"""Shared test fixtures and helpers for the tasks unit test package.

DB-backed tests live under ``tests/integration/tasks/`` and use the
testcontainers fixtures from ``tests/integration/conftest.py``.  The
helpers here are MagicMock-based, used by tools/spawn/tick unit tests
that don't need a real database.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4


def _default_workspace_config() -> dict:
    """Minimum config required by ``create_child_session`` to seed a shared
    workspace on the child.  Matches the same shape used in
    ``tests/test_coordinator.py``."""
    return {
        "storage_bucket": "tenant-bucket",
        "workspace_path": "/workspace/tenant-bucket/parent",
        "supports_vision": False,
    }


def _make_session(**overrides: Any) -> MagicMock:
    """Build a MagicMock that quacks like a ``surogates.session.models.Session``."""
    session = MagicMock()
    session.id = overrides.get("id", uuid4())
    session.parent_id = overrides.get("parent_id")
    session.task_id = overrides.get("task_id")
    session.agent_id = overrides.get("agent_id", "agent-test")
    session.user_id = overrides.get("user_id", None)
    session.service_account_id = overrides.get("service_account_id", None)
    session.org_id = overrides.get("org_id", uuid4())
    session.model = overrides.get("model", "gpt-4o")
    session.channel = overrides.get("channel", "web")
    session.status = overrides.get("status", "active")
    session.config = overrides.get("config", _default_workspace_config())
    return session


def _make_store() -> AsyncMock:
    """A SessionStore-shaped AsyncMock with the minimum surface the task
    layer touches (``create_session``, ``emit_event``, ``get_session``)."""
    store = AsyncMock()
    store.create_session = AsyncMock(return_value=_make_session(id=uuid4()))
    store.emit_event = AsyncMock(return_value=1)
    store.get_session = AsyncMock(return_value=_make_session())
    store.get_events = AsyncMock(return_value=[])
    return store


def _make_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.zadd = AsyncMock()
    redis.publish = AsyncMock()
    return redis


def _make_task(**overrides: Any) -> MagicMock:
    """A MagicMock ORM Task row with sensible defaults for tool/spawn tests."""
    t = MagicMock()
    t.id = overrides.get("id", uuid4())
    t.org_id = overrides.get("org_id", uuid4())
    t.parent_session_id = overrides.get("parent_session_id", uuid4())
    t.agent_def_name = overrides.get("agent_def_name", None)
    t.goal = overrides.get("goal", "test goal")
    t.context = overrides.get("context", None)
    t.current_session_id = overrides.get("current_session_id", None)
    t.status = overrides.get("status", "ready")
    t.result = overrides.get("result", None)
    t.blocked_reason = overrides.get("blocked_reason", None)
    t.attempt_count = overrides.get("attempt_count", 0)
    t.max_attempts = overrides.get("max_attempts", 3)
    return t
