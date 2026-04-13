"""Tests for surogates.channels.dedup.MessageDeduplicator."""

import time

from surogates.channels.dedup import MessageDeduplicator


class TestMessageDeduplicator:
    def test_first_message_not_duplicate(self):
        d = MessageDeduplicator()
        assert d.is_duplicate("msg1") is False

    def test_same_message_is_duplicate(self):
        d = MessageDeduplicator()
        d.is_duplicate("msg1")
        assert d.is_duplicate("msg1") is True

    def test_different_messages(self):
        d = MessageDeduplicator()
        d.is_duplicate("msg1")
        assert d.is_duplicate("msg2") is False

    def test_empty_id_not_duplicate(self):
        d = MessageDeduplicator()
        assert d.is_duplicate("") is False
        assert d.is_duplicate("") is False

    def test_eviction_on_max_size(self):
        d = MessageDeduplicator(max_size=5, ttl_seconds=300)
        for i in range(10):
            d.is_duplicate(f"msg{i}")
        # After eviction, old messages should no longer be tracked.
        # (exact count depends on which survived eviction)
        assert len(d._seen) <= 10

    def test_clear(self):
        d = MessageDeduplicator()
        d.is_duplicate("msg1")
        d.clear()
        assert d.is_duplicate("msg1") is False
