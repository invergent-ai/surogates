"""The worker must honour the per-agent iteration cap the spawn path computed.

Four separate producers derive a child's iteration allowance and write it into
the session config -- ``harness/agent_resolver.py`` (clamped to 30),
``tools/builtin/coordinator.py``, ``tools/builtin/delegate.py`` and
``tasks/spawn.py``. Children are separate enqueued sessions, so the value has
to survive to the worker that wakes them.

It did not: ``grep -rn max_iterations surogates/orchestrator/`` returned
nothing and every session got a flat ``IterationBudget(max_total=90)``. A
cheap agent capped at 5 could burn 90 iterations; the carefully-derived cap
was dead config.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from surogates.orchestrator.worker import (
    _DEFAULT_MAX_ITERATIONS,
    _resolve_iteration_budget,
)


def _session(config: dict | None) -> SimpleNamespace:
    return SimpleNamespace(config=config)


def test_config_cap_is_honoured():
    budget = _resolve_iteration_budget(_session({"max_iterations": 5}))
    assert budget.max_total == 5


def test_absent_cap_falls_back_to_the_platform_default():
    assert _resolve_iteration_budget(_session({})).max_total == (
        _DEFAULT_MAX_ITERATIONS
    )


def test_missing_config_falls_back_to_the_platform_default():
    assert _resolve_iteration_budget(_session(None)).max_total == (
        _DEFAULT_MAX_ITERATIONS
    )


def test_cap_cannot_exceed_the_platform_ceiling():
    """The config is agent-supplied; it may lower the ceiling, never raise it."""
    budget = _resolve_iteration_budget(_session({"max_iterations": 10_000}))
    assert budget.max_total == _DEFAULT_MAX_ITERATIONS


@pytest.mark.parametrize("bad", [0, -1, "thirty", None, 3.5, [], {}])
def test_unusable_values_fall_back_rather_than_stall_the_session(bad):
    """A malformed cap must not produce a zero or negative budget.

    ``IterationBudget(max_total=0)`` is exhausted on construction, so the
    session would go straight to a final summary having done nothing.
    """
    budget = _resolve_iteration_budget(_session({"max_iterations": bad}))
    assert budget.max_total == _DEFAULT_MAX_ITERATIONS


def test_boolean_is_not_treated_as_an_integer():
    """``True`` is an int in Python; a config that says ``true`` is malformed,
    and a budget of 1 would silently cripple the session."""
    budget = _resolve_iteration_budget(_session({"max_iterations": True}))
    assert budget.max_total == _DEFAULT_MAX_ITERATIONS


def test_the_returned_budget_is_usable():
    budget = _resolve_iteration_budget(_session({"max_iterations": 2}))
    assert budget.consume() is True
    assert budget.consume() is True
    assert budget.consume() is False
    assert budget.exhausted is True
