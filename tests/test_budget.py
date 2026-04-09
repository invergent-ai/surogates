"""Tests for surogates.harness.budget.IterationBudget."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from surogates.harness.budget import IterationBudget


class TestIterationBudgetBasics:
    """Core budget arithmetic"""

    def test_initial_state(self):
        budget = IterationBudget(max_total=50)
        assert budget.remaining == 50
        assert budget.max_total == 50
        assert budget.used == 0
        assert not budget.exhausted

    def test_consume_decrements_by_one(self):
        budget = IterationBudget(max_total=10)
        assert budget.consume() is True
        assert budget.remaining == 9
        assert budget.used == 1

    def test_consume_multiple(self):
        budget = IterationBudget(max_total=5)
        for _ in range(5):
            assert budget.consume() is True
        assert budget.exhausted
        assert budget.consume() is False
        assert budget.remaining == 0
        assert budget.used == 5

    def test_refund_gives_back_one(self):
        budget = IterationBudget(max_total=10)
        budget.consume()
        budget.consume()
        budget.consume()
        assert budget.used == 3
        budget.refund()
        assert budget.used == 2
        assert budget.remaining == 8

    def test_refund_does_not_go_negative(self):
        budget = IterationBudget(max_total=10)
        # Refund without consuming — _used stays at 0
        budget.refund()
        assert budget.used == 0
        assert budget.remaining == 10

    def test_default_max_total(self):
        budget = IterationBudget()
        assert budget.max_total == 90

    def test_repr(self):
        budget = IterationBudget(max_total=10)
        r = repr(budget)
        assert "used=0" in r
        assert "max_total=10" in r


class TestIterationBudgetThreadSafety:
    """Concurrent consume from multiple threads."""

    def test_thread_safety_concurrent_consume(self):
        budget = IterationBudget(max_total=1000)
        successes: list[int] = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            local_count = 0
            for _ in range(100):
                if budget.consume():
                    local_count += 1
            successes.append(local_count)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(worker) for _ in range(10)]
            for f in futures:
                f.result()

        # Total consumed should equal exactly max_total.
        assert sum(successes) == 1000
        assert budget.remaining == 0
        assert budget.exhausted

    def test_thread_safety_consume_and_refund(self):
        budget = IterationBudget(max_total=100)
        barrier = threading.Barrier(4)

        def consumer():
            barrier.wait()
            for _ in range(25):
                budget.consume()

        def refunder():
            barrier.wait()
            for _ in range(10):
                budget.refund()

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(consumer) for _ in range(2)]
            futures += [pool.submit(refunder) for _ in range(2)]
            for f in futures:
                f.result()

        # State should be consistent (no crashes, no negative remaining).
        assert budget.remaining >= 0
        assert budget.remaining <= budget.max_total
