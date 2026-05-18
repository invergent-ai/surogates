# Thinking Runaway Mitigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Execution status

- [x] Task 1: Add `LLM_HEARTBEAT` event type
- [x] Task 2: Conditional stale-timeout bump for reasoning models
- [x] Task 3: Heartbeat emission from the watchdog
- [ ] Task 4: In-stream runaway-reasoning detector
- [ ] Task 5: Per-turn `thinking_disabled_for_turn` flag in AgentHarness
- [ ] Task 6: Reset flag at user-turn boundary
- [ ] Task 7: Outer retry path — re-issue runaway streams with thinking disabled
- [ ] Final integration check

**Goal:** Prevent the three GLM-5.1 thinking-runaway failure modes observed in PROD session `5274a540-50d7-46ed-8642-f38320f14cad` by (1) bumping the stream-stale watchdog for reasoning-capable models, (2) surfacing a transient "still working" heartbeat during silent reasoning, and (3) detecting in-stream runaway reasoning and retrying once with thinking disabled — for the remainder of that user turn only.

**Architecture:** Three independent layers, all in the surogates harness:
- `surogates/harness/llm_call.py` — model-aware stale-timeout, heartbeat emission from the existing watchdog, in-stream reasoning-character counter that triggers a "runaway" signal.
- `surogates/harness/loop.py` — per-turn `thinking_disabled_for_turn` flag that the existing thinking-gate consults; flag is set when the LLM-call layer signals a runaway-recovery, cleared on every new user turn.
- `surogates/session/events.py` — new `LLM_HEARTBEAT` event type.

Each layer is independently mergeable. The retry path uses the existing `MAX_LLM_RETRIES` budget — no new retry layer.

**Tech Stack:** Python 3.12, asyncio, OpenAI async SDK, pytest + pytest-asyncio.

