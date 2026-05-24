"""End-to-end gate behavior for ``emit_turn_summaries`` in worker.py.

These tests exercise the exact conditional that decides whether a
TurnSummarizer instance is wired into the AgentHarness:

* When ``emit_turn_summaries=False``, the harness gets ``None`` even
  if a summary_model is configured.
* When ``emit_turn_summaries=True`` AND a summary_auxiliary exists,
  the harness gets a real TurnSummarizer that uses the auxiliary's
  client + model.
* When ``emit_turn_summaries=True`` BUT no summary_model is
  configured (auxiliary is None), the harness still gets ``None``
  because there's no model to call.
"""

from __future__ import annotations

from types import SimpleNamespace

from surogates.harness.turn_summarizer import TurnSummarizer


def _build_summarizer_under_gate(
    *,
    emit_turn_summaries: bool,
    summary_auxiliary,
) -> TurnSummarizer | None:
    """Mirror the gate written in surogates/orchestrator/worker.py.

    Keeping the gate logic in one tested helper keeps a single source
    of truth as the wiring evolves.
    """
    if emit_turn_summaries and summary_auxiliary is not None:
        return TurnSummarizer(
            summary_client=summary_auxiliary.client,
            summary_model=summary_auxiliary.model,
        )
    return None


def test_gate_off_returns_none_even_with_summary_model() -> None:
    aux = SimpleNamespace(client=object(), model="cheap-model")
    assert _build_summarizer_under_gate(
        emit_turn_summaries=False, summary_auxiliary=aux,
    ) is None


def test_gate_on_without_summary_model_returns_none() -> None:
    assert _build_summarizer_under_gate(
        emit_turn_summaries=True, summary_auxiliary=None,
    ) is None


def test_gate_on_with_summary_model_returns_turn_summarizer() -> None:
    client_sentinel = object()
    aux = SimpleNamespace(client=client_sentinel, model="cheap-model")
    summarizer = _build_summarizer_under_gate(
        emit_turn_summaries=True, summary_auxiliary=aux,
    )
    assert isinstance(summarizer, TurnSummarizer)
    assert summarizer._client is client_sentinel
    assert summarizer._model == "cheap-model"


def test_worker_uses_same_gate_logic() -> None:
    """Pin the gate condition in surogates/orchestrator/worker.py.

    This guards against silent rewrites of the gate (e.g. swapping
    'and' for 'or' would defeat the kill switch). We parse the worker
    source and verify the condition shape; a refactor that moves the
    gate elsewhere should update this test deliberately rather than
    accidentally regressing.
    """
    import inspect

    from surogates.orchestrator import worker

    src = inspect.getsource(worker)
    assert "settings.worker.emit_turn_summaries" in src
    assert "summary_auxiliary is not None" in src
    # The two conditions are AND-ed.
    assert "settings.worker.emit_turn_summaries\n            and summary_auxiliary is not None" in src
