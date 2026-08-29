"""Spilled tool results must land where ``read_file`` will look for them.

The spill previously always wrote to a local ``/tmp`` path.  With tools
executing in a sandbox pod that path is both on the wrong machine and
outside the session workspace, so ``read_file`` refused it and the model
lost the output it was told it could page through.
"""

import pytest

from surogates.harness.context import ContextCompressor
from surogates.tools.utils.tool_result_storage import (
    STORAGE_DIR,
    WORKSPACE_STORAGE_DIR,
    enforce_turn_budget,
    make_sandbox_writer,
    maybe_persist_tool_result,
)


class _FakePool:
    """Records what the harness asks the sandbox to write."""

    def __init__(self, output: str = "ok") -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._output = output

    async def execute(self, session_id: str, name: str, input: str) -> str:
        self.calls.append((session_id, name, input))
        return self._output


async def test_sandboxed_spill_is_workspace_relative():
    pool = _FakePool()
    writer = make_sandbox_writer(pool, "owner-1")

    result = await maybe_persist_tool_result(
        content="x" * 5000,
        tool_name="kb_read_page",
        tool_use_id="toolu_abc",
        threshold=100,
        writer=writer,
    )

    assert len(pool.calls) == 1
    session_id, tool, _payload = pool.calls[0]
    assert (session_id, tool) == ("owner-1", "write_file")
    # The advertised path must be the one read_file can resolve: relative to
    # the workspace, never an absolute /tmp path outside it.
    assert f"{WORKSPACE_STORAGE_DIR}/toolu_abc.txt" in result
    assert STORAGE_DIR not in result


async def test_spill_falls_back_to_truncation_when_sandbox_write_fails():
    # The sandbox reports tool failures in-band as a JSON error body.
    pool = _FakePool(output='{"error": "Write denied"}')
    writer = make_sandbox_writer(pool, "owner-1")

    result = await maybe_persist_tool_result(
        content="y" * 5000,
        tool_name="kb_read_page",
        tool_use_id="toolu_def",
        threshold=100,
        writer=writer,
    )

    assert "could not be saved" in result
    # Never advertise a path that was not written.
    assert WORKSPACE_STORAGE_DIR not in result


async def test_local_spill_used_when_no_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "surogates.tools.utils.tool_result_storage.STORAGE_DIR", str(tmp_path),
    )
    result = await maybe_persist_tool_result(
        content="z" * 5000,
        tool_name="kb_read_page",
        tool_use_id="toolu_ghi",
        threshold=100,
    )
    assert str(tmp_path / "toolu_ghi.txt") in result
    assert (tmp_path / "toolu_ghi.txt").read_text() == "z" * 5000


async def test_turn_budget_spill_routes_through_the_writer():
    pool = _FakePool()
    writer = make_sandbox_writer(pool, "owner-2")
    messages = [
        {"tool_call_id": "a", "content": "a" * 300_000},
        {"tool_call_id": "b", "content": "b" * 10},
    ]

    await enforce_turn_budget(messages, writer=writer)

    assert [c[1] for c in pool.calls] == ["write_file"]
    assert f"{WORKSPACE_STORAGE_DIR}/a.txt" in messages[0]["content"]


def test_context_window_step_down_moves_the_compaction_threshold():
    c = ContextCompressor("gpt-4o", quiet_mode=True, threshold_percent=0.5)
    c.context_length = 200_000
    c._derive_budgets()
    assert c.threshold_tokens == 100_000

    # A provider rejecting the request as too long steps the window down,
    # and every derived budget must follow it.
    assert c.set_context_length(64_000) is True
    assert c.context_length == 64_000
    assert c.threshold_tokens == 32_000
    assert c.should_compress(33_000) is True

    # Growing is the guess that just failed; refuse it.
    assert c.set_context_length(500_000) is False
    assert c.context_length == 64_000


def test_compression_threshold_is_configurable():
    from surogates.config import LLMSettings

    assert LLMSettings().compression_threshold == 0.50
    assert LLMSettings(compression_threshold=0.8).compression_threshold == 0.8
    with pytest.raises(ValueError):
        LLMSettings(compression_threshold=1.5)
