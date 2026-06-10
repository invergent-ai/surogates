"""Unit tests for CodeCommandMixin via a fake harness (no real AgentHarness)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.harness.loop_code_commands import CodeCommandMixin
from surogates.session.events import EventType

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeStore:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def emit_event(self, session_id, event_type, data):
        self.events.append((event_type, data))
        return len(self.events)  # real store returns a BIGSERIAL int id

    async def advance_harness_cursor(self, session_id, *, through_event_id, lease_token):
        return None


class _Harness(CodeCommandMixin):
    def __init__(self, vault=None):
        self._store = _FakeStore()
        self._tenant = SimpleNamespace(org_id=uuid4(), user_id=uuid4())
        self._credential_vault = vault


def _session():
    return SimpleNamespace(id=uuid4(), config={})


def _lease():
    return SimpleNamespace(lease_token="lease-token")


def _last_message(harness) -> str:
    event_type, data = harness._store.events[-1]
    assert event_type == EventType.LLM_RESPONSE
    return data["message"]["content"]


async def test_help_emits_usage():
    h = _Harness()
    await h._handle_code_command(_session(), "/code", _lease())
    assert "/code claude" in _last_message(h)


async def test_login_emits_instructions():
    h = _Harness()
    await h._handle_code_command(_session(), "/code login claude", _lease())
    assert "claude setup-token" in _last_message(h)


async def test_status_without_vault_explains():
    h = _Harness(vault=None)
    await h._handle_code_command(_session(), "/code status", _lease())
    assert "vault" in _last_message(h).lower()


async def test_run_is_stubbed():
    h = _Harness()
    await h._handle_code_command(_session(), '/code claude "do it"', _lease())
    assert "isn't available yet" in _last_message(h)
