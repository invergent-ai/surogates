"""Integration tests for the session_search builtin."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from surogates.session.events import EventType
from surogates.tools.builtin.session_search import _session_search_handler

from .conftest import create_org, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_session_search_accepts_service_account_principal(
    session_store, session_factory,
):
    """API sessions should search prior sessions for the same service account."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(session_factory, org_id)

    session = await session_store.create_session(
        user_id=None,
        service_account_id=issued.id,
        org_id=org_id,
        agent_id="agent-a",
        channel="api",
    )
    await session_store.emit_event(
        session.id,
        EventType.USER_MESSAGE,
        {"content": "session 2 was about deployment notes"},
    )

    result = json.loads(
        await _session_search_handler(
            {"query": "deployment notes"},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id,
                user_id=None,
                service_account_id=issued.id,
            ),
            agent_id="agent-a",
        )
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["results"][0]["session_id"] == str(session.id)
