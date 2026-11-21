"""The iteration ceiling of a wake: 90 unless the operator sets ``worker.max_iterations_ceiling``.

A session's ``config.max_iterations`` may lower the ceiling, never raise it. Before the setting, the 90 was fixed, so a
deployment that bounds long tasks by their own time limit (a session asking for 250 calls) was still cut at 90.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from surogates.config import WorkerSettings
from surogates.orchestrator.worker import _resolve_iteration_budget


def session(**config) -> SimpleNamespace:
    return SimpleNamespace(id="s", config=config)


def test_the_default_ceiling_is_still_90():
    assert _resolve_iteration_budget(session(max_iterations=250)).max_total == 90
    assert _resolve_iteration_budget(session()).max_total == 90
    assert _resolve_iteration_budget(session(max_iterations=40)).max_total == 40
    assert WorkerSettings().max_iterations_ceiling == 90


@pytest.mark.parametrize(("asked", "got"), [(250, 250), (40, 40), (400, 250), (None, 250)])
def test_a_raised_ceiling_lets_a_session_ask_up_to_it(asked, got):
    cfg = {} if asked is None else {"max_iterations": asked}
    assert _resolve_iteration_budget(session(**cfg), 250).max_total == got


@pytest.mark.parametrize("bad", [0, -5, True, "250", 2.5, None])
def test_an_unusable_ceiling_is_the_default(bad):
    assert _resolve_iteration_budget(session(max_iterations=250), bad).max_total == 90


@pytest.mark.parametrize("bad", [0, True, "x"])
def test_an_unusable_session_value_takes_the_ceiling(bad):
    assert _resolve_iteration_budget(session(max_iterations=bad), 250).max_total == 250


def test_the_ceiling_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("SUROGATES_WORKER_MAX_ITERATIONS_CEILING", "250")
    assert WorkerSettings().max_iterations_ceiling == 250
