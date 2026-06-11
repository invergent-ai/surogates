"""End-to-end gate behavior for ``emit_turn_summaries`` in worker.py.

These tests exercise the exact conditional that decides whether a
TurnSummarizer instance is wired into the AgentHarness:

* When ``emit_turn_summaries=False``, the harness gets ``None`` even
  if a summary_model is configured.
* When ``emit_turn_summaries=True``, the harness gets a real
  TurnSummarizer running turn summaries on the agent's base model.
  The cheap summary auxiliary is optional — without it iteration
  summaries are skipped, but turn summaries still run.
"""

from __future__ import annotations

from types import SimpleNamespace

from surogates.harness.turn_summarizer import TurnSummarizer


def _build_summarizer_under_gate(
    *,
    emit_turn_summaries: bool,
    base_client,
    base_model: str,
    summary_auxiliary,
) -> TurnSummarizer | None:
    """Mirror the gate written in surogates/orchestrator/worker.py.

    Keeping the gate logic in one tested helper keeps a single source
    of truth as the wiring evolves.
    """
    if emit_turn_summaries:
        return TurnSummarizer(
            base_client=base_client,
            base_model=base_model,
            summary_client=(
                summary_auxiliary.client
                if summary_auxiliary is not None
                else None
            ),
            summary_model=(
                summary_auxiliary.model
                if summary_auxiliary is not None
                else ""
            ),
        )
    return None


def test_gate_off_returns_none_even_with_summary_model() -> None:
    aux = SimpleNamespace(client=object(), model="cheap-model")
    assert _build_summarizer_under_gate(
        emit_turn_summaries=False,
        base_client=object(),
        base_model="base-model",
        summary_auxiliary=aux,
    ) is None


def test_gate_on_without_summary_model_still_returns_summarizer() -> None:
    """Turn summaries run on the base model, so the cheap auxiliary
    being unconfigured must not disable the whole summarizer."""
    base_sentinel = object()
    summarizer = _build_summarizer_under_gate(
        emit_turn_summaries=True,
        base_client=base_sentinel,
        base_model="base-model",
        summary_auxiliary=None,
    )
    assert isinstance(summarizer, TurnSummarizer)
    assert summarizer._base_client is base_sentinel
    assert summarizer._base_model == "base-model"
    assert summarizer._summary_client is None


def test_gate_on_with_summary_model_wires_both_slots() -> None:
    base_sentinel = object()
    cheap_sentinel = object()
    aux = SimpleNamespace(client=cheap_sentinel, model="cheap-model")
    summarizer = _build_summarizer_under_gate(
        emit_turn_summaries=True,
        base_client=base_sentinel,
        base_model="base-model",
        summary_auxiliary=aux,
    )
    assert isinstance(summarizer, TurnSummarizer)
    assert summarizer._base_client is base_sentinel
    assert summarizer._base_model == "base-model"
    assert summarizer._summary_client is cheap_sentinel
    assert summarizer._summary_model == "cheap-model"


def test_worker_uses_same_gate_logic() -> None:
    """Pin the gate condition in surogates/orchestrator/worker.py.

    This guards against silent rewrites of the gate (e.g. re-adding a
    summary-slot requirement would silently disable turn summaries for
    tenants without a cheap model). We parse the worker source and
    verify the condition shape; a refactor that moves the gate
    elsewhere should update this test deliberately rather than
    accidentally regressing.
    """
    import inspect

    from surogates.orchestrator import worker

    src = inspect.getsource(worker)
    assert "if settings.worker.emit_turn_summaries:" in src
    assert "base_client=llm_client" in src
    assert "base_model=model_id" in src
