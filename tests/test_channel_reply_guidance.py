"""On a messaging channel every reply text reaches the person, and the model
is told how to send nothing.

After a red alert the model had already sent the emergency script beside its
escalate call, then closed the turn with a note to itself ("nothing more to
the person this turn"), and the patient received that note on WhatsApp.  The
silent marker existed but was only ever named in a woken step's context.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from surogates.harness.prompt import PromptBuilder
from surogates.session.events import SILENT_REPLY, EventType
from surogates.session.models import Session
from surogates.session.store import _build_channel_payload
from surogates.tenant.context import TenantContext


def _context(channel: str) -> str:
    now = datetime.now(timezone.utc)
    session = Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel=channel,
        status="active", config={}, created_at=now, updated_at=now,
    )
    tenant = TenantContext(
        org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    return PromptBuilder(tenant, skills=[], session=session, available_tools=set())._context_section()


@pytest.mark.parametrize("channel", ["whatsapp", "slack", "telegram", "teams", "discord"])
def test_a_messaging_channel_is_told_how_to_send_nothing(channel):
    text = _context(channel)
    assert "## What reaches the person" in text
    assert f"end the turn with exactly {SILENT_REPLY}" in text


def test_the_web_chat_is_not_told_about_the_silent_marker():
    assert SILENT_REPLY not in _context("web")


def test_the_advertised_marker_is_what_delivery_drops():
    said = {"message": {"role": "assistant", "content": SILENT_REPLY}}
    assert _build_channel_payload(EventType.LLM_RESPONSE, said, "whatsapp") == {}
