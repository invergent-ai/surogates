"""Inbox hook tests for the clarify tool."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from surogates.db.models import InboxItem
from surogates.tools.builtin import clarify as clarify_module

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_clarify_emits_inbox_input_required(
    session_store,
    session_factory,
    monkeypatch,
):
    async def _answered_immediately(**kwargs):
        return {
            "cancelled": False,
            "responses": [
                {
                    "question": "Which color?",
                    "answer": "Blue",
                    "is_other": False,
                },
            ],
        }

    monkeypatch.setattr(
        clarify_module,
        "_wait_for_response",
        _answered_immediately,
    )

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="test-agent",
    )

    raw = await clarify_module._clarify_handler(
        {
            "questions": [
                {
                    "prompt": "Which color?",
                    "choices": [{"label": "Blue"}, {"label": "Green"}],
                },
            ],
        },
        session_id=session.id,
        session_store=session_store,
        tool_call_id="tc-clarify-1",
    )

    result = json.loads(raw)
    assert result["cancelled"] is False

    async with session_factory() as db:
        row = (
            await db.execute(
                select(InboxItem).where(InboxItem.session_id == session.id)
            )
        ).scalar_one()

    assert row.kind == "input_required"
    assert row.title == "Which color?"
    assert row.payload["tool_call_id"] == "tc-clarify-1"
    assert row.payload["questions"][0]["prompt"] == "Which color?"
    assert row.action_ref["tool_call_id"] == "tc-clarify-1"
