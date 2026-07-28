"""Integration tests for eager browser-state pruning.

These exercise the *composed* behaviour: the real ``ContextCompressor`` running
``prune_stale_browser_states`` over a realistically-shaped browser session, and
the downstream effect on ``should_compress`` (the token-size gate that decides
compaction frequency).  Browser state payloads mirror the real
``browser_get_state`` markdown shape (header plus one line per element) but
contain no real page data.
"""

from __future__ import annotations

import json

from surogates.harness.context import (
    _PRUNED_TOOL_PLACEHOLDER,
    ContextCompressor,
)


def _realistic_state(step: int, n_nodes: int = 130) -> str:
    """A browser_get_state result shaped like the real tool output.

    ~130 elements lands around ~1K tokens, matching the measured production
    average for a markdown snapshot.
    """
    roles = ["button", "link", "textbox", "checkbox", "searchbox", "tab"]
    lines = [
        f"# View {step}",
        f"https://example.test/page/{step}",
        "viewport 1280x800",
        "",
    ]
    for i in range(n_nodes):
        if i % 7 == 0:
            lines.append(f"## section {i} on view {step}")
        elif i % 3 == 0:
            lines.append(f"element {i} on view {step} — some visible label text here")
        else:
            lines.append(
                f'- {roles[i % len(roles)]} @e{i} "element {i} on view {step}"'
            )
    return "\n".join(lines)


def _browser_session(n_states: int) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "You are a browser agent."}]
    msgs.append({"role": "user", "content": "Complete the multi-step flow."})
    for i in range(1, n_states + 1):
        msgs.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {
                            "name": "browser_get_state",
                            "arguments": '{"interactive_only": true}',
                        },
                    }
                ],
            }
        )
        msgs.append(
            {"role": "tool", "tool_call_id": f"c{i}", "content": _realistic_state(i)}
        )
    return msgs


def _small_ctx_compressor(**kw) -> ContextCompressor:
    # Shrink the declared context window so the compaction threshold is small
    # enough to reach in a test (threshold = 50% of context_window).  Sized
    # against the markdown snapshot: 12 states estimate ~16K tokens unpruned
    # and ~3K pruned, so a 12K threshold sits clear of both.
    return ContextCompressor(
        "claude-opus-4-7",
        quiet_mode=True,
        model_overrides={"claude-opus-4-7": {"context_window": 24_000}},
        **kw,
    )


def _chars(messages) -> int:
    return sum(len(m.get("content") or "") for m in messages if isinstance(m.get("content"), str))


def test_prune_keeps_current_state_and_clears_the_rest_on_real_shape():
    c = _small_ctx_compressor(browser_state_keep_last=2)
    before = _browser_session(12)
    after = c.prune_stale_browser_states(before)

    results = [m for m in after if m.get("role") == "tool"]
    cleared = [r for r in results if r["content"] == _PRUNED_TOOL_PLACEHOLDER]
    kept = [r for r in results if r["content"] != _PRUNED_TOOL_PLACEHOLDER]

    assert len(cleared) == 10           # 12 states, keep last 2
    assert len(kept) == 2
    # The two kept states are the most recent ones (the current page view).
    assert "https://example.test/page/12" in kept[-1]["content"]
    # Substantial payload reduction.
    assert _chars(after) < 0.35 * _chars(before)


def test_prune_drops_context_below_compaction_threshold():
    """Lever 2: eager pruning lowers the token estimate that gates compaction,
    so a session that WOULD compact no longer needs to."""
    c = _small_ctx_compressor(browser_state_keep_last=2)
    session = _browser_session(12)

    # Before pruning the browser states push the context over the threshold.
    assert c.should_compress(session) is True
    # After pruning stale states it no longer needs compaction.
    pruned = c.prune_stale_browser_states(session)
    assert c.should_compress(pruned) is False


def test_prune_preserves_every_tool_pairing():
    c = _small_ctx_compressor(browser_state_keep_last=2)
    before = _browser_session(12)
    after = c.prune_stale_browser_states(before)

    call_ids = {
        tc["id"]
        for m in after
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    }
    result_ids = {m["tool_call_id"] for m in after if m.get("role") == "tool"}
    # Every assistant tool_call still has its matching tool result and vice
    # versa — pruning content never orphans a pairing.
    assert call_ids == result_ids
    assert len(result_ids) == 12


def test_disabled_leaves_full_session_intact():
    c = _small_ctx_compressor(prune_browser_state=False)
    session = _browser_session(12)
    assert c.prune_stale_browser_states(session) == session
