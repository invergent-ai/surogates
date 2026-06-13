"""Insight propagation for research runs.

Two layers: a deterministic concat-propagate used by the wake-time
harvest (crash-safe, no LLM), and an LLM-synthesis backprop (port of
Arbor ``tree_ops.py:518-597``) used inside the tool-call paths where an
LLM client is available. The synthesis fails open to the concat result,
so a dead executor or provider outage can never strand the tree.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


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


_SYNTH_SYSTEM = (
    "You are a research insight synthesizer. Given insights from child "
    "experiments, produce a concise summary that captures the key learnings, "
    "patterns, and actionable conclusions. Be specific about what works and "
    "what doesn't. Keep it under 200 words."
)


async def synthesize_insight(
    llm_client, model: str | None, *, node_label: str, child_insights: list[str],
) -> str | None:
    """LLM-synthesize one node's insight from its children (port of Arbor's
    ``tree_ops.py:533-571``).

    Fails open to ``None``: no client/model, no children, or any provider
    error returns ``None`` so the caller keeps the prior (concat) insight.
    """
    if llm_client is None or not model or not child_insights:
        return None
    joined = "\n".join(child_insights)
    try:
        resp = await llm_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYNTH_SYSTEM},
                {"role": "user", "content": (
                    f"{node_label}\n\nChildren insights:\n{joined}\n\n"
                    "Synthesize these into a concise research insight."
                )},
            ],
            max_tokens=600, temperature=0.0,
        )
        msg = resp.choices[0].message
        text = (getattr(msg, "content", None) or "").strip()
        return text or None
    except Exception:
        logger.warning("research: insight synthesis failed (continuing)", exc_info=True)
        return None


async def propagate_insights_llm(
    store, run_id, node_key: str, *, llm_client, model: str | None,
) -> int:
    """Walk ``node_key``'s ancestors (parent -> root); at each, synthesize an
    insight from its children's insights and persist it. Returns the number of
    ancestors updated. No-ops (returns 0) when synthesis is unavailable."""
    if llm_client is None or not model:
        return 0
    nodes = await store.list_nodes(run_id)
    by_key = {n.node_key: n for n in nodes}
    children: dict[str, list] = {}
    for node in nodes:
        if node.parent_key:
            children.setdefault(node.parent_key, []).append(node)
    if node_key not in by_key:
        return 0

    updated = 0
    cur = by_key[node_key].parent_key
    while cur is not None and cur in by_key:
        ancestor = by_key[cur]
        parts: list[str] = []
        for child in children.get(cur, []):
            if child.insight:
                score = f" (score={child.score})" if child.score is not None else ""
                parts.append(f"- [{child.node_key}, {child.status}{score}]: {child.insight}")
        if parts:
            label = (
                "This is the ROOT node — produce a global research insight summary."
                if ancestor.parent_key is None
                else f"This is node {ancestor.node_key} (hypothesis: {ancestor.hypothesis})."
            )
            summary = await synthesize_insight(
                llm_client, model, node_label=label, child_insights=parts,
            )
            if summary:
                await store.update_node(run_id, cur, insight=summary)
                updated += 1
        cur = ancestor.parent_key
    return updated
