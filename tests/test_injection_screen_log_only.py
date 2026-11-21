"""``api.injection_screen_service_accounts``: a service account's message flagged by the injection screen is recorded,
not refused, when the deployment says ``log``; every other channel, and the default, block as before.

11-21: the screen refused an ordinary SWE-Bench Pro task text (an Ansible change) as an "encoding attack" (0.8), so
the task could never start in an evaluation that sends task texts through the API. A deployment whose service
accounts are its own callers can now keep the record (the security event, with action "logged") and let them through.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from surogates.api.routes.sessions import SendMessageRequest, send_message
from surogates.session.events import EventType
from tests.test_chat_attachments import _agent_runtime, _stub_session

pytestmark = pytest.mark.asyncio


class _Flags:
    def detect(self, content: str, *, source: str):
        if "BLOCK_ME" in content:
            return SimpleNamespace(is_injection=True, explanation="encoding attack", threat_level=None, confidence=0.8,
                                   injection_type=None, matched_patterns=[])
        return SimpleNamespace(is_injection=False, explanation="")


async def _send(monkeypatch, *, channel: str, mode: str | None, content: str = "BLOCK_ME fix the parser",
                service_account: bool = True, seen: list | None = None):
    session = _stub_session()
    session.channel = channel
    store = SimpleNamespace(emit_event=AsyncMock(return_value=99), update_session_status=AsyncMock(return_value=None))
    if seen is not None:
        seen.append(store)
    monkeypatch.setattr("surogates.api.routes.sessions._get_injection_detector", lambda: _Flags())

    async def _get_session(*_a, **_k):
        return session

    monkeypatch.setattr("surogates.api.routes.sessions._get_session_for_tenant", _get_session)
    monkeypatch.setattr("surogates.api.routes.sessions._get_session_store", lambda _req: store)
    monkeypatch.setattr("surogates.api.routes.sessions.enqueue_session", AsyncMock(return_value=None))
    monkeypatch.setattr("surogates.api.routes.sessions.require_user_writable_session", lambda _s, _t: None)
    settings = None if mode is None else SimpleNamespace(api=SimpleNamespace(injection_screen_service_accounts=mode))
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(redis=None, session_factory=None, storage=None, settings=settings)),
        url=SimpleNamespace(path="/sessions/x/messages"),
    )
    result = await send_message(session_id=session.id, body=SendMessageRequest(content=content), request=request,
                                tenant=SimpleNamespace(user_id=None if service_account else uuid4(),
                                                       service_account_id=uuid4() if service_account else None),
                                agent_runtime=_agent_runtime())
    return result, store


def _security_events(store):
    return [c.args[2] for c in store.emit_event.await_args_list if c.args[1] == EventType.SECURITY_INJECTION_BLOCKED]


async def test_by_default_a_service_accounts_flagged_message_is_refused(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        await _send(monkeypatch, channel="api", mode=None)
    assert ei.value.status_code == 422


async def test_block_refuses_as_before(monkeypatch):
    stores: list = []
    with pytest.raises(HTTPException):
        await _send(monkeypatch, channel="api", mode="block", seen=stores)
    events = _security_events(stores[0])
    assert len(events) == 1 and events[0]["action"] == "blocked"


async def test_log_records_and_lets_a_service_accounts_message_through(monkeypatch):
    _result, store = await _send(monkeypatch, channel="api", mode="log")
    events = _security_events(store)
    assert len(events) == 1 and events[0]["action"] == "logged"
    assert any(c.args[1] == EventType.USER_MESSAGE for c in store.emit_event.await_args_list)


async def test_log_never_opens_another_channel(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        await _send(monkeypatch, channel="web", mode="log")
    assert ei.value.status_code == 422


async def test_log_never_lets_a_person_through_on_a_service_accounts_channel(monkeypatch):
    stores: list = []
    with pytest.raises(HTTPException) as ei:
        await _send(monkeypatch, channel="api", mode="log", service_account=False, seen=stores)
    assert ei.value.status_code == 422
    events = _security_events(stores[0])
    assert len(events) == 1 and events[0]["action"] == "blocked"


async def test_a_clean_message_records_nothing(monkeypatch):
    _result, store = await _send(monkeypatch, channel="api", mode="log", content="fix the parser")
    assert _security_events(store) == []
