"""Unit tests for LLM-synthesis insight backprop (mocked LLM client)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from surogates.arbor.propagate import propagate_insights_llm, synthesize_insight


class _FakeLLM:
    """Minimal OpenAI-style client: returns a canned assistant message."""

    def __init__(self, text="SYNTH: retrieval is the bottleneck"):
        self._text = text
        self.calls = 0

        async def _create(**kwargs):
            self.calls += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=self._text))]
            )

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))


class _RaisingLLM:
    def __init__(self):
        async def _create(**kwargs):
            raise RuntimeError("provider down")

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))


@pytest.mark.asyncio
async def test_synthesize_insight_returns_text():
    out = await synthesize_insight(
        _FakeLLM(), "test-model",
        node_label="ROOT (objective: maximize F1)",
        child_insights=["- [1, done] retrieval helps", "- [2, pruned] tuning lr did not"],
    )
    assert "SYNTH" in out


@pytest.mark.asyncio
async def test_synthesize_insight_fails_open_to_none():
    out = await synthesize_insight(
        _RaisingLLM(), "m", node_label="x", child_insights=["- [1] y"],
    )
    assert out is None


@pytest.mark.asyncio
async def test_synthesize_insight_none_without_children():
    out = await synthesize_insight(_FakeLLM(), "m", node_label="x", child_insights=[])
    assert out is None


class _StubStore:
    """Two-level tree: ROOT <- 1, 2 (children of ROOT)."""

    def __init__(self):
        self.nodes = {
            "ROOT": SimpleNamespace(node_key="ROOT", parent_key=None,
                                    hypothesis="obj", insight=None, status="pending", score=None),
            "1": SimpleNamespace(node_key="1", parent_key="ROOT",
                                 hypothesis="h1", insight="retrieval helps", status="done", score=0.5),
            "2": SimpleNamespace(node_key="2", parent_key="ROOT",
                                 hypothesis="h2", insight="lr tuning fails", status="pruned", score=0.2),
        }
        self.writes: list[tuple[str, str]] = []

    async def list_nodes(self, run_id):
        return list(self.nodes.values())

    async def get_node(self, run_id, key):
        return self.nodes[key]

    async def update_node(self, run_id, key, **fields):
        if "insight" in fields:
            self.writes.append((key, fields["insight"]))
            self.nodes[key].insight = fields["insight"]


@pytest.mark.asyncio
async def test_propagate_insights_llm_synthesizes_ancestors():
    store, llm = _StubStore(), _FakeLLM()
    n = await propagate_insights_llm(store, "run", "1", llm_client=llm, model="m")
    assert n == 1
    assert any(key == "ROOT" and "SYNTH" in ins for key, ins in store.writes)


@pytest.mark.asyncio
async def test_propagate_insights_llm_noop_without_llm():
    store = _StubStore()
    n = await propagate_insights_llm(store, "run", "1", llm_client=None, model=None)
    assert n == 0 and store.writes == []
