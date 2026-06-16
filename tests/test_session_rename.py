"""Tests for the user-driven session rename endpoint.

Covers:

1. ``UpdateSessionRequest`` validates the title (non-empty, length cap).
2. The ``PATCH /sessions/{id}`` route calls ``store.update_session_title``
   with the cleaned title and returns the refreshed session.
3. Scheduler-owned (read-only) sessions are rejected.
4. The store's ``update_session_title`` overwrites unconditionally,
   distinct from ``update_session_title_if_empty``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from surogates.api.routes.sessions import (
    UpdateSessionRequest,
    update_session,
)
from surogates.session.models import Session


# ---------------------------------------------------------------------------
# UpdateSessionRequest schema
# ---------------------------------------------------------------------------


def test_update_session_request_accepts_valid_title():
    req = UpdateSessionRequest(title="Debug Redis Failures")
    assert req.title == "Debug Redis Failures"


def test_update_session_request_strips_surrounding_whitespace():
    req = UpdateSessionRequest(title="   Trimmed Title   ")
    assert req.title == "Trimmed Title"


def test_update_session_request_rejects_empty_title():
    with pytest.raises(ValidationError):
        UpdateSessionRequest(title="")


def test_update_session_request_rejects_blank_title():
    with pytest.raises(ValidationError):
        UpdateSessionRequest(title="    ")


def test_update_session_request_rejects_overlong_title():
    with pytest.raises(ValidationError):
        UpdateSessionRequest(title="x" * 257)


def test_update_session_request_accepts_max_length_title():
    req = UpdateSessionRequest(title="x" * 256)
    assert len(req.title) == 256


# ---------------------------------------------------------------------------
# update_session route
# ---------------------------------------------------------------------------


def _stub_session(*, channel: str = "web", config: dict | None = None) -> Session:
    from datetime import datetime, timezone

    return Session(
        id=uuid4(),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        agent_id="test-agent",
        channel=channel,
        status="active",
        config=config or {},
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
    )


@pytest.fixture()
def patched_update_session(monkeypatch):
    """Patch route dependencies so ``update_session`` runs without DB."""

    async def _runner(
        *,
        title: str,
        session: Session | None = None,
    ):
        target = session or _stub_session()
        renamed = target.model_copy(update={"title": title.strip()})
        store = SimpleNamespace(
            update_session_title=AsyncMock(),
            get_session=AsyncMock(return_value=renamed),
        )

        app = FastAPI()
        app.state.session_store = store
        app.state.settings = SimpleNamespace(agent_id=target.agent_id)
        request = SimpleNamespace(
            app=app,
            url=SimpleNamespace(path="/v1/sessions/abc"),
        )

        monkeypatch.setattr(
            "surogates.api.routes.sessions._get_session_for_tenant",
            AsyncMock(return_value=target),
        )

        body = UpdateSessionRequest(title=title)
        tenant = SimpleNamespace(
            user_id=target.user_id,
            org_id=target.org_id,
            service_account_id=None,
            session_scope_id=None,
        )
        agent_runtime = SimpleNamespace(agent_id=target.agent_id)

        response = await update_session(
            session_id=target.id,
            body=body,
            request=request,
            tenant=tenant,
            agent_runtime=agent_runtime,
        )
        return {
            "response": response,
            "store": store,
            "target": target,
        }

    return _runner


async def test_update_session_persists_title(patched_update_session):
    result = await patched_update_session(title="My new title")
    store = result["store"]
    target = result["target"]

    store.update_session_title.assert_awaited_once_with(target.id, "My new title")
    store.get_session.assert_awaited_once_with(target.id)
    assert result["response"].title == "My new title"


async def test_update_session_strips_whitespace_before_persist(
    patched_update_session,
):
    result = await patched_update_session(title="  Padded Title  ")
    store = result["store"]
    target = result["target"]

    # ``UpdateSessionRequest`` strips on validation -- the store sees the
    # already-cleaned value, not the raw whitespace-padded one.
    store.update_session_title.assert_awaited_once_with(target.id, "Padded Title")


async def test_update_session_rejects_scheduled_run(patched_update_session):
    """Scheduler-owned sessions are read-only -- rename must 409."""
    scheduled = _stub_session(
        channel="scheduled",
        config={"scheduled_session_id": str(uuid4())},
    )

    with pytest.raises(HTTPException) as exc:
        await patched_update_session(title="Cannot rename", session=scheduled)
    assert exc.value.status_code == 409
