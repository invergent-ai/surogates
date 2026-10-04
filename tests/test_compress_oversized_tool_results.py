"""A short history with huge tool results must shrink instead of overflowing.

PROD sessions reading spreadsheets reached 131k real tokens in ~20 messages:
too few to summarise, so compaction returned them unchanged and the session
crash-looped on the provider's context-length 400.
"""

from __future__ import annotations

from typing import Any

import pytest

from surogates.harness.context import (
    ContextCompressor,
    _estimate_messages_tokens_rough,
)


def _spreadsheet_session(reads: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "user", "content": "Summarise the workbooks."}]
    for i in range(reads):
        call_id = f"call_{i}"
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "read_file", "arguments": f'{{"path": "book{i}.xlsx"}}'},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": f"## Sheet{i}\n" + "| 1 | 2 | 3 |\n" * 3_500,  # ~50 KB, the read cap
        })
    return messages


@pytest.mark.asyncio
async def test_short_history_over_budget_truncates_oldest_tool_results() -> None:
    compressor = ContextCompressor("gpt-4o-mini", quiet_mode=True)
    messages = _spreadsheet_session(6)
    assert len(messages) <= compressor.protect_first_n + compressor.protect_last_n + 1

    # Dense tables tokenize denser than chars/4; ~110k real, as in PROD.
    scale = 1.5
    real_tokens = int(scale * _estimate_messages_tokens_rough(messages))
    compressed, data = await compressor.compress(messages, None, current_tokens=real_tokens)

    assert data["strategy"] == "truncate_tool_results"
    assert len(compressed) == len(messages)
    assert [m.get("tool_call_id") for m in compressed] == [m.get("tool_call_id") for m in messages]
    assert scale * _estimate_messages_tokens_rough(compressed) <= compressor.threshold_tokens // 2
    assert "truncated tool output" in compressed[2]["content"]  # oldest read cut
    assert compressed[-1]["content"] == messages[-1]["content"]  # newest read kept


@pytest.mark.asyncio
async def test_short_history_under_budget_is_untouched() -> None:
    compressor = ContextCompressor("gpt-4o-mini", quiet_mode=True)
    messages = _spreadsheet_session(1)

    compressed, data = await compressor.compress(messages, None)

    assert data["strategy"] == "too_few_messages"
    assert compressed == messages
