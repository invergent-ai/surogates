"""`session doctor` -- read-only coherence checks for one session.

Answers the question the PROD debugging flow actually asks: why is this
session not doing anything? Every check is a read; nothing here can change
state.

Scope is deliberately narrow. Invariants that are enforced only at creation
(so a legacy row can violate them undetected) and config values the runtime
silently ignores are worth reporting; restating what the event log already
shows plainly is not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from surogates.session.doctor import diagnose_session


class _Store:
    def __init__(self, session: Any, pending: dict | None = None) -> None:
        self._session = session
        self._pending = pending

    async def get_session(self, session_id):
        if self._session is None:
            raise LookupError("nope")
        return self._session


def _session(**config) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), status="active", config=config)


async def _codes(store, monkeypatch, pending=None) -> list[str]:
    async def fake_pending(_store, *, session_id, tool_call_id=None):
        return pending

    monkeypatch.setattr(
        "surogates.session.doctor.pending_input_for_session", fake_pending,
    )
    return [f.code for f in await diagnose_session(store, uuid4())]


@pytest.mark.asyncio
async def test_a_healthy_session_reports_nothing(monkeypatch):
    assert await _codes(_Store(_session()), monkeypatch) == []


@pytest.mark.asyncio
async def test_missing_session_is_reported(monkeypatch):
    assert "session_not_found" in await _codes(_Store(None), monkeypatch)


@pytest.mark.asyncio
async def test_an_open_question_explains_the_silence(monkeypatch):
    """The most common 'stuck' session is one waiting on a human."""
    pending = {
        "tool_call_id": "call_1",
        "questions": [{"prompt": "which one?"}],
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=2),
    }
    codes = await _codes(_Store(_session()), monkeypatch, pending=pending)
    assert "waiting_on_user" in codes
    assert "input_request_expired" not in codes


@pytest.mark.asyncio
async def test_an_expired_question_is_called_out(monkeypatch):
    """Past the tool's own wait cap nobody is listening for the answer."""
    pending = {
        "tool_call_id": "call_1",
        "questions": [],
        "created_at": datetime.now(timezone.utc) - timedelta(hours=3),
    }
    codes = await _codes(_Store(_session()), monkeypatch, pending=pending)
    assert "input_request_expired" in codes


@pytest.mark.asyncio
async def test_naive_timestamps_do_not_crash_the_check(monkeypatch):
    """Postgres hands back naive UTC; comparing it to an aware now() raises."""
    pending = {
        "tool_call_id": "call_1",
        "questions": [],
        "created_at": datetime.utcnow() - timedelta(hours=3),
    }
    codes = await _codes(_Store(_session()), monkeypatch, pending=pending)
    assert "input_request_expired" in codes


@pytest.mark.asyncio
async def test_two_objectives_at_once_is_reported(monkeypatch):
    """Exclusivity is enforced only when a mission is created, so a row that
    predates the rule -- or was written directly -- can hold both."""
    session = _session(
        outcome={"description": "ship it", "status": "active"},
        active_mission_id=str(uuid4()),
    )
    assert "objective_conflict" in await _codes(_Store(session), monkeypatch)


@pytest.mark.asyncio
async def test_a_paused_goal_beside_a_mission_is_fine(monkeypatch):
    """Only an *active* outcome conflicts -- the rule the creation path uses."""
    session = _session(
        outcome={"description": "ship it", "status": "paused"},
        active_mission_id=str(uuid4()),
    )
    assert "objective_conflict" not in await _codes(_Store(session), monkeypatch)


@pytest.mark.parametrize("bad", [0, -1, "thirty", 3.5, True, [], {}])
@pytest.mark.asyncio
async def test_an_unusable_iteration_cap_is_reported(monkeypatch, bad):
    """The worker silently falls back to the platform default, so an operator
    who set this has no way to know it did nothing."""
    codes = await _codes(_Store(_session(max_iterations=bad)), monkeypatch)
    assert "unusable_max_iterations" in codes


@pytest.mark.parametrize("good", [1, 30, 90])
@pytest.mark.asyncio
async def test_a_usable_iteration_cap_is_silent(monkeypatch, good):
    codes = await _codes(_Store(_session(max_iterations=good)), monkeypatch)
    assert "unusable_max_iterations" not in codes


@pytest.mark.asyncio
async def test_findings_carry_a_readable_detail(monkeypatch):
    async def fake_pending(_store, *, session_id, tool_call_id=None):
        return None

    monkeypatch.setattr(
        "surogates.session.doctor.pending_input_for_session", fake_pending,
    )
    findings = await diagnose_session(_Store(_session(max_iterations=0)), uuid4())
    assert findings and all(f.detail.strip() for f in findings)
