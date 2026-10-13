"""The content-with-tools fallback only ever holds the latest tool step's words.

The loop uses it as a final answer when a turn ends with nothing new to say. On
PROD session 96784ab3 a tool step 16 minutes earlier ("I'll load the spreadsheet
skill first...") was still cached after later tool steps that said nothing, and
the turn ended on it as if it were the answer.
"""

from __future__ import annotations

from surogates.harness.reasoning import ContentWithToolsCache

_CALL = [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]


def test_a_later_silent_tool_step_drops_the_cached_words():
    cache = ContentWithToolsCache()
    cache.maybe_cache({"content": "I'll load the spreadsheet skill first.", "tool_calls": _CALL})
    cache.maybe_cache({"content": "", "tool_calls": _CALL})
    assert cache.get_fallback() is None


def test_the_latest_tool_step_words_stay_available():
    # "You're welcome!" said alongside a memory save is the answer when the
    # follow-up has nothing to add.
    cache = ContentWithToolsCache()
    cache.maybe_cache({"content": "You're welcome!", "tool_calls": _CALL})
    cache.maybe_cache({"content": "", "reasoning": "thinking only"})
    assert cache.get_fallback() == "You're welcome!"
