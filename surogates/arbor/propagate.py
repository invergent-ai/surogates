"""Deterministic insight propagation (crash-safe; no LLM in the wake path).

LLM-synthesis backprop (the verbatim port of Arbor
``tree_ops.py:555-571``) arrives in v2 inside tool-call paths; this
concat pass keeps the constraints block fresh even when every LLM call
fails, so a dead executor can never strand the tree's memory.
"""
from __future__ import annotations


def concat_propagate(
    *, node_key: str, insight: str,
    insights: dict[str, str | None],
    parents: dict[str, str],
    cap_chars: int = 1200,
) -> dict[str, str]:
    """Append ``"[from <key>] <insight>"`` to every ancestor's insight.

    Walks the parent chain from ``node_key`` to the root, stamping the
    lesson onto each ancestor. Returns ``{ancestor_key: new_insight}``
    for the caller to persist; mutates ``insights`` in place so a single
    pass over several siblings accumulates correctly. When an ancestor's
    insight would exceed ``cap_chars`` the TAIL is kept — the newest
    lessons matter most.
    """
    insight = (insight or "").strip()
    if not insight:
        return {}
    stamp = f"[from {node_key}] {insight}"
    updates: dict[str, str] = {}
    current = parents.get(node_key)
    while current is not None:
        existing = (insights.get(current) or "").strip()
        merged = f"{existing}\n{stamp}" if existing else stamp
        if len(merged) > cap_chars:
            merged = merged[-cap_chars:]
        updates[current] = merged
        insights[current] = merged
        current = parents.get(current)
    return updates
