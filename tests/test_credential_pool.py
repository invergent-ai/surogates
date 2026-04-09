"""Tests for surogates.harness.credentials -- credential pool for LLM resilience."""

from __future__ import annotations

import time

from surogates.harness.credentials import (
    EXHAUSTED_TTL_429,
    EXHAUSTED_TTL_DEFAULT,
    STATUS_EXHAUSTED,
    STATUS_OK,
    CredentialPool,
    PooledCredential,
)


def _make_cred(id: str, priority: int = 0, label: str = "") -> PooledCredential:
    return PooledCredential(id=id, api_key=f"sk-{id}", priority=priority, label=label)


class TestPooledCredential:
    """Tests for the PooledCredential dataclass."""

    def test_runtime_api_key(self) -> None:
        cred = PooledCredential(id="a", api_key="sk-test")
        assert cred.runtime_api_key == "sk-test"

    def test_runtime_base_url_default_none(self) -> None:
        cred = PooledCredential(id="a", api_key="sk-test")
        assert cred.runtime_base_url is None

    def test_runtime_base_url_custom(self) -> None:
        cred = PooledCredential(id="a", api_key="sk-test", base_url="https://custom.api")
        assert cred.runtime_base_url == "https://custom.api"

    def test_default_status_is_ok(self) -> None:
        cred = PooledCredential(id="a", api_key="sk-test")
        assert cred.status == STATUS_OK


class TestCredentialPool:
    """Tests for the CredentialPool."""

    def test_current_returns_first_available(self) -> None:
        pool = CredentialPool([_make_cred("a"), _make_cred("b")])
        current = pool.current()
        assert current is not None
        assert current.id == "a"

    def test_current_respects_priority(self) -> None:
        pool = CredentialPool([_make_cred("b", priority=2), _make_cred("a", priority=1)])
        current = pool.current()
        assert current is not None
        assert current.id == "a"

    def test_has_available_true(self) -> None:
        pool = CredentialPool([_make_cred("a")])
        assert pool.has_available() is True

    def test_has_available_false_when_empty(self) -> None:
        pool = CredentialPool([])
        assert pool.has_available() is False

    def test_current_returns_none_when_empty(self) -> None:
        pool = CredentialPool([])
        assert pool.current() is None

    def test_mark_exhausted_and_rotate(self) -> None:
        pool = CredentialPool([_make_cred("a"), _make_cred("b")])
        next_cred = pool.mark_exhausted_and_rotate(429, "rate limited")
        assert next_cred is not None
        assert next_cred.id == "b"

    def test_mark_exhausted_last_returns_none(self) -> None:
        pool = CredentialPool([_make_cred("a")])
        next_cred = pool.mark_exhausted_and_rotate(429, "rate limited")
        assert next_cred is None

    def test_rotate_updates_status(self) -> None:
        pool = CredentialPool([_make_cred("a"), _make_cred("b")])
        pool.mark_exhausted_and_rotate(429, "rate limited")
        # After rotation, "a" should be exhausted
        assert pool._entries[0].status == STATUS_EXHAUSTED
        assert pool._entries[0].error_code == 429

    def test_exhausted_credential_skipped(self) -> None:
        pool = CredentialPool([_make_cred("a"), _make_cred("b")])
        pool.mark_exhausted_and_rotate(429, "rate limited")
        current = pool.current()
        assert current is not None
        assert current.id == "b"

    def test_all_exhausted_returns_none(self) -> None:
        pool = CredentialPool([_make_cred("a"), _make_cred("b")])
        pool.mark_exhausted_and_rotate(429)
        pool.mark_exhausted_and_rotate(429)
        assert pool.current() is None
        assert pool.has_available() is False

    def test_exhausted_ttl_429(self) -> None:
        pool = CredentialPool([_make_cred("a"), _make_cred("b")])
        pool.mark_exhausted_and_rotate(429)
        entry = pool._entries[0]
        assert entry.reset_at is not None
        expected_reset = entry.status_at + EXHAUSTED_TTL_429
        assert abs(entry.reset_at - expected_reset) < 1.0

    def test_exhausted_ttl_default_for_402(self) -> None:
        pool = CredentialPool([_make_cred("a"), _make_cred("b")])
        pool.mark_exhausted_and_rotate(402)
        entry = pool._entries[0]
        assert entry.reset_at is not None
        expected_reset = entry.status_at + EXHAUSTED_TTL_DEFAULT
        assert abs(entry.reset_at - expected_reset) < 1.0

    def test_cooldown_expired_resets_to_ok(self) -> None:
        pool = CredentialPool([_make_cred("a")])
        pool.mark_exhausted_and_rotate(429)
        # Manually expire the cooldown
        pool._entries[0] = PooledCredential(
            id="a", api_key="sk-a", status=STATUS_EXHAUSTED,
            status_at=time.time() - 10, reset_at=time.time() - 1,
        )
        current = pool.current()
        assert current is not None
        assert current.id == "a"
        assert current.status == STATUS_OK

    def test_error_message_truncated(self) -> None:
        pool = CredentialPool([_make_cred("a"), _make_cred("b")])
        long_msg = "x" * 1000
        pool.mark_exhausted_and_rotate(429, long_msg)
        entry = pool._entries[0]
        assert entry.error_message is not None
        assert len(entry.error_message) == 500
