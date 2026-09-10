"""Message submission preserves metadata in the persisted user event."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from surogates.api.routes.sessions import SendMessageRequest, send_message
from surogates.session.models import Session


def _stub_session(*, status: str = "active") -> Session:
    now = SimpleNamespace()  # unused -- Session uses default datetimes via factory
    from datetime import datetime, timezone

    return Session(
        id=uuid4(),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        agent_id="test-agent",
        channel="web",
        status=status,
        config={},
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
    )


class _StubInjectionDetector:
    def detect(self, content, *, source):
        return SimpleNamespace(is_injection=False, explanation="")


@pytest.fixture()
def patched_send_message(monkeypatch):
    """Patch route dependencies so ``send_message`` runs without DB/Redis/AGT."""
    # Skip the AGT prompt-injection detector -- it would try to download
    # weights on first import.
    monkeypatch.setattr(
        "surogates.api.routes.sessions._get_injection_detector",
        lambda: _StubInjectionDetector(),
    )
    # The route enqueues onto Redis after emitting the event.  Replace it
    # with an async no-op so the test does not need a Redis container.
    monkeypatch.setattr(
        "surogates.api.routes.sessions.enqueue_session",
        AsyncMock(),
    )

    async def _runner(
        *,
        metadata: dict | None,
        session_status: str = "active",
    ) -> dict:
        session = _stub_session(status=session_status)
        store = SimpleNamespace(
            emit_event=AsyncMock(return_value=123),
            update_session_status=AsyncMock(),
        )

        app = FastAPI()
        app.state.session_store = store
        app.state.redis = SimpleNamespace()
        app.state.settings = SimpleNamespace(agent_id=session.agent_id)
        request = SimpleNamespace(
            app=app,
            url=SimpleNamespace(path="/v1/sessions/abc/messages"),
        )

        # Bypass _get_session_for_tenant + require_user_writable_session.
        monkeypatch.setattr(
            "surogates.api.routes.sessions._get_session_for_tenant",
            AsyncMock(return_value=session),
        )
        monkeypatch.setattr(
            "surogates.api.routes.sessions.require_user_writable_session",
            lambda _s: None,
        )

        body = SendMessageRequest(content="hi", metadata=metadata)
        tenant = SimpleNamespace(
            user_id=session.user_id,
            org_id=session.org_id,
            service_account_id=None,
            session_scope_id=None,
        )
        agent_runtime = SimpleNamespace(
            agent_id=session.agent_id, multi_session=True,
        )

        response = await send_message(
            session_id=session.id,
            body=body,
            request=request,
            tenant=tenant,
            agent_runtime=agent_runtime,
        )
        return {
            "response": response,
            "emit_args": store.emit_event.await_args,
        }

    return _runner


async def test_send_message_forwards_metadata_into_event(patched_send_message):
    metadata = {
        "view_context": {
            "kind": "agent",
            "id": "agt_123",
            "name": "Triage bot",
        }
    }

    result = await patched_send_message(metadata=metadata)
    args, kwargs = result["emit_args"]
    event_data = args[2] if len(args) >= 3 else kwargs["data"]

    assert event_data["content"] == "hi"
    assert event_data["metadata"] == metadata


async def test_send_message_omits_metadata_when_absent(patched_send_message):
    result = await patched_send_message(metadata=None)
    args, kwargs = result["emit_args"]
    event_data = args[2] if len(args) >= 3 else kwargs["data"]

    assert "metadata" not in event_data


async def test_send_message_preserves_empty_metadata_dict(patched_send_message):
    """An explicit empty ``{}`` is still forwarded so callers can probe support."""
    result = await patched_send_message(metadata={})
    args, kwargs = result["emit_args"]
    event_data = args[2] if len(args) >= 3 else kwargs["data"]

    assert event_data["metadata"] == {}
