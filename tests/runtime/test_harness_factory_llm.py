"""Tests for harness_factory's per-session LLM bundle wiring.

Plan 2 / Task 7.  Every session-bootstrap path builds a fresh
SessionLLMClients from the resolved AgentRuntimeContext; the
process-wide AsyncOpenAI instance from Plan 1 is gone.
"""

from __future__ import annotations

import inspect


def test_harness_factory_uses_build_session_llm_clients():
    """Source-level regression — the wiring lives inside an
    integration-heavy function, so a string scan keeps the test
    fast.  Plan 2 Task 8 (audit) verifies no other call site
    survives outside the helm-mode transitional helper."""
    import surogates.orchestrator.worker as worker

    # harness_factory is a closure defined inside run_worker; grep
    # the whole module source.  We separately verify nothing in
    # run_worker constructs a process-wide AsyncOpenAI (next test).
    module_src = inspect.getsource(worker)
    assert "build_session_llm_clients" in module_src, (
        "worker module must construct SessionLLMClients per session"
    )


def test_run_worker_does_not_construct_process_wide_asyncopenai():
    """The Plan 1 worker built a single AsyncOpenAI(**llm_kwargs) at
    startup and threaded it through every harness_factory call.
    Plan 2 deletes that — the per-session bundle takes over."""
    import surogates.orchestrator.worker as worker

    src = inspect.getsource(worker.run_worker)
    assert "AsyncOpenAI(" not in src, (
        "run_worker must not construct a process-wide AsyncOpenAI; "
        "the per-session SessionLLMClients takes over"
    )
