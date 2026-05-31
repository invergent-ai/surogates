"""The worker process must boot with no
per-agent queue assumption — shared-mode pods read from the shared
key, helm-mode pods are a deprecated path that still works by
sharing the same single queue (the gate isolates tenants)."""

from __future__ import annotations

import inspect


def test_run_worker_does_not_call_agent_queue_key():
    import surogates.orchestrator.worker as worker

    src = inspect.getsource(worker.run_worker)
    assert "agent_queue_key(" not in src, (
        "run_worker must not call agent_queue_key — "
        "the per-agent queues into a single shared key"
    )


def test_run_worker_constructs_turn_concurrency_gate():
    """Source-level: the per-tenant gate is wired at worker boot so
    the dispatcher's dequeue path can consult it."""
    import surogates.orchestrator.worker as worker

    src = inspect.getsource(worker.run_worker)
    assert "TurnConcurrencyGate(" in src
