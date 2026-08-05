"""Compaction continuity and failure-visibility tests.

The compressor is rebuilt by ``harness_factory`` on every wake, so anything
it needs to remember across compactions has to be recoverable from the
message list itself.  And when the summariser is unavailable the middle is
dropped -- that has to be visible to both the model and an operator reading
the ``context.compact`` event, not silent.
"""

from __future__ import annotations

from typing import Any

import pytest

from surogates.harness.context import SUMMARY_PREFIX, ContextCompressor


def _compressor(**kwargs: Any) -> ContextCompressor:
    return ContextCompressor("gpt-4o-mini", quiet_mode=True, **kwargs)


def _conversation(n: int) -> list[dict[str, Any]]:
    """A head + long middle + tail that comfortably exceeds the protect floors."""
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "kick off the work"},
        {"role": "assistant", "content": "starting"},
        {"role": "user", "content": "carry on"},
    ]
    for i in range(n):
        messages.append({"role": "assistant", "content": f"step {i} " + "x" * 200})
        messages.append({"role": "user", "content": f"next {i}"})
    return messages


@pytest.mark.asyncio
async def test_previous_summary_is_recovered_from_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh compressor must carry forward the summary already in context."""
    comp = _compressor()
    seen: dict[str, Any] = {}

    async def fake_generate(turns, client, **kw):
        seen["previous"] = comp._previous_summary
        seen["turns"] = turns
        return "rolled up"

    monkeypatch.setattr(comp, "_generate_summary", fake_generate)

    messages = _conversation(40)
    # A prior compaction left its marker just past the protected head.
    marker = {
        "role": "assistant",
        "content": f"{SUMMARY_PREFIX}\nearlier work: built the parser",
    }
    messages.insert(3, marker)

    await comp.compress(messages, llm_client=object())

    assert seen["previous"] is not None
    assert "built the parser" in seen["previous"]
    # And the marker must not also be re-fed as a turn to summarise.
    assert marker not in seen["turns"]


@pytest.mark.asyncio
async def test_summariser_failure_is_visible_not_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped middle must be announced in-context and in the event."""
    comp = _compressor()

    async def fake_generate(turns, client, **kw):
        return None

    monkeypatch.setattr(comp, "_generate_summary", fake_generate)

    messages = _conversation(40)
    original_len = len(messages)

    compressed, data = await comp.compress(messages, llm_client=object())

    # The token win is kept -- this is not a no-op.
    assert len(compressed) < original_len
    # The loss is stated to the model.
    assert any(
        "could not be summarised" in str(m.get("content", "")).lower()
        for m in compressed
    ), compressed
    # And to anyone querying the events table.
    assert data["strategy"] == "summary_unavailable"
    assert data.get("summary")
