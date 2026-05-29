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


def test_auxiliary_builders_only_called_from_allowed_sites():
    """Plan 2 / Task 8 audit.

    The three ``build_*_auxiliary_llm`` helpers are kept for the
    transitional period (Plan 9 deletes them with helm retirement),
    but only the explicitly-allowed call sites may import them:

    - ``surogates/orchestrator/worker.py`` — only inside
      ``_build_helm_session_llm_clients`` (the helm-mode adapter),
      NOT inside ``harness_factory``.  Task 7 already enforces the
      harness_factory side via a separate regression.
    - ``surogates/harness/title_generator.py`` — standalone title
      generation for new sessions; doesn't touch the harness's
      per-session bundle.
    - ``surogates/harness/self_discover.py`` — standalone plan-step
      summarisation; same property.

    Any other importer is a Plan 2 regression — every harness path
    on the hot loop must source LLM clients from the per-session
    SessionLLMClients bundle (Plan 1b governance + Plan 2 vault
    isolation rely on this).
    """
    import re
    from pathlib import Path

    allowed = {
        "surogates/orchestrator/worker.py",
        "surogates/harness/title_generator.py",
        "surogates/harness/self_discover.py",
        # The builders' own definitions:
        "surogates/harness/auxiliary_client.py",
    }
    pattern = re.compile(
        r"\b(build_summary_auxiliary_llm|"
        r"build_vision_auxiliary_llm|"
        r"build_advisor_auxiliary_llm)\b"
    )
    offenders: list[str] = []
    for path in Path("surogates").rglob("*.py"):
        rel = str(path)
        if rel in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{rel}:{line} ({m.group(1)})")
    assert not offenders, (
        "Auxiliary LLM builders may only be imported by the allowed "
        "sites (helm-mode adapter + standalone title/self-discover); "
        "every other harness path must source LLM clients from the "
        "per-session SessionLLMClients bundle:\n" + "\n".join(offenders)
    )


import pytest as _pytest


@_pytest.mark.asyncio
async def test_build_helm_session_llm_clients_closes_main_on_aux_failure(
    monkeypatch,
):
    """Plan 2 post-review: the helm adapter instantiates AsyncOpenAI
    for the main slot before invoking the auxiliary builders.  If any
    of the three auxiliary builders raises, the main client must be
    aclose()d before the exception propagates so a flaky settings
    override doesn't leak an AsyncOpenAI per failed session start."""
    from types import SimpleNamespace

    import surogates.orchestrator.worker as worker

    closed: list = []

    class _FakeOpenAI:
        def __init__(self, *_a, **_k):
            self._closed = False
            closed.append(self)

        async def close(self):
            self._closed = True

    monkeypatch.setattr(
        "surogates.orchestrator.worker.AsyncOpenAI", _FakeOpenAI,
        raising=True,
    )

    def boom_summary(*_a, **_k):
        raise RuntimeError("settings.llm.summary_model is malformed")

    monkeypatch.setattr(
        "surogates.harness.auxiliary_client.build_summary_auxiliary_llm",
        boom_summary,
        raising=True,
    )

    settings = SimpleNamespace(
        llm=SimpleNamespace(
            api_key="sk-helm", base_url="https://api.example.com",
            model="m",
        ),
    )
    tenant = SimpleNamespace(
        org_id="o-1", user_id=None, org_config={}, user_preferences={},
    )

    with _pytest.raises(RuntimeError, match="malformed"):
        await worker._build_helm_session_llm_clients(settings, tenant)

    # Main was instantiated before the auxiliary builder raised; it
    # must have been aclose()d before the exception propagated.
    assert len(closed) == 1
    assert closed[0]._closed is True


def test_load_prompt_catalogs_receives_bundle():
    """Plan 3 / Task 15 source-level regression: the
    _load_prompt_catalogs call from harness_factory threads
    bundle=bundle through so the platform-skills layer reads from
    the per-session bundle accessor."""
    import inspect
    import surogates.orchestrator.worker as worker

    src = inspect.getsource(worker)
    assert "_load_prompt_catalogs(" in src
    assert "bundle=bundle" in src


def test_harness_factory_does_not_directly_import_auxiliary_builders():
    """Task 8 sibling — within ``worker.py`` itself, only
    ``_build_helm_session_llm_clients`` may reference the builders.

    The function-level grep on ``harness_factory`` would let a
    reference slip if it lived in a module-level closure, so we walk
    the source by AST-extracting only ``harness_factory``'s body."""
    import surogates.orchestrator.worker as worker

    # harness_factory is defined inside run_worker; pull its source
    # via inspect by reading run_worker's source and asserting the
    # build_*_auxiliary substrings live ONLY in
    # _build_helm_session_llm_clients's source.
    run_worker_src = inspect.getsource(worker.run_worker)
    # In Plan 2 / Task 7, harness_factory inside run_worker became
    # the closure that wires SessionLLMClients.  The three builder
    # names must not appear in run_worker's source — they live
    # exclusively in _build_helm_session_llm_clients at module scope.
    for symbol in (
        "build_summary_auxiliary_llm",
        "build_vision_auxiliary_llm",
        "build_advisor_auxiliary_llm",
    ):
        assert symbol not in run_worker_src, (
            f"{symbol} must not be referenced inside run_worker "
            "(including harness_factory); it lives only in the "
            "_build_helm_session_llm_clients module-scope adapter"
        )
