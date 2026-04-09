"""Tests for surogates.channels.source and surogates.channels.web."""

from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

import pytest

from surogates.channels.source import SessionSource, build_session_key
from surogates.channels.web import format_sse_event
from surogates.session.models import Event


# =========================================================================
# SessionSource + build_session_key
# =========================================================================


class TestBuildSessionKey:
    """Deterministic routing key generation."""

    def test_dm_key(self):
        source = SessionSource(
            platform="slack",
            chat_id="C123",
            chat_type="dm",
            user_id="U456",
        )
        key = build_session_key(source)
        assert key == "agent:slack:dm:C123"

    def test_group_key(self):
        source = SessionSource(
            platform="teams",
            chat_id="CONV_ID",
            chat_type="group",
            user_id="USER1",
        )
        key = build_session_key(source)
        assert key == "agent:teams:group:CONV_ID"

    def test_thread_key(self):
        source = SessionSource(
            platform="telegram",
            chat_id="CHAT_ID",
            chat_type="group",
            user_id="USER1",
            thread_id="THREAD_99",
        )
        key = build_session_key(source)
        assert key == "agent:telegram:group:CHAT_ID:THREAD_99"

    def test_per_user_groups(self):
        source = SessionSource(
            platform="teams",
            chat_id="CONV_ID",
            chat_type="group",
            user_id="USER1",
        )
        key = build_session_key(source, per_user_groups=True)
        assert key == "agent:teams:group:CONV_ID:USER1"

    def test_per_user_groups_with_thread(self):
        source = SessionSource(
            platform="slack",
            chat_id="C100",
            chat_type="channel",
            user_id="U200",
            thread_id="T300",
        )
        key = build_session_key(source, per_user_groups=True)
        assert key == "agent:slack:channel:C100:U200:T300"

    def test_dm_ignores_per_user_groups(self):
        source = SessionSource(
            platform="web",
            chat_id="CHAT1",
            chat_type="dm",
            user_id="USER1",
        )
        key_default = build_session_key(source)
        key_per_user = build_session_key(source, per_user_groups=True)
        # DMs don't add user_id (chat_type is not group/channel).
        assert key_default == key_per_user == "agent:web:dm:CHAT1"

    def test_thread_without_per_user_groups(self):
        source = SessionSource(
            platform="slack",
            chat_id="C1",
            chat_type="dm",
            user_id="U1",
            thread_id="T1",
        )
        key = build_session_key(source)
        # Threads are always appended.
        assert key == "agent:slack:dm:C1:T1"


class TestSessionSourceImmutability:
    """SessionSource is frozen."""

    def test_frozen(self):
        source = SessionSource(
            platform="web",
            chat_id="c",
            chat_type="dm",
            user_id="u",
        )
        with pytest.raises(AttributeError):
            source.platform = "changed"  # type: ignore[misc]


# =========================================================================
# format_sse_event
# =========================================================================


class TestFormatSSEEvent:
    """SSE event formatting."""

    def test_format_sse_event_basic(self):
        event = Event(
            id=42,
            session_id=uuid4(),
            type="llm.response",
            data={"content": "Hello!"},
            created_at=datetime.now(),
        )
        sse = format_sse_event(event)
        assert sse["event"] == "llm.response"
        assert sse["id"] == "42"
        data = json.loads(sse["data"])
        assert data["content"] == "Hello!"

    def test_format_sse_event_none_id(self):
        event = Event(
            id=None,
            session_id=uuid4(),
            type="status.update",
            data={"status": "running"},
        )
        sse = format_sse_event(event)
        assert sse["id"] == ""

    def test_format_sse_event_empty_data(self):
        event = Event(
            id=1,
            session_id=uuid4(),
            type="ping",
            data={},
        )
        sse = format_sse_event(event)
        assert json.loads(sse["data"]) == {}
