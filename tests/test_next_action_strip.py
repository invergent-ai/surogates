"""Tests for the ``<next_action>`` footer strip and its delivery wiring.

Messaging channels (Telegram, Slack, ...) deliver assistant text raw, so
the session store strips the ``<next_action>`` footer server-side before
enqueueing.  These tests pin the strip semantics to the web SDK parser's
behaviour (``sdk/agent-chat-react/src/lib/next-action.ts``): every block
removed, malformed blocks removed, footer-only messages strip to "".
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from surogates.harness.next_action import strip_next_action_blocks

FOOTER = '<next_action complexity="low" summary="hide">\ndone\n</next_action>'


class TestStripNextActionBlocks:
    def test_strips_trailing_footer(self):
        text = f"Here is your answer.\n\n{FOOTER}"
        assert strip_next_action_blocks(text) == "Here is your answer."

    def test_no_footer_passthrough_is_identity(self):
        text = "Plain reply with <other_tag>markup</other_tag> kept."
        assert strip_next_action_blocks(text) is text

    def test_footer_only_message_strips_to_empty(self):
        # A "done"-only turn must become nothing-to-deliver, not an
        # empty-looking Telegram message.
        assert strip_next_action_blocks(FOOTER) == ""

    def test_strips_multiple_blocks(self):
        text = f"part one\n{FOOTER}\npart two\n{FOOTER}"
        cleaned = strip_next_action_blocks(text)
        assert "next_action" not in cleaned
        assert "part one" in cleaned and "part two" in cleaned

    def test_strips_block_without_attributes(self):
        # The parse regex in expert_routing requires complexity=; the
        # strip must remove malformed blocks too.
        text = "answer\n<next_action>\nI'll continue.\n</next_action>"
        assert strip_next_action_blocks(text) == "answer"

    def test_strips_dangling_unclosed_block(self):
        # Token-limit truncation mid-footer must not leak the open tag.
        text = 'answer\n<next_action complexity="low" summary="hide">\nI will'
        assert strip_next_action_blocks(text) == "answer"

    def test_case_insensitive(self):
        text = 'answer\n<NEXT_ACTION COMPLEXITY="LOW">done</NEXT_ACTION>'
        assert strip_next_action_blocks(text) == "answer"

    def test_preserves_interior_whitespace_and_body(self):
        text = f"line one\n\n  indented code\n\nline two\n\n{FOOTER}"
        assert (
            strip_next_action_blocks(text)
            == "line one\n\n  indented code\n\nline two"
        )

    def test_many_unclosed_openers_is_linear_not_quadratic(self):
        # ReDoS regression: any block regex of the shape
        # ``<next_action[^>]*>...</next_action>`` — lazy OR tempered —
        # walks from every opener toward a closer that never arrives:
        # O(openers x length), measured ~300s at 50k openers.  This
        # input comes from model output, which can echo prompt-injected
        # garbage verbatim — it must never wedge the delivery worker.
        # The closer-driven scan is linear by construction.
        text = "answer\n" + '<next_action complexity="low" summary="hide">' * 50_000
        start = time.perf_counter()
        cleaned = strip_next_action_blocks(text)
        elapsed = time.perf_counter() - start
        assert cleaned == "answer"
        assert elapsed < 2.0, f"strip took {elapsed:.2f}s — backtracking regression"


class TestEnqueueChannelDeliveryStrip:
    """The store-level wiring: footer stripped before the outbox row."""

    @staticmethod
    def _make_store(channel: str = "telegram"):
        from surogates.session.store import SessionStore

        added: list = []
        db = MagicMock()
        db.add = added.append
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        factory_calls: list = []

        class _Ctx:
            async def __aenter__(self):
                factory_calls.append(1)
                return db

            async def __aexit__(self, *exc):
                return False

        store = SessionStore(session_factory=lambda: _Ctx())
        session_id = uuid4()
        # Pre-populate the channel cache so no session lookup happens.
        store._channel_cache[session_id] = (channel, {"telegram_chat_id": "42"})
        return store, session_id, added, factory_calls

    async def test_strips_footer_before_enqueue(self):
        from surogates.session.events import EventType

        store, sid, added, _ = self._make_store()
        await store._enqueue_channel_delivery(
            sid, 7, EventType.LLM_RESPONSE,
            {"message": {"content": f"Here you go.\n\n{FOOTER}"}},
        )
        assert len(added) == 1
        assert added[0].payload["content"] == "Here you go."

    async def test_footer_only_message_not_enqueued(self):
        from surogates.session.events import EventType

        store, sid, added, factory_calls = self._make_store()
        await store._enqueue_channel_delivery(
            sid, 8, EventType.LLM_RESPONSE, {"message": {"content": FOOTER}},
        )
        # Strips to "" -> nothing-to-deliver: no row, no DB session opened.
        assert added == []
        assert factory_calls == []

    async def test_web_channel_keeps_footer(self):
        from surogates.session.events import EventType

        # Web sessions return before payload building — the SSE path
        # delivers the raw event and the web SDK strips client-side.
        store, sid, added, factory_calls = self._make_store(channel="web")
        await store._enqueue_channel_delivery(
            sid, 9, EventType.LLM_RESPONSE,
            {"message": {"content": f"hi\n{FOOTER}"}},
        )
        assert added == []
        assert factory_calls == []

    async def test_list_shaped_content_passes_through_unchanged(self):
        from surogates.session.events import EventType

        # Non-str content skips the strip (isinstance guard) and keeps
        # the pre-existing passthrough behaviour.
        store, sid, added, _ = self._make_store()
        blocks = [{"type": "text", "text": "hi"}]
        await store._enqueue_channel_delivery(
            sid, 10, EventType.LLM_RESPONSE, {"message": {"content": blocks}},
        )
        assert len(added) == 1
        assert added[0].payload["content"] == blocks
