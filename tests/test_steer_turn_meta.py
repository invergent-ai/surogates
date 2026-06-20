"""LLM streaming metadata can use a turn-local iteration index."""
from __future__ import annotations

from surogates.harness.llm_call import _stamp_turn_meta


def test_stamp_turn_meta_accepts_turn_local_iteration_index():
    payload = {"content": "delta"}
    out = _stamp_turn_meta(
        payload,
        iteration=9,
        turn_id="turn-2",
        iteration_index=0,
    )
    assert out is payload
    assert out == {
        "content": "delta",
        "turn_id": "turn-2",
        "iteration_index": 0,
    }


def test_stamp_turn_meta_falls_back_to_iteration_when_no_override():
    payload = {}
    _stamp_turn_meta(payload, iteration=3, turn_id="turn-1")
    assert payload == {"turn_id": "turn-1", "iteration_index": 2}


def test_stamp_turn_meta_noops_without_turn_id_even_with_override():
    payload = {}
    _stamp_turn_meta(payload, iteration=3, turn_id=None, iteration_index=0)
    assert payload == {}