**Source of truth for "is this model a reasoning model":** the existing `model_supports_thinking_toggle()` predicate at [`surogates/harness/expert_routing.py:519`](../../../surogates/harness/expert_routing.py#L519).

---

## File Structure

**Modify:**
- `surogates/session/events.py` — add `LLM_HEARTBEAT` enum member.
- `surogates/harness/llm_call.py` — three changes (timeout bump, heartbeat, runaway detector + retry-with-thinking-off).
- `surogates/harness/loop.py` — per-turn flag, gate consults it, reset at turn boundary, propagate from LLM-call response.

**Create:**
- `tests/test_thinking_runaway.py` — all new tests for tasks 2 through 7. `tests/test_expert_routing.py` remains part of the regression run because the thinking gate shares helpers with expert routing, but this plan does not add tests there.

**Don't modify:**
- `tests/test_stream_stall.py` — existing watchdog tests. Stays valid; this plan adds *new* tests in a sibling file to avoid coupling.

---

## Task 1: Add `LLM_HEARTBEAT` event type

**Files:**
- Modify: `surogates/session/events.py`

- [ ] **Step 1: Add the enum member**

In `surogates/session/events.py`, add `LLM_HEARTBEAT = "llm.heartbeat"` immediately after `LLM_DELTA`:

```python
    # LLM interaction
    LLM_REQUEST = "llm.request"
    LLM_RESPONSE = "llm.response"
    LLM_THINKING = "llm.thinking"
    LLM_DELTA = "llm.delta"
    # Emitted by the streaming watchdog when the upstream has been
    # silent past STREAM_HEARTBEAT_INTERVAL but is still inside the
    # stale-timeout window. Lets the UI distinguish "model is silently
    # reasoning" from "stream is dead".
    LLM_HEARTBEAT = "llm.heartbeat"
```

- [ ] **Step 2: Run the events test suite to confirm no regression**

Run: `cd /work/surogates && .venv/bin/pytest tests/ -k "events" -q`
Expected: PASS (no event-related tests should break from adding a new enum member).

- [ ] **Step 3: Commit**

```bash
cd /work/surogates
git add surogates/session/events.py
git commit -m "feat(events): add LLM_HEARTBEAT event type"
```

---

## Task 2: Conditional stale-timeout bump for reasoning models

**Files:**
- Modify: `surogates/harness/llm_call.py` (function `compute_stream_stale_timeout` at lines 94-114)
- Test: `tests/test_thinking_runaway.py` (new file)

The current `compute_stream_stale_timeout` scales by message length (50k → 240s, 100k → 300s). We add a model-class tier: when `model_supports_thinking_toggle(model)` is true, the baseline rises to 600s. Explicit env-var overrides still win.

- [ ] **Step 1: Create the test file with the failing test**

Create `tests/test_thinking_runaway.py`:

```python
"""Tests for the thinking-runaway mitigation: timeout bump, heartbeat,
in-stream runaway detection, and retry-with-thinking-off.

Each test exercises one layer in isolation; the runaway-retry test
(test_runaway_retry_disables_thinking) covers the end-to-end glue.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.llm_call import (
    call_llm_streaming_inner,
    compute_stream_stale_timeout,
)
from surogates.session.events import EventType


def _make_session():
    return SimpleNamespace(
        id=uuid4(),
        config={"temperature": 0.7},
        model="zai-org/GLM-5.1",
    )


def test_stream_stale_timeout_bumped_for_reasoning_models(monkeypatch):
    """Reasoning models get a 600s default so long silent reasoning
    phases on DeepInfra do not trip the watchdog."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 180.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", False,
    )

    timeout = compute_stream_stale_timeout(
        [{"role": "user", "content": "short request"}],
        base_url="https://api.deepinfra.com/v1/openai",
        model="zai-org/GLM-5.1",
    )

    assert timeout == 600.0


def test_stream_stale_timeout_unchanged_for_non_reasoning_models(monkeypatch):
    """OpenAI/Anthropic and other non-toggle models keep the 180s default."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 180.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", False,
    )

    timeout = compute_stream_stale_timeout(
        [{"role": "user", "content": "short request"}],
        base_url="https://api.openai.com/v1",
        model="gpt-4o",
    )

    assert timeout == 180.0


def test_stream_stale_timeout_explicit_override_wins_for_reasoning(monkeypatch):
    """SUROGATES_STREAM_STALE_TIMEOUT env var must override the reasoning bump."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 90.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", True,
    )

    timeout = compute_stream_stale_timeout(
        [{"role": "user", "content": "short"}],
        base_url="https://api.deepinfra.com/v1/openai",
        model="zai-org/GLM-5.1",
        explicit_timeout=90.0,
    )

    assert timeout == 90.0
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_thinking_runaway.py::test_stream_stale_timeout_bumped_for_reasoning_models -v`
Expected: FAIL — assertion `180.0 == 600.0` (or similar).

- [ ] **Step 3: Add the reasoning-model bump constant and predicate import**

In `surogates/harness/llm_call.py`, near the top with the other timeout constants (around line 60-66):

```python
# Stale stream detection timeout (seconds).  If no real streaming chunk
# arrives within this window the stream is considered stale and will be
# cancelled.  Configurable via ``SUROGATES_STREAM_STALE_TIMEOUT`` env var.
STREAM_STALE_TIMEOUT_EXPLICIT: bool = "SUROGATES_STREAM_STALE_TIMEOUT" in os.environ
STREAM_STALE_TIMEOUT: float = float(
    os.environ.get("SUROGATES_STREAM_STALE_TIMEOUT", "180.0")
)

# Reasoning-capable models (GLM-5, Qwen3, QwQ) often go silent on the
# wire for several minutes during their reasoning phase -- the upstream
# only emits chunks once thinking resolves.  Bump the watchdog ceiling
# for these models so legitimate long reasoning isn't killed.  Verified
# against PROD session 5274a540: iter 8 was killed by the 180s watchdog
# while GLM-5.1 was still reasoning silently.
STREAM_STALE_TIMEOUT_REASONING: float = 600.0
```

- [ ] **Step 4: Import the predicate and apply the bump**

Still in `surogates/harness/llm_call.py`, add the import alongside other harness imports near the top of the file:

```python
from surogates.harness.expert_routing import model_supports_thinking_toggle
```

(If a circular import bites, do the import lazily inside `compute_stream_stale_timeout` instead — same call site, just below the explicit-timeout fast-path.)

Then update `compute_stream_stale_timeout` (currently lines 94-114):

```python
def compute_stream_stale_timeout(
    messages: list[dict[str, Any]] | None,
    *,
    base_url: str = "",
    model: str = "",
    explicit_timeout: float | None = None,
) -> float:
    """Return the stale-stream watchdog timeout for one request."""
    if explicit_timeout is not None:
        return float(explicit_timeout)

    if not STREAM_STALE_TIMEOUT_EXPLICIT and _is_local_base_url(base_url):
        return float("inf")

    # Reasoning-capable upstreams need a much higher ceiling -- they
    # routinely go silent for multiple minutes during the reasoning
    # phase.  The env-var explicit override (handled above) still wins.
    if not STREAM_STALE_TIMEOUT_EXPLICIT and model_supports_thinking_toggle(model):
        baseline = STREAM_STALE_TIMEOUT_REASONING
    else:
        baseline = STREAM_STALE_TIMEOUT

    approx_tokens = _estimate_message_tokens(messages or [])
    if approx_tokens > 100_000:
        return max(baseline, 300.0)
    if approx_tokens > 50_000:
        return max(baseline, 240.0)
    return baseline
```

- [ ] **Step 5: Run the new tests and confirm they pass**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_thinking_runaway.py -v -k "stale_timeout"`
Expected: PASS (3 tests).

- [ ] **Step 6: Run the existing stall tests to confirm no regression**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_stream_stall.py -v`
Expected: PASS (all existing tests).

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add surogates/harness/llm_call.py tests/test_thinking_runaway.py
git commit -m "feat(harness): raise stream-stale watchdog to 600s for reasoning models"
```

---

## Task 3: Heartbeat emission from the watchdog

**Files:**
- Modify: `surogates/harness/llm_call.py` (`_watchdog` inside `call_llm_streaming_inner`, around lines 1036-1072)
- Test: `tests/test_thinking_runaway.py`

The watchdog already wakes every `STREAM_CHUNK_POLL_INTERVAL` (1 s). When the gap since `last_chunk_time` exceeds `STREAM_HEARTBEAT_INTERVAL` (15 s) but is still below `stale_timeout`, emit one `LLM_HEARTBEAT` event. Track `last_heartbeat_time` so we don't emit on every poll.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_thinking_runaway.py`:

```python
class _BlockingStream:
    """Yields prefix chunks, then hangs until aclose()."""

    def __init__(self, prefix_chunks):
        self._prefix = list(prefix_chunks)
        self._i = 0
        self.closed = False
        self._close_event = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i < len(self._prefix):
            chunk = self._prefix[self._i]
            self._i += 1
            return chunk
        await self._close_event.wait()
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True
        self._close_event.set()


def _chunk(content=None, reasoning_content=None, finish_reason=None):
    delta = SimpleNamespace(
        content=content,
        role=None,
        tool_calls=None,
        reasoning_content=reasoning_content,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        model="zai-org/GLM-5.1",
        usage=None,
    )


@pytest.mark.asyncio
async def test_watchdog_emits_heartbeat_during_silent_stream(monkeypatch):
    """When the stream is silent past STREAM_HEARTBEAT_INTERVAL but
    still inside the stale_timeout window, the watchdog must emit
    LLM_HEARTBEAT events so the UI can show 'still working'."""
    # Compressed timings so the test runs in ~1s, not minutes.
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 2.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", True,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_HEARTBEAT_INTERVAL", 0.2,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_CHUNK_POLL_INTERVAL", 0.05,
    )

    stream = _BlockingStream([_chunk(content="Hi")])
    llm_client = MagicMock()
    llm_client.chat.completions.create = AsyncMock(return_value=stream)
    store = AsyncMock()

    msg, usage = await asyncio.wait_for(
        call_llm_streaming_inner(
            session=_make_session(),
            create_kwargs={"model": "zai-org/GLM-5.1", "messages": []},
            iteration=1,
            llm_client=llm_client,
            store=store,
            interrupt_check=lambda: False,
        ),
        timeout=5.0,
    )

    heartbeat_calls = [
        c for c in store.emit_event.await_args_list
        if c.args[1] == EventType.LLM_HEARTBEAT
    ]
    # In 2s stale window with 0.2s heartbeat interval, expect at least 3
    # heartbeats (roughly t=0.2, 0.4, 0.6, ... before stale at t=2.0).
    assert len(heartbeat_calls) >= 3, (
        f"expected ≥3 heartbeats, got {len(heartbeat_calls)}"
    )
    payload = heartbeat_calls[0].args[2]
    assert payload["iteration"] == 1
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_thinking_runaway.py::test_watchdog_emits_heartbeat_during_silent_stream -v`
Expected: FAIL — `STREAM_HEARTBEAT_INTERVAL` attribute does not exist (and no heartbeat events emitted).

- [ ] **Step 3: Add the heartbeat-interval constant**

In `surogates/harness/llm_call.py`, just after the `STREAM_CHUNK_POLL_INTERVAL` constant (around line 73):

```python
# Heartbeat interval (seconds).  When the stream is silent past this
# threshold but still inside the stale-timeout window, the watchdog
# emits an LLM_HEARTBEAT event so the UI can distinguish "model is
# silently reasoning" from "stream is dead".  Picked to be short
# enough that users see motion within ~15s of silence.
STREAM_HEARTBEAT_INTERVAL: float = 15.0
```

- [ ] **Step 4: Modify the watchdog to emit heartbeats**

In `call_llm_streaming_inner`, replace the existing `_watchdog` (around lines 1039-1072). The new version tracks `last_heartbeat_time` and emits at most one heartbeat per interval:

```python
    stop_reason: str | None = None
    stop_event = asyncio.Event()
    last_heartbeat_time: float = time.monotonic()

    async def _watchdog() -> None:
        nonlocal stop_reason, last_heartbeat_time
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=STREAM_CHUNK_POLL_INTERVAL,
                )
                return  # stop_event fired -- main loop already done
            except asyncio.TimeoutError:
                pass

            now = time.monotonic()
            if (now - last_chunk_time) > stale_timeout:
                logger.warning(
                    "Stream stale for %.0fs (threshold %.0fs) — no chunks "
                    "received. Cancelling stream for session %s (iteration %d).",
                    now - last_chunk_time,
                    stale_timeout,
                    session.id,
                    iteration,
                )
                stop_reason = "stale"
                await _close_stream()
                return
            if interrupt_check is not None and interrupt_check():
                logger.info(
                    "Mid-stream interrupt for session %s (iteration %d); "
                    "cancelling stream",
                    session.id,
                    iteration,
                )
                stop_reason = "interrupt"
                await _close_stream()
                return

            # Heartbeat: stream is silent but still inside the stale
            # window. Surface a transient signal so the UI shows motion.
            silent_for = now - last_chunk_time
            since_last_beat = now - last_heartbeat_time
            if (
                silent_for >= STREAM_HEARTBEAT_INTERVAL
                and since_last_beat >= STREAM_HEARTBEAT_INTERVAL
            ):
                last_heartbeat_time = now
                try:
                    await store.emit_event(
                        session.id,
                        EventType.LLM_HEARTBEAT,
                        {
                            "iteration": iteration,
                            "silent_for_seconds": round(silent_for, 1),
                        },
                    )
                except Exception:
                    # Heartbeat is best-effort -- never fail the stream
                    # because the store transiently rejected an event.
                    logger.debug(
                        "Heartbeat emit failed for session %s",
                        session.id,
                        exc_info=True,
                    )
```

Also reset `last_heartbeat_time` whenever a chunk arrives. Inside the main `async for chunk in response:` block, on the same line that updates `last_chunk_time` (currently line 1080):

```python
        async for chunk in response:
            last_chunk_time = time.monotonic()
            last_heartbeat_time = last_chunk_time
            if stop_reason is not None:
                ...
```

- [ ] **Step 5: Run the heartbeat test and confirm it passes**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_thinking_runaway.py::test_watchdog_emits_heartbeat_during_silent_stream -v`
Expected: PASS.

- [ ] **Step 6: Run the watchdog regression suite**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_stream_stall.py -v`
Expected: PASS — heartbeat additions must not affect stale-detection or healthy-stream behaviour.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add surogates/harness/llm_call.py tests/test_thinking_runaway.py
git commit -m "feat(harness): emit LLM_HEARTBEAT during silent streaming"
```

---

## Task 4: In-stream runaway-reasoning detector

**Files:**
- Modify: `surogates/harness/llm_call.py` (constants and the streaming accumulator)
- Test: `tests/test_thinking_runaway.py`

While the stream is running, accumulate the total `reasoning_content` chars and track whether any `content`/`tool_calls` delta has arrived. If `reasoning_chars` crosses `RUNAWAY_REASONING_CHAR_THRESHOLD` before the first content/tool-call delta, set `stop_reason = "runaway_reasoning"` and close the stream. The existing finally-block code path already surfaces `stop_reason` in `usage_data["stream_error_reason"]`.

Threshold of 16 000 chars chosen from probe: GLM-5.1 emits ~4 chars/token, so 16 000 chars ≈ 4 000 reasoning tokens. PROD iter-6 dead-end was ~15 000 reasoning tokens (~60 KB); iter-4 (legitimate) was ~1 200 (~5 KB). Threshold sits comfortably between.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_thinking_runaway.py`:

```python
@pytest.mark.asyncio
async def test_runaway_reasoning_cancels_stream(monkeypatch):
    """When reasoning_content chars exceed RUNAWAY_REASONING_CHAR_THRESHOLD
    without any content or tool_call delta, the stream is cancelled and
    the response is marked with stream_error_reason='runaway_reasoning'."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 30.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", True,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.RUNAWAY_REASONING_CHAR_THRESHOLD", 100,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_CHUNK_POLL_INTERVAL", 0.02,
    )

    # 6 reasoning chunks × 30 chars = 180 chars, crosses the 100 threshold.
    chunks = [_chunk(reasoning_content="x" * 30) for _ in range(6)]

    class _ReasoningStream(_BlockingStream):
        async def __anext__(self):
            if self._i < len(self._prefix):
                chunk = self._prefix[self._i]
                self._i += 1
                await asyncio.sleep(0.01)
                return chunk
            await self._close_event.wait()
            raise StopAsyncIteration

    stream = _ReasoningStream(chunks)
    llm_client = MagicMock()
    llm_client.chat.completions.create = AsyncMock(return_value=stream)
    store = AsyncMock()

    msg, usage = await asyncio.wait_for(
        call_llm_streaming_inner(
            session=_make_session(),
            create_kwargs={"model": "zai-org/GLM-5.1", "messages": []},
            iteration=1,
            llm_client=llm_client,
            store=store,
            interrupt_check=lambda: False,
        ),
        timeout=3.0,
    )

    assert stream.closed is True
    assert usage["finish_reason"] == "interrupted"
    assert usage["stream_error_reason"] == "runaway_reasoning"


@pytest.mark.asyncio
async def test_runaway_detector_silent_after_content_arrives(monkeypatch):
    """Once any content delta has arrived, runaway detection MUST NOT
    fire even if reasoning continues to accumulate.  Some models
    interleave reasoning and content; we only care about the
    'all reasoning, never any visible output' failure mode."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 30.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", True,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.RUNAWAY_REASONING_CHAR_THRESHOLD", 50,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_CHUNK_POLL_INTERVAL", 0.02,
    )

    # Content arrives first, then a flood of reasoning -- not a runaway.
    chunks = [
        _chunk(content="Hello"),
        *[_chunk(reasoning_content="x" * 30) for _ in range(10)],
        _chunk(finish_reason="stop"),
    ]

    class _Stream:
        def __init__(self, chunks):
            self._chunks = chunks
            self._i = 0
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._i >= len(self._chunks):
                raise StopAsyncIteration
            chunk = self._chunks[self._i]
            self._i += 1
            await asyncio.sleep(0.01)
            return chunk

        async def aclose(self):
            self.closed = True

    stream = _Stream(chunks)
    llm_client = MagicMock()
    llm_client.chat.completions.create = AsyncMock(return_value=stream)
    store = AsyncMock()

    msg, usage = await call_llm_streaming_inner(
        session=_make_session(),
        create_kwargs={"model": "zai-org/GLM-5.1", "messages": []},
        iteration=1,
        llm_client=llm_client,
        store=store,
        interrupt_check=lambda: False,
    )

    assert usage["finish_reason"] == "stop"
    assert usage.get("stream_error_reason") is None
    assert msg["content"] == "Hello"
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_thinking_runaway.py -v -k "runaway"`
Expected: FAIL — `RUNAWAY_REASONING_CHAR_THRESHOLD` does not exist; runaway never fires.

- [ ] **Step 3: Add the runaway-threshold constant**

In `surogates/harness/llm_call.py`, alongside the heartbeat constant added in Task 3:

```python
# Maximum reasoning_content characters allowed before any visible
# content or tool-call delta has arrived.  Above this, the stream is
# treated as a runaway-reasoning failure and cancelled so the outer
# retry layer can retry with thinking disabled.  ~4 chars/token on
# GLM-5.1, so 16 000 chars ≈ 4 000 reasoning tokens.  PROD iter-6
# dead-end was ~60 KB (well over); iter-4 legitimate was ~5 KB (well
# under).  Configurable for testing only -- no env var.
RUNAWAY_REASONING_CHAR_THRESHOLD: int = 16_000
```

- [ ] **Step 4: Track reasoning chars and content-emitted flag in the stream loop**

In `call_llm_streaming_inner`, two changes inside the `async for chunk in response:` block (around lines 1106-1153).

First, initialise the counter and flag alongside the other accumulators (around line 1011 where `last_chunk_time` is set):

```python
    last_chunk_time: float = time.monotonic()
    reasoning_char_count: int = 0
    content_or_tool_emitted: bool = False
```

Second, update them where deltas are processed. Replace the existing reasoning-content block (lines 1127-1140) with:

```python
            # Reasoning content delta (DeepSeek, Qwen, Moonshot, GLM-5).
            reasoning_text = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
            )
            if reasoning_text:
                reasoning_parts.append(reasoning_text)
                reasoning_char_count += len(reasoning_text)
                # Emit LLM_DELTA event for reasoning so the frontend can
                # stream reasoning content incrementally (same as text deltas).
                await store.emit_event(
                    session.id,
                    EventType.LLM_DELTA,
                    {"reasoning": reasoning_text, "iteration": iteration},
                )
```

And update the content block (lines 1142-1153) so it marks the flag:

```python
            # Text content delta.
            text_delta = getattr(delta, "content", None)
            if text_delta:
                visible_delta = context_scrubber.feed(think_scrubber.feed(text_delta))
                if visible_delta:
                    content_or_tool_emitted = True
                    content_parts.append(visible_delta)
                    # Emit LLM_DELTA event.
                    await store.emit_event(
                        session.id,
                        EventType.LLM_DELTA,
                        {"content": visible_delta, "iteration": iteration},
                    )
```

And the tool-calls block (lines 1160-1209) — flip the flag at the top of `if tc_deltas:`:

```python
            tc_deltas = getattr(delta, "tool_calls", None)
            if tc_deltas:
                content_or_tool_emitted = True
                for tc_delta in tc_deltas:
                    ...
```

- [ ] **Step 5: Add the runaway check at the end of each delta**

After the reasoning, content, and tool-call blocks for the current `delta`, add the runaway check before the next chunk is read:

```python
            # Runaway-reasoning: model has emitted >threshold chars of
            # reasoning_content without any visible content or tool call.
            # Cancel; the outer retry will reissue with thinking disabled.
            if (
                not content_or_tool_emitted
                and reasoning_char_count > RUNAWAY_REASONING_CHAR_THRESHOLD
            ):
                logger.warning(
                    "Runaway reasoning: %d chars of reasoning_content with no "
                    "content/tool_call (threshold %d). Cancelling stream for "
                    "session %s (iteration %d).",
                    reasoning_char_count,
                    RUNAWAY_REASONING_CHAR_THRESHOLD,
                    session.id,
                    iteration,
                )
                stop_reason = "runaway_reasoning"
                await _close_stream()
                interrupted = True
                break
```

The existing `usage_data` construction (line 1278-1281) already surfaces `stop_reason` as `stream_error_reason`, so no change needed there.

- [ ] **Step 6: Run the runaway tests and confirm they pass**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_thinking_runaway.py -v -k "runaway"`
Expected: PASS (both runaway tests).

- [ ] **Step 7: Run the full streaming regression suite**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_stream_stall.py tests/test_streaming_executor.py tests/test_midstream_interrupt.py tests/test_midstream_retry.py tests/test_stream_scrubbers.py -v`
Expected: PASS — runaway logic must not regress stall, mid-stream interrupt, or scrubber behaviour.

- [ ] **Step 8: Commit**

```bash
cd /work/surogates
git add surogates/harness/llm_call.py tests/test_thinking_runaway.py
git commit -m "feat(harness): detect runaway reasoning streams and cancel"
```

---

## Task 5: Per-turn `thinking_disabled_for_turn` flag in AgentHarness

**Files:**
- Modify: `surogates/harness/loop.py` (`AgentHarness.__init__` and `_maybe_apply_thinking_gate`)
- Test: `tests/test_thinking_runaway.py`

Add a per-loop-instance flag that, when set, forces `_maybe_apply_thinking_gate` to disable thinking regardless of what the classifier says. The flag is set by Task 7 (retry path) and cleared by Task 6 (turn boundary).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_thinking_runaway.py`:

```python
@pytest.mark.asyncio
async def test_thinking_gate_respects_disabled_flag(monkeypatch):
    """When _thinking_disabled_for_turn is True, the thinking gate
    forces enable_thinking=False regardless of the classifier."""
    from surogates.harness.loop import AgentHarness

    # Build a minimal AgentHarness without the full constructor by
    # directly instantiating and patching only what the gate uses.
    loop = AgentHarness.__new__(AgentHarness)
    loop._tenant = None
    loop._thinking_disabled_for_turn = True

    monkeypatch.setattr(
        "surogates.harness.loop.classify_hard_task_async",
        AsyncMock(side_effect=AssertionError("classifier should not be called")),
    )

    create_kwargs = {"model": "zai-org/GLM-5.1", "extra_body": {}}
    await loop._maybe_apply_thinking_gate(
        create_kwargs,
        messages=[{"role": "user", "content": "easy"}],
    )

    extra = create_kwargs["extra_body"]
    assert extra["chat_template_kwargs"]["enable_thinking"] is False


@pytest.mark.asyncio
async def test_thinking_gate_unchanged_when_flag_not_set(monkeypatch):
    """When flag is False and classifier says required=True, gate must
    leave extra_body alone (model default = thinking on)."""
    from surogates.harness.loop import AgentHarness

    loop = AgentHarness.__new__(AgentHarness)
    loop._tenant = None
    loop._thinking_disabled_for_turn = False

    monkeypatch.setattr(
        "surogates.harness.loop.classify_hard_task_async",
        AsyncMock(return_value=SimpleNamespace(
            required=True,
            category="debugging",
            reason="test",
        )),
    )

    create_kwargs = {"model": "zai-org/GLM-5.1"}
    await loop._maybe_apply_thinking_gate(
        create_kwargs,
        messages=[{"role": "user", "content": "Debug this Python stack trace and explain the root cause."}],
    )

    extra = create_kwargs.get("extra_body") or {}
    # Either no extra_body at all, or it doesn't disable thinking.
    if "chat_template_kwargs" in extra:
        assert extra["chat_template_kwargs"].get("enable_thinking") is not False
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_thinking_runaway.py -v -k "thinking_gate"`
Expected: FAIL — `_thinking_disabled_for_turn` is ignored and `enable_thinking=False` is not injected.

- [ ] **Step 3: Add the flag to AgentHarness**

In `surogates/harness/loop.py`, add the flag in `AgentHarness.__init__` alongside `self._user_turn_count` (around line 949):

```python
        self._user_turn_count: int = 0

        # When set, the thinking gate forces enable_thinking=False for
        # every iteration in the current user turn, overriding the
        # classifier.  Set by the LLM-call retry path when a
        # runaway-reasoning stream was cancelled and re-issued with
        # thinking disabled (the same task would runaway again otherwise).
        # Cleared at the start of each new user turn.
        self._thinking_disabled_for_turn: bool = False
```

- [ ] **Step 4: Make `_maybe_apply_thinking_gate` consult the flag**

In `surogates/harness/loop.py`, modify `_maybe_apply_thinking_gate` (currently lines 3243-3295) so that the flag short-circuits any classifier work:

```python
    async def _maybe_apply_thinking_gate(
        self,
        create_kwargs: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> None:
        """Disable upstream reasoning on easy turns when the model supports it.

        Two paths can disable thinking:
        1. ``self._thinking_disabled_for_turn`` is True -- a prior
           runaway in this user turn already proved that thinking is
           failing here.  Force it off and skip the classifier entirely.
        2. The cached LLM classifier returns ``required=False`` for
           this turn.

        The flag in (1) clears on the next user turn (see the user-turn
        bookkeeping where ``_user_turn_count`` is incremented), so
        future turns get thinking back automatically.

        Failures (aux unavailable, network error, structured-output
        parse miss) fall through silently -- the request just keeps
        the model default (thinking on), matching the previous
        behavior.
        """
        model_id = str(create_kwargs.get("model") or "")
        if not model_supports_thinking_toggle(model_id):
            return
        if not messages:
            return

        if self._thinking_disabled_for_turn:
            thinking_extra = build_thinking_extra_body(enable_thinking=False)
            create_kwargs["extra_body"] = merge_extra_body(
                create_kwargs.get("extra_body"),
                thinking_extra,
            )
            logger.debug(
                "Thinking-gate: forcing reasoning off (runaway flag set "
                "for current user turn).",
            )
            return

        try:
            classification = await classify_hard_task_async(
                messages,
                tenant=self._tenant,
            )
        except Exception:
            logger.debug(
                "Thinking-gate classification failed; leaving model default.",
                exc_info=True,
            )
            return

        if classification.required:
            return

        thinking_extra = build_thinking_extra_body(enable_thinking=False)
        create_kwargs["extra_body"] = merge_extra_body(
            create_kwargs.get("extra_body"),
            thinking_extra,
        )
        logger.debug(
            "Auto-think gate: disabling reasoning for easy turn "
            "(category=%s, reason=%s).",
            classification.category,
            classification.reason,
        )
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_thinking_runaway.py -v -k "thinking_gate"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/harness/loop.py tests/test_thinking_runaway.py
git commit -m "feat(harness): per-turn flag forces thinking off after runaway"
```

---

## Task 6: Reset flag at user-turn boundary

**Files:**
- Modify: `surogates/harness/loop.py` (the line that increments `_user_turn_count`)
- Test: `tests/test_thinking_runaway.py`

The flag must clear whenever a new user turn begins so future turns are not punished for one bad turn. `_user_turn_count` is incremented at exactly one place (line 1505); reset the flag adjacent to it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_thinking_runaway.py`:

```python
def test_thinking_disabled_flag_resets_with_user_turn_count():
    """The per-turn flag must clear whenever the loop increments
    _user_turn_count (start of a new user turn).  This is a structural
    test -- it inspects the source to confirm the reset lives next to
    the counter increment, since the full wake() pipeline is too heavy
    to exercise in a unit test."""
    import inspect
    from surogates.harness import loop as loop_module

    source = inspect.getsource(loop_module)
    # Find the counter increment line.
    inc_idx = source.index("self._user_turn_count += 1")
    # Look at the next ~5 lines for the flag reset.
    snippet = source[inc_idx:inc_idx + 400]
    assert "self._thinking_disabled_for_turn = False" in snippet, (
        "_thinking_disabled_for_turn must be reset within ~5 lines of "
        "the _user_turn_count increment"
    )
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_thinking_runaway.py::test_thinking_disabled_flag_resets_with_user_turn_count -v`
Expected: FAIL — the reset line isn't there yet.

- [ ] **Step 3: Add the reset adjacent to the increment**

In `surogates/harness/loop.py`, at the user-turn-counter increment (currently line 1505):

```python
        # --- User turn tracking for memory nudge ---
        self._user_turn_count += 1
        # New user turn clears any prior runaway-thinking suppression.
        # Future turns get thinking back automatically.
        self._thinking_disabled_for_turn = False
        should_review_memory = False
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_thinking_runaway.py::test_thinking_disabled_flag_resets_with_user_turn_count -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/harness/loop.py tests/test_thinking_runaway.py
git commit -m "feat(harness): clear thinking-disabled flag on new user turn"
```

---

## Task 7: Outer retry path — re-issue runaway streams with thinking disabled

**Files:**
- Modify: `surogates/harness/llm_call.py` (function `call_llm_with_retry`, the success path inside the for-loop around lines 335-443)
- Modify: `surogates/harness/loop.py` (the caller that receives the LLM result and propagates `thinking_disabled_due_to_runaway` into `_thinking_disabled_for_turn`)
- Test: `tests/test_thinking_runaway.py`

When `usage_data["stream_error_reason"] == "runaway_reasoning"`, do **one** silent retry within the existing `MAX_LLM_RETRIES` budget, mutating `create_kwargs.extra_body.chat_template_kwargs.enable_thinking = False`. Stamp the successful response's `usage_data["thinking_disabled_due_to_runaway"] = True` so the loop layer can flip the per-turn flag.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_thinking_runaway.py`:

```python
@pytest.mark.asyncio
async def test_runaway_retry_disables_thinking(monkeypatch):
    """After a runaway-reasoning cancel, the next attempt within
    call_llm_with_retry must re-issue with
    chat_template_kwargs.enable_thinking=False and stamp the response."""
    from surogates.harness.llm_call import call_llm_with_retry

    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", True,
    )

    # First call returns a runaway-marked response; second returns a
    # clean response.  We assert the second call had enable_thinking=False
    # injected into extra_body.
    call_log: list[dict] = []

    async def fake_streaming(*, session, create_kwargs, iteration,
                              llm_client, store, interrupt_check,
                              set_streaming_enabled,
                              on_tool_call_complete):
        call_log.append({
            "extra_body": dict(create_kwargs.get("extra_body") or {}),
        })
        if len(call_log) == 1:
            return (
                {"role": "assistant", "content": ""},
                {
                    "finish_reason": "interrupted",
                    "stream_error_reason": "runaway_reasoning",
                    "input_tokens": 100, "output_tokens": 0,
                    "model": "zai-org/GLM-5.1",
                },
            )
        return (
            {"role": "assistant", "content": "Hello"},
            {
                "finish_reason": "stop",
                "input_tokens": 100, "output_tokens": 5,
                "model": "zai-org/GLM-5.1",
            },
        )

    monkeypatch.setattr(
        "surogates.harness.llm_call.call_llm_streaming", fake_streaming,
    )

    session = _make_session()
    llm_client = MagicMock()
    store = AsyncMock()

    msg, usage = await call_llm_with_retry(
        session=session,
        create_kwargs={
            "model": "zai-org/GLM-5.1",
            "messages": [{"role": "user", "content": "build a video"}],
        },
        iteration=1,
        llm_client=llm_client,
        store=store,
        interrupt_check=lambda: False,
        set_streaming_enabled=lambda enabled: None,
        streaming_enabled=True,
        rotate_credential=lambda *args, **kwargs: False,
        context_compressor=None,
        rate_limit_guard=None,
        activate_fallback=lambda: False,
        get_current_model=lambda: None,
    )

    assert len(call_log) == 2, "must retry once after runaway"
    # First call: no enable_thinking constraint.
    first_extra = call_log[0]["extra_body"]
    first_ct = first_extra.get("chat_template_kwargs", {})
    assert first_ct.get("enable_thinking") is not False
    # Second call: enable_thinking forced off.
    second_extra = call_log[1]["extra_body"]
    second_ct = second_extra.get("chat_template_kwargs", {})
    assert second_ct.get("enable_thinking") is False
    # Response marker propagates to loop.
    assert usage["thinking_disabled_due_to_runaway"] is True
    assert msg["content"] == "Hello"
```

Note: `call_llm_with_retry`' real signature may gain kwargs over time. Match it to the actual signature when the test is written — find the canonical signature by searching for `async def call_llm_with_retry` in `surogates/harness/llm_call.py`; pass the minimum required arguments as shown above.

- [ ] **Step 2: Run the test to confirm it fails**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_thinking_runaway.py::test_runaway_retry_disables_thinking -v`
Expected: FAIL — only one call is made (no retry on runaway marker), or the second call lacks the disable.

- [ ] **Step 3: Add the runaway-retry branch to the outer retry loop**

In `surogates/harness/llm_call.py`, inside `call_llm_with_retry`, modify the success path. Add a `runaway_retry_used` flag before the for-loop and inject the retry logic just before the `return result`:

```python
    has_retried_429 = False
    thinking_sig_retry_attempted = False
    compression_attempts = 0
    max_compression_attempts = 3
    active_on_tool_call_complete = on_tool_call_complete
    runaway_retry_used: bool = False  # only one silent thinking-off retry per call

    # Pre-call sanitization: clean surrogates and fix orphaned tool pairs.
    if "messages" in create_kwargs:
        sanitize_messages(create_kwargs["messages"])
        create_kwargs["messages"] = sanitize_tool_pairs(create_kwargs["messages"])

    for attempt in range(1, MAX_LLM_RETRIES + 1):
        ...
        try:
            if streaming_enabled:
                result = await call_llm_streaming(...)
            else:
                result = await call_llm_non_streaming(...)

            # Response shape validation.
            assistant_message, usage_data = result
            ...

            # Empty response -- likely provider issue.  Try fallback
            # immediately instead of burning through retries.
            if result is None or not assistant_message:
                ...

            # Runaway-reasoning soft retry: model produced > threshold
            # of reasoning_content without any visible content/tool_call.
            # Re-issue once with enable_thinking=False; the same task
            # plus thinking-on would runaway again.  Per-call budget of
            # one silent retry; further runaways fall through as
            # interrupted responses (loop layer surfaces them).
            if (
                usage_data.get("stream_error_reason") == "runaway_reasoning"
                and not runaway_retry_used
                and attempt < MAX_LLM_RETRIES
            ):
                runaway_retry_used = True
                from surogates.harness.expert_routing import (
                    build_thinking_extra_body,
                    merge_extra_body,
                )
                thinking_extra = build_thinking_extra_body(
                    enable_thinking=False,
                )
                create_kwargs["extra_body"] = merge_extra_body(
                    create_kwargs.get("extra_body"),
                    thinking_extra,
                )
                logger.warning(
                    "Runaway reasoning on session %s iter %d; retrying "
                    "with enable_thinking=False (attempt %d).",
                    session.id, iteration, attempt + 1,
                )
                continue

            # If a runaway retry succeeded, stamp the response so the
            # outer loop knows to flip its per-turn flag.
            if runaway_retry_used:
                usage_data["thinking_disabled_due_to_runaway"] = True

            return result
```

- [ ] **Step 4: Propagate the marker to the harness loop**

The exact call sites where `call_llm_with_retry` returns are in `surogates/harness/loop.py`. Find them by searching for the function name. Right after each result is unpacked into `assistant_message` / `usage_data`, add:

```python
            if usage_data.get("thinking_disabled_due_to_runaway"):
                self._thinking_disabled_for_turn = True
                logger.info(
                    "Runaway-reasoning recovery: disabling thinking for "
                    "remainder of user turn (session=%s).",
                    session.id,
                )
```

To find the right lines: grep for `call_llm_with_retry(` in `loop.py` (currently there are two call sites; both need the same propagation, but a small helper is cleaner). Place the check immediately after the function returns and the result is destructured.

- [ ] **Step 5: Run the retry test and confirm it passes**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_thinking_runaway.py::test_runaway_retry_disables_thinking -v`
Expected: PASS.

- [ ] **Step 6: Run the full new test file**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_thinking_runaway.py -v`
Expected: PASS — all tests from Tasks 2-7.

- [ ] **Step 7: Run the full harness regression suite**

Run: `cd /work/surogates && .venv/bin/pytest tests/test_stream_stall.py tests/test_streaming_executor.py tests/test_midstream_interrupt.py tests/test_midstream_retry.py tests/test_stream_scrubbers.py tests/test_expert_routing.py -v`
Expected: PASS — full streaming and expert-routing suite must still pass.

- [ ] **Step 8: Commit**

```bash
cd /work/surogates
git add surogates/harness/llm_call.py surogates/harness/loop.py tests/test_thinking_runaway.py
git commit -m "feat(harness): retry runaway reasoning with thinking disabled"
```

---

## Final integration check

- [ ] **Step 1: Run the entire harness test suite**

Run: `cd /work/surogates && .venv/bin/pytest tests/ -q --ignore=tests/integration`
Expected: PASS. Integration tests are excluded because they require live infra (Postgres, Redis) that may not be available locally.

- [ ] **Step 2: Manual smoke test against PROD-like prompt (optional)**

Re-run `/tmp/probe_glm_reasoning_stream.py` to confirm the probe still works after the harness changes (the probe imports the OpenAI SDK directly, not the harness, so this is just a sanity check).

Run: `/work/surogates/.venv/bin/python /tmp/probe_glm_reasoning_stream.py`
Expected: same output as before — DeepInfra emits `reasoning_content` deltas, ~5 600 chars of reasoning before content.

- [ ] **Step 3: Build wheel and verify import**

Run: `cd /work/surogates && .venv/bin/python -c "from surogates.harness.llm_call import RUNAWAY_REASONING_CHAR_THRESHOLD, STREAM_HEARTBEAT_INTERVAL, STREAM_STALE_TIMEOUT_REASONING; print(RUNAWAY_REASONING_CHAR_THRESHOLD, STREAM_HEARTBEAT_INTERVAL, STREAM_STALE_TIMEOUT_REASONING)"`
Expected output: `16000 15.0 600.0`

---

## Notes for the implementer

- **Don't add config knobs.** All three thresholds (`STREAM_STALE_TIMEOUT_REASONING`, `STREAM_HEARTBEAT_INTERVAL`, `RUNAWAY_REASONING_CHAR_THRESHOLD`) are intentionally module-level constants, not env vars. They're tunable in code if PROD shows wrong numbers; YAGNI on env exposure until then.
- **The `_user_turn_count += 1` line is the only correct reset point.** Not `wake()` — wake can fire multiple times within one user turn (harness retries, lease renewals). The user-turn counter is incremented exactly once per inbound `user.message`.
- **Tests use `monkeypatch.setattr` on module-level constants.** When adding `STREAM_STALE_TIMEOUT_EXPLICIT = True` in tests, you bypass the local-endpoint fast-path; do this whenever the test base_url is non-local to keep the timeout deterministic.
- **No new dependencies.** Everything reuses existing harness primitives: `model_supports_thinking_toggle`, `build_thinking_extra_body`, `merge_extra_body`, `EventType.LLM_HEARTBEAT`.

---

## Self-review

**Spec coverage:**
- "Raise STREAM_STALE_TIMEOUT to 600s" → Task 2 (conditional on reasoning models, since unconditional bump punishes non-reasoning models).
- "Surface a 'still thinking' heartbeat" → Tasks 1 (event type) + 3 (watchdog emission).
- "Count reasoning tokens; if >4k before any content/tool_call, interrupt and retry with thinking off" → Task 4 (detector) + Task 7 (retry path).
- "Thinking off for the rest of the user turn, then back to default" → Task 5 (flag + gate) + Task 6 (reset) + Task 7 (set on successful retry).

**Placeholder scan:** No TODOs, no "implement appropriate error handling", no "similar to Task N". Step 4 of Task 7 deliberately points the implementer at a grep instead of pre-stating the line number, because the call site is in loop.py and may move; the description is specific enough to find unambiguously.

**Type consistency:**
- `_thinking_disabled_for_turn` — bool, used identically in Tasks 5, 6, 7.
- `stream_error_reason` value — string `"runaway_reasoning"`, set in Task 4, checked in Task 7. Matches the existing `"stale"` / `"interrupt"` values that share the same field.
- `thinking_disabled_due_to_runaway` — bool key on `usage_data`, written in Task 7 step 3, read in Task 7 step 4. Names match.
- Event payload for `LLM_HEARTBEAT` — `{"iteration": int, "silent_for_seconds": float}` defined in Task 3 step 4, asserted in Task 3 step 1 test (`payload["iteration"] == 1`). Consistent.
