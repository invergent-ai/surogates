import inspect

import pytest

from surogates.harness.context import ContextCompressor


class _FakeCompletions:
    def __init__(self, capture):
        self._capture = capture

    async def create(self, *, model, messages, max_tokens):
        self._capture["prompt"] = messages[0]["content"]

        class _M:
            content = "## Goal\nWrap up."

        class _C:
            message = _M()

        class _R:
            choices = [_C()]

        return _R()


class _FakeChat:
    def __init__(self, capture):
        self.completions = _FakeCompletions(capture)


class _FakeClient:
    def __init__(self, capture):
        self.chat = _FakeChat(capture)


def test_compress_accepts_pre_compress_guidance():
    sig = inspect.signature(ContextCompressor.compress)
    assert "pre_compress_guidance" in sig.parameters
    assert sig.parameters["pre_compress_guidance"].default == ""


@pytest.mark.asyncio
async def test_guidance_reaches_summary_prompt():
    capture = {}
    compressor = ContextCompressor(
        "surogate", quiet_mode=True, summary_client=_FakeClient(capture),
    )
    compressor._summary_failure_cooldown_until = 0.0
    turns = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    await compressor._generate_summary(
        turns, None, pre_compress_guidance="ROLLBACK owned by Ada",
    )
    assert "ROLLBACK owned by Ada" in capture["prompt"]


@pytest.mark.asyncio
async def test_no_guidance_leaves_prompt_clean():
    capture = {}
    compressor = ContextCompressor(
        "surogate", quiet_mode=True, summary_client=_FakeClient(capture),
    )
    compressor._summary_failure_cooldown_until = 0.0
    turns = [{"role": "user", "content": "hello"}]
    await compressor._generate_summary(turns, None)
    assert "MEMORY FACTS TO PRESERVE" not in capture["prompt"]
