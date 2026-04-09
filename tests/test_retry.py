"""Tests for surogates.harness.retry -- jittered exponential backoff."""

from __future__ import annotations

from surogates.harness.retry import jittered_backoff


class TestJitteredBackoff:
    """Tests for jittered_backoff()."""

    def test_first_attempt_returns_base_delay_range(self) -> None:
        """First attempt (attempt=1) should return delay around base_delay."""
        delay = jittered_backoff(1, base_delay=2.0, max_delay=60.0, jitter_ratio=0.5)
        # base_delay=2.0, exponent=0 -> delay=2.0, jitter in [0, 1.0]
        assert 2.0 <= delay <= 3.0

    def test_second_attempt_doubles_base(self) -> None:
        """Second attempt (attempt=2) should return delay around 2*base_delay."""
        delay = jittered_backoff(2, base_delay=2.0, max_delay=60.0, jitter_ratio=0.5)
        # exponent=1 -> delay=4.0, jitter in [0, 2.0]
        assert 4.0 <= delay <= 6.0

    def test_third_attempt_quadruples_base(self) -> None:
        """Third attempt (attempt=3) should return delay around 4*base_delay."""
        delay = jittered_backoff(3, base_delay=2.0, max_delay=60.0, jitter_ratio=0.5)
        # exponent=2 -> delay=8.0, jitter in [0, 4.0]
        assert 8.0 <= delay <= 12.0

    def test_respects_max_delay(self) -> None:
        """Delay should never exceed max_delay + jitter."""
        delay = jittered_backoff(100, base_delay=2.0, max_delay=10.0, jitter_ratio=0.5)
        # Capped at 10.0, jitter in [0, 5.0]
        assert 10.0 <= delay <= 15.0

    def test_zero_attempt_uses_max_delay_range(self) -> None:
        """attempt=0 should treat exponent as 0 (no negative exponent)."""
        delay = jittered_backoff(0, base_delay=2.0, max_delay=60.0, jitter_ratio=0.5)
        # exponent=max(0, -1)=0 -> delay=2.0, jitter in [0, 1.0]
        assert 2.0 <= delay <= 3.0

    def test_zero_jitter_ratio(self) -> None:
        """jitter_ratio=0 should produce exact exponential delays."""
        delay = jittered_backoff(1, base_delay=2.0, max_delay=60.0, jitter_ratio=0.0)
        assert delay == 2.0

    def test_zero_base_delay_returns_max(self) -> None:
        """base_delay=0 should fall through to max_delay."""
        delay = jittered_backoff(1, base_delay=0.0, max_delay=10.0, jitter_ratio=0.5)
        assert 10.0 <= delay <= 15.0

    def test_decorrelation_across_calls(self) -> None:
        """Successive calls should produce different delays (jitter decorrelation)."""
        delays = [jittered_backoff(2, base_delay=2.0, max_delay=60.0, jitter_ratio=0.5) for _ in range(10)]
        # At least some values should differ
        assert len(set(delays)) > 1

    def test_large_exponent_capped(self) -> None:
        """Exponent >= 63 should use max_delay instead of overflowing."""
        delay = jittered_backoff(65, base_delay=2.0, max_delay=30.0, jitter_ratio=0.0)
        assert delay == 30.0

    def test_always_positive(self) -> None:
        """Delay should always be positive."""
        for attempt in range(0, 10):
            d = jittered_backoff(attempt, base_delay=1.0, max_delay=60.0, jitter_ratio=0.5)
            assert d > 0
