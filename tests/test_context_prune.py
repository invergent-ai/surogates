"""Tests for eager pruning of superseded tool results (browser page snapshots).

The agent only ever acts on the *current* browser state; older
``browser_get_state`` snapshots are dead weight that gets replayed on every
subsequent LLM call.  ``prune_superseded_tool_results`` replaces all but the
most recent ``keep_last`` results of the targeted tools with a short
placeholder, cutting input tokens without changing behaviour.
"""

from __future__ import annotations

import copy

from surogates.harness.context import (
    _PRUNED_TOOL_PLACEHOLDER,
    ContextCompressor,
    prune_superseded_tool_results,
)

PLACEHOLDER = _PRUNED_TOOL_PLACEHOLDER
BIG = "<state>" + ("x" * 600) + "</state>"  # well over min_chars
SMALL = "ok"


def _asst_call(call_id: str, name: str, arguments: str = "{}") -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


def _tool_result(call_id: str, content) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _prune(messages, keep_last=2, tool_names=frozenset({"browser_get_state"})):
    return prune_superseded_tool_results(
        messages,
        tool_names=tool_names,
        keep_last=keep_last,
        placeholder=PLACEHOLDER,
        min_chars=200,
    )


def _browser_session(n_states: int) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "sys"}]
    msgs.append({"role": "user", "content": "open the page"})
    for i in range(1, n_states + 1):
        msgs.append(_asst_call(f"c{i}", "browser_get_state"))
        msgs.append(_tool_result(f"c{i}", f"{BIG}#{i}"))
    return msgs


def test_keeps_last_n_states_and_placeholders_older():
    msgs = _browser_session(4)
    pruned, count = _prune(msgs, keep_last=2)

    # 4 states, keep last 2 -> 2 pruned.
    assert count == 2
    results = [m for m in pruned if m.get("role") == "tool"]
    assert results[0]["content"] == PLACEHOLDER  # state #1 superseded
    assert results[1]["content"] == PLACEHOLDER  # state #2 superseded
    assert results[2]["content"] == f"{BIG}#3"   # kept
    assert results[3]["content"] == f"{BIG}#4"   # kept (current)


def test_preserves_pairing_fields_on_pruned_result():
    msgs = _browser_session(3)
    pruned, _ = _prune(msgs, keep_last=1)
    first_result = next(m for m in pruned if m.get("role") == "tool")
    assert first_result["role"] == "tool"
    assert first_result["tool_call_id"] == "c1"
    assert first_result["content"] == PLACEHOLDER


def test_does_not_touch_other_tools():
    msgs = [
        {"role": "system", "content": "sys"},
        _asst_call("f1", "read_file"),
        _tool_result("f1", BIG),  # old, but read_file is not a target
        _asst_call("b1", "browser_get_state"),
        _tool_result("b1", BIG),
        _asst_call("b2", "browser_get_state"),
        _tool_result("b2", BIG),
    ]
    pruned, count = _prune(msgs, keep_last=1)
    # Only 1 browser state is superseded; read_file untouched.
    assert count == 1
    read_result = next(m for m in pruned if m.get("tool_call_id") == "f1")
    assert read_result["content"] == BIG


def test_respects_min_chars():
    msgs = [
        {"role": "system", "content": "sys"},
        _asst_call("b1", "browser_get_state"),
        _tool_result("b1", SMALL),  # below min_chars -> never pruned
        _asst_call("b2", "browser_get_state"),
        _tool_result("b2", BIG),
    ]
    pruned, count = _prune(msgs, keep_last=1)
    assert count == 0
    small_result = next(m for m in pruned if m.get("tool_call_id") == "b1")
    assert small_result["content"] == SMALL


def test_idempotent_does_not_recount_placeholders():
    msgs = _browser_session(4)
    once, count1 = _prune(msgs, keep_last=2)
    twice, count2 = _prune(once, keep_last=2)
    assert count1 == 2
    assert count2 == 0  # already pruned; nothing new to do
    assert twice == once


def test_unpaired_tool_result_left_untouched():
    # A tool result whose call_id has no assistant tool_call -> name unknown.
    msgs = [
        {"role": "system", "content": "sys"},
        _tool_result("orphan", BIG),
        _asst_call("b1", "browser_get_state"),
        _tool_result("b1", BIG),
        _asst_call("b2", "browser_get_state"),
        _tool_result("b2", BIG),
    ]
    pruned, count = _prune(msgs, keep_last=1)
    assert count == 1  # only b1 (superseded, known-name)
    orphan = next(m for m in pruned if m.get("tool_call_id") == "orphan")
    assert orphan["content"] == BIG


def test_does_not_mutate_input():
    msgs = _browser_session(3)
    snapshot = copy.deepcopy(msgs)
    _prune(msgs, keep_last=1)
    assert msgs == snapshot  # original untouched


def test_keep_last_ge_count_prunes_nothing():
    msgs = _browser_session(2)
    pruned, count = _prune(msgs, keep_last=2)
    assert count == 0
    assert pruned == msgs


