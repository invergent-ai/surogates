"""Unit tests for the deterministic harvest fold (stubbed task rows).

``fold_task_into_node`` takes the node_key explicitly (the harvest hook
knows it from the ``idea_nodes.task_id`` -> ``tasks.id`` join, and
``record_from_task`` looks it up). Metadata supplies only score / insight
/ result; a completed task missing those folds as done-with-null-score
(via LLM extraction when a model is available, else fail-open).
"""
from __future__ import annotations

import pytest

from surogates.harness.loop_arbor import fold_task_into_node


class _CompletedTask:
    status = "done"
    result = "long prose report"
    result_metadata = {
        "score": 0.42, "insight": "lesson", "result": "ok", "branch": "b",
    }


class _FailedTask:
    status = "failed"
    result = None
    result_metadata = None


class _StubStore:
    def __init__(self):
        self.updates: list[tuple[str, dict]] = []

    async def update_node(self, run_id, key, **fields):
        self.updates.append((key, fields))

    async def list_nodes(self, run_id):
        return []


@pytest.mark.asyncio
async def test_fold_uses_metadata_verbatim():
    store = _StubStore()
    out = await fold_task_into_node(
        store, "run", "1", _CompletedTask(), llm_client=None, model=None,
    )
    key, fields = store.updates[0]
    assert key == "1"
    assert fields["status"] == "done"
    assert fields["score"] == 0.42
    assert fields["insight"] == "lesson"
    assert out["folded"] == "1"


@pytest.mark.asyncio
async def test_fold_failed_task_spends_budget_as_failed():
    store = _StubStore()
    await fold_task_into_node(
        store, "run", "1", _FailedTask(), llm_client=None, model=None,
    )
    _key, fields = store.updates[0]
    assert fields["status"] == "failed"
    assert "crash" in fields["insight"].lower() or "fail" in fields["insight"].lower()


@pytest.mark.asyncio
async def test_fold_completed_without_report_and_no_model_falls_open():
    class _BareCompleted:
        status = "done"
        result = "some output without structured metadata"
        result_metadata = {}

    store = _StubStore()
    await fold_task_into_node(
        store, "run", "1", _BareCompleted(), llm_client=object(), model=None,
    )
    _key, fields = store.updates[0]
    assert fields["status"] == "done"
    assert fields["score"] is None