def test_non_string_content_is_skipped():
    # Multimodal / structured content is left alone (conservative).
    blocks = [{"type": "text", "text": BIG}]
    msgs = [
        {"role": "system", "content": "sys"},
        _asst_call("b1", "browser_get_state"),
        _tool_result("b1", blocks),
        _asst_call("b2", "browser_get_state"),
        _tool_result("b2", BIG),
    ]
    pruned, count = _prune(msgs, keep_last=1)
    assert count == 0
    kept = next(m for m in pruned if m.get("tool_call_id") == "b1")
    assert kept["content"] == blocks


def test_empty_messages():
    pruned, count = _prune([], keep_last=2)
    assert pruned == []
    assert count == 0


# ---------------------------------------------------------------------------
# ContextCompressor.prune_stale_browser_states (config-driven wrapper)
# ---------------------------------------------------------------------------


def _compressor(**kw) -> ContextCompressor:
    # claude-opus-4-7 is in the static catalog, so no discovery / warnings.
    return ContextCompressor("claude-opus-4-7", quiet_mode=True, **kw)


def test_method_prunes_superseded_browser_states_when_enabled():
    c = _compressor(prune_browser_state=True, browser_state_keep_last=2)
    out = c.prune_stale_browser_states(_browser_session(4))
    results = [m for m in out if m.get("role") == "tool"]
    assert results[0]["content"] == PLACEHOLDER
    assert results[1]["content"] == PLACEHOLDER
    assert results[2]["content"] == f"{BIG}#3"
    assert results[3]["content"] == f"{BIG}#4"


def test_method_is_noop_when_disabled():
    c = _compressor(prune_browser_state=False, browser_state_keep_last=2)
    msgs = _browser_session(4)
    out = c.prune_stale_browser_states(msgs)
    assert out == msgs


def test_method_honours_keep_last_config():
    c = _compressor(prune_browser_state=True, browser_state_keep_last=1)
    out = c.prune_stale_browser_states(_browser_session(3))
    results = [m for m in out if m.get("role") == "tool"]
    assert results[0]["content"] == PLACEHOLDER
    assert results[1]["content"] == PLACEHOLDER
    assert results[2]["content"] == f"{BIG}#3"  # only the current one kept


def test_method_defaults_enabled():
    # Default construction (no kwargs) should prune — the fix ships on.
    c = _compressor()
    out = c.prune_stale_browser_states(_browser_session(4))
    pruned = [m for m in out if m.get("role") == "tool" and m["content"] == PLACEHOLDER]
    assert len(pruned) == 2  # 4 states, default keep_last=2


def test_llmsettings_defaults_enable_pruning():
    from surogates.config import LLMSettings

    settings = LLMSettings()
    assert settings.prune_browser_state is True
    assert settings.browser_state_keep_last == 2


def test_method_never_clears_current_state_even_at_keep_last_zero():
    # Misconfiguration guard: keep_last=0 must never blind the agent by
    # clearing the current page state — the most recent state always survives.
    c = _compressor(prune_browser_state=True, browser_state_keep_last=0)
    out = c.prune_stale_browser_states(_browser_session(3))
    results = [m for m in out if m.get("role") == "tool"]
    assert results[-1]["content"] == f"{BIG}#3"


def test_no_target_tool_results_returns_input_unchanged():
    # Non-browser session: nothing to prune, returns the same list (exercises
    # the cheap early-return, no scan of tool results needed).
    msgs = [
        {"role": "system", "content": "sys"},
        _asst_call("f1", "read_file"),
        _tool_result("f1", BIG),
        _asst_call("f2", "list_dir"),
        _tool_result("f2", BIG),
    ]
    out, count = _prune(msgs, keep_last=1)
    assert count == 0
    assert out is msgs


class TestNavigateSnapshotsArePruned:
    """browser_navigate returns a page outline, so its results supersede too.

    Regression from GAIA dev-021: navigate snapshots were not in the pruned
    set, so every one stayed in context for the whole session. One task made
    34 browser calls and peaked at 324,700 input tokens against a 262,144
    window -- it had passed the run before.
    """

    def test_navigate_is_in_the_superseded_set(self) -> None:
        from surogates.harness.context import _SUPERSEDED_STATE_TOOLS

        assert "browser_navigate" in _SUPERSEDED_STATE_TOOLS
        assert "browser_get_state" in _SUPERSEDED_STATE_TOOLS

    def test_older_navigate_snapshots_are_replaced(self) -> None:
        msgs: list[dict] = [{"role": "system", "content": "sys"}]
        for i in range(4):
            msgs.append(_asst_call(f"n{i}", "browser_navigate"))
            msgs.append(_tool_result(f"n{i}", BIG))

        out, pruned = _prune(
            msgs, keep_last=1,
            tool_names=frozenset({"browser_get_state", "browser_navigate"}),
        )
        results = [m for m in out if m.get("role") == "tool"]
        assert pruned == 3
        assert [r["content"] for r in results[:3]] == [PLACEHOLDER] * 3
        # The current page must always survive, or the agent has no DOM to act on.
        assert results[-1]["content"] == BIG
