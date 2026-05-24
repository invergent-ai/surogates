# Simple Chat Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a default "Simple" view to `agent-chat-react` that groups each LLM iteration under a model-generated one-liner and appends a per-turn artifact summary card. Today's per-tool view stays as Expert mode behind a toggle.

**Architecture:** Two coordinated changes. The harness gains a `TurnSummarizer` that uses the existing `summary_model` auxiliary LLM to emit two new persisted events: `iteration.summary` (one per LLM iteration) and `turn.summary` (one per assistant turn). The SDK reducer attaches these to assistant messages, a new `IterationGroup` component renders the grouped view in Simple mode, and a `TurnSummaryCard` lists artifacts beneath the turn's final text. A `Simple / Expert` toggle in the composer's tools row gates the two render paths.

**Tech Stack:** Python 3.12 + pytest + asyncio + pydantic-settings (harness); TypeScript + React 19 + Vitest + @testing-library/react (SDK).

**Spec:** [docs/superpowers/specs/2026-05-24-simple-chat-mode-design.md](../specs/2026-05-24-simple-chat-mode-design.md)

---

## Status

Updated before each commit during inline execution.

- [x] A1: WorkerSettings.emit_turn_summaries kill switch
- [x] A2: ITERATION_SUMMARY and TURN_SUMMARY event types
- [x] A3: thread turn_id through call_llm_with_retry and LLM_DELTA emissions
- [x] A4: generate turn_id in wake() and stamp LLM_THINKING/LLM_RESPONSE
- [x] A5: TurnSummarizer module
- [x] A6: wire TurnSummarizer into AgentHarness and worker.py
- [x] A7: emit iteration.summary per iteration
- [x] A8: emit turn.summary and drain in _complete_session
- [x] A9: emit_turn_summaries gate integration test
- [x] B1: SDK event type union and AGENT_CHAT_LISTENED_EVENTS
- [ ] B2: summary types and AgentChatMessage/State extensions
- [ ] B3: reducer stamps turnId/iterationIndex
- [ ] B4: reducer handlers for iteration.summary and turn.summary
- [ ] B5: viewMode runtime state, adapter methods, localStorage fallback
- [ ] B6: IterationGroup component (reuses existing timeline pieces)
- [ ] B7: TurnSummaryCard (with ArtifactBlock resolution)
- [ ] B8: Simple/Expert toggle in composer
- [ ] B9: Render IterationGroup + TurnSummaryCard in Simple mode
- [ ] B10: opt existing tests into Expert mode
- [ ] C1: end-to-end manual verification

---

## Review corrections

These corrections were added during plan review and override conflicting
details in the task bodies below.

- Existing backend tests live under `tests/`, not `tests/harness/`.
  Creating `tests/harness/` is acceptable, but do not rely on
  non-existent fixtures such as `fake_harness_turn`,
  `worker_harness_for_settings`, or `settings_with_summary_model`.
  Before Tasks A4/A7/A8/A9, create explicit local test scaffolding or
  shared fixtures by adapting the existing patterns in
  `tests/test_harness_resilience.py` (`_make_harness`) and
  `tests/test_outcome_harness.py` (`FakeStore`). The plan must not
  leave "use whatever fixture exists" as an implementation decision.
- Task A3 must test behavior, not only the
  `call_llm_with_retry` signature. Add a test that drives a streaming
  delta path with a fake store and asserts each emitted `llm.delta`
  payload includes `turn_id` and `iteration_index`. Also use the
  current helper name `call_llm_non_streaming`; the plan text currently
  mentions `call_llm_nonstreaming`, which does not exist.
- Tasks A7/A8 must either pass prior completed iteration summaries into
  `summarize_iteration` or explicitly update the spec. The current
  Task A7 note saying v1 passes no prior summaries contradicts the
  design.
- Task A8 candidate artifact extraction must read current tool-call
  payload keys: `name` and `arguments`, not `tool_name` and `args`.
- Task B4 must not redefine `numberValue`; `src/runtime/reducer.ts`
  already has `numberValue(value): number` that returns `0` for
  non-numeric input. Add a separate `optionalNumberValue(value):
  number | null`, or use an inline `typeof value === "number"` check
  in the summary handlers.
- Task B6 must preserve the existing timeline behavior inside expanded
  iterations. Do not create a separate reduced renderer that only shows
  raw reasoning and `ToolCallBlock`; instead expose/reuse
  `messageToEntries`, `TimelineEntryItem`, `groupBrowserActivityEntries`,
  and `groupWebSearchEntries`, or keep `IterationGroup` in
  `chat-thread.tsx` where those helpers are available. This is required
  by the spec's non-goal of not reworking browser/web-search grouping
  or per-tool renderers.
- Task B7 does not yet satisfy artifact links for `kind: "artifact"`.
  It must resolve the artifact id against the session's
  `artifact.created` system message metadata and render the existing
  `ArtifactBlock`, or the plan should narrow the spec. Plain text
  fallback is only acceptable when the artifact cannot be resolved.
- Task B9's Simple path must not drop system entries that are currently
  threaded into assistant groups, especially `skill.invoked` and
  `artifact.created`. If `SimpleAssistantGroup` filters to assistant
  messages only, it needs an explicit path for system timeline entries
  inside expanded iterations or adjacent to the group.

---

## Phase A — Harness changes

All paths in this phase are relative to `/work/surogates/`.

### Task A1: Kill-switch setting

**Files:**
- Modify: `surogates/config.py:197-206` (WorkerSettings)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py` (create file if absent — see other `tests/test_*.py` files for the existing import shape):

```python
import os

from surogates.config import WorkerSettings


def test_worker_settings_default_emit_turn_summaries_is_true():
    settings = WorkerSettings()
    assert settings.emit_turn_summaries is True


def test_worker_settings_emit_turn_summaries_disabled_via_env(monkeypatch):
    monkeypatch.setenv("SUROGATES_WORKER_EMIT_TURN_SUMMARIES", "false")
    settings = WorkerSettings()
    assert settings.emit_turn_summaries is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates && python -m pytest tests/test_config.py::test_worker_settings_default_emit_turn_summaries_is_true -v
```
Expected: FAIL with `AttributeError: 'WorkerSettings' object has no attribute 'emit_turn_summaries'`.

- [ ] **Step 3: Add the field**

In `surogates/config.py`, modify the `WorkerSettings` class (currently ending at line 206 with `use_api_for_harness_tools`). Add:

```python
class WorkerSettings(BaseSettings):
    """Worker process configuration."""

    model_config = {"env_prefix": "SUROGATES_WORKER_"}

    concurrency: int = 50
    poll_timeout: int = 5
    workspace_path: str = "/tmp/surogates/workspaces"
    api_base_url: str = "http://localhost:8000"
    use_api_for_harness_tools: bool = True
    # Emit iteration.summary / turn.summary events from the harness.
    # Kill switch for the Simple chat-mode summary pipeline.
    emit_turn_summaries: bool = True
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /work/surogates && python -m pytest tests/test_config.py -v
```
Expected: both new tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates && git add surogates/config.py tests/test_config.py && git commit -m "feat(harness): add WorkerSettings.emit_turn_summaries kill switch"
```

---

### Task A2: New EventType enum values

**Files:**
- Modify: `surogates/session/events.py`
- Test: `tests/session/test_events.py`

- [ ] **Step 1: Write the failing test**

Create `tests/session/test_events.py` (the `tests/session/` directory may not yet exist — create it and add an empty `__init__.py`):

```python
from surogates.session.events import EventType


def test_iteration_summary_event_type():
    assert EventType.ITERATION_SUMMARY.value == "iteration.summary"


def test_turn_summary_event_type():
    assert EventType.TURN_SUMMARY.value == "turn.summary"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates && python -m pytest tests/session/test_events.py -v
```
Expected: FAIL with `AttributeError: ITERATION_SUMMARY`.

- [ ] **Step 3: Add the enum values**

In `surogates/session/events.py`, add to the `EventType` enum (place after the existing `# LLM interaction` group near line 24):

```python
    LLM_HEARTBEAT = "llm.heartbeat"

    # Per-LLM-iteration summary, one-line imperative ("Rework hero
    # paragraph"). Emitted after each iteration completes; rendered
    # by Simple chat mode in agent-chat-react.
    ITERATION_SUMMARY = "iteration.summary"

    # Per-assistant-turn recap + curated artifact list. Emitted after
    # the final iteration of a turn completes; rendered as the
    # TurnSummaryCard at the bottom of each turn in Simple mode.
    TURN_SUMMARY = "turn.summary"
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /work/surogates && python -m pytest tests/session/test_events.py -v
```
Expected: both new tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates && git add surogates/session/events.py tests/session/test_events.py tests/session/__init__.py 2>/dev/null; cd /work/surogates && git commit -m "feat(events): add ITERATION_SUMMARY and TURN_SUMMARY event types"
```

---

### Task A3: Thread `turn_id` through `call_llm_with_retry` and LLM_DELTA emissions

**Files:**
- Modify: `surogates/harness/llm_call.py:367-388` (signature), and every site that calls `store.emit_event(..., EventType.LLM_DELTA, ...)` inside the file (currently at lines 561, 1247, 1261, 1376 — verify with the grep step before editing)
- Modify: `surogates/harness/loop.py:1766-1788` (the `call_llm_with_retry(...)` invocation in `_run_iteration`)
- Test: `tests/harness/test_llm_call_turn_id.py` (new)

- [ ] **Step 1: Find every LLM_DELTA emit site in `llm_call.py`**

```bash
cd /work/surogates && grep -n "EventType.LLM_DELTA" surogates/harness/llm_call.py
```
Note each line number; you'll patch each one in Step 3.

- [ ] **Step 2: Write the failing test**

Create `tests/harness/test_llm_call_turn_id.py`:

```python
"""turn_id propagation into LLM_DELTA payloads."""
from __future__ import annotations

import inspect

from surogates.harness.llm_call import call_llm_with_retry


def test_call_llm_with_retry_accepts_turn_id_kwarg():
    sig = inspect.signature(call_llm_with_retry)
    assert "turn_id" in sig.parameters
    param = sig.parameters["turn_id"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY
    assert param.default is None  # optional for backwards compat
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd /work/surogates && python -m pytest tests/harness/test_llm_call_turn_id.py -v
```
Expected: FAIL with `assert "turn_id" in sig.parameters`.

- [ ] **Step 4: Add the parameter and thread it into every LLM_DELTA emission**

In `surogates/harness/llm_call.py`:

a. Add `turn_id: str | None = None,` to the keyword-only block of `call_llm_with_retry` (insert right after `iteration: int,` near line 371):

```python
async def call_llm_with_retry(
    *,
    session: Session,
    create_kwargs: dict[str, Any],
    iteration: int,
    turn_id: str | None = None,
    llm_client: AsyncOpenAI,
    ...existing kwargs...
) -> tuple[dict[str, Any], dict[str, Any]]:
```

b. Each downstream helper that `call_llm_with_retry` invokes (`call_llm_streaming`, `call_llm_non_streaming`, the partial-tool-call retry, the reasoning-delta retry, etc.) also needs `turn_id` if it emits LLM_DELTA directly. The simplest pattern: thread `turn_id` through every helper that emits LLM_DELTA, and at each `store.emit_event(..., EventType.LLM_DELTA, payload)` site, inject `payload["turn_id"] = turn_id` and `payload["iteration_index"] = iteration - 1` (only when `turn_id is not None`).

For each LLM_DELTA emit site identified in Step 1, mutate the payload dict immediately before the `emit_event` call. Example (the partial-tool-call retry at line 561):

```python
            if isinstance(exc, PartialToolCallStreamError):
                if not classified.retryable or attempt >= MAX_LLM_RETRIES:
                    raise
                payload: dict[str, Any] = {
                    "iteration": iteration,
                    "reconnect": True,
                    "partial_tool_names": exc.partial_tool_names,
                }
                if turn_id is not None:
                    payload["turn_id"] = turn_id
                    payload["iteration_index"] = max(iteration - 1, 0)
                await store.emit_event(
                    session.id,
                    EventType.LLM_DELTA,
                    payload,
                )
```

Apply the same pattern at lines 1247, 1261, 1376 (recheck the line numbers from Step 1 — they may shift as you edit). For helpers that build the payload elsewhere (e.g. `call_llm_streaming` constructs payloads internally), add `turn_id` to those helpers' signatures and pass it through from `call_llm_with_retry`'s call sites.

c. Update the `call_llm_streaming` and `call_llm_non_streaming` helper signatures (lines ~445 and ~456) so the kwarg passes through:

```python
            if streaming_enabled:
                result = await call_llm_streaming(
                    session=session,
                    create_kwargs=create_kwargs,
                    iteration=iteration,
                    turn_id=turn_id,
                    llm_client=llm_client,
                    ...
                )
            else:
                result = await call_llm_non_streaming(
                    session=session,
                    create_kwargs=create_kwargs,
                    iteration=iteration,
                    turn_id=turn_id,
                    llm_client=llm_client,
                    ...
                )
```

Apply the same to `call_llm_streaming`'s definition (around line 996) and any internal call sites that re-invoke it.

- [ ] **Step 5: Run test to verify it passes**

```bash
cd /work/surogates && python -m pytest tests/harness/test_llm_call_turn_id.py -v
```
Expected: PASS.

- [ ] **Step 6: Re-run the existing llm_call test suite to make sure nothing broke**

```bash
cd /work/surogates && python -m pytest tests/harness/ -v -k "llm_call or streaming" 2>&1 | tail -30
```
Expected: all green.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates && git add surogates/harness/llm_call.py tests/harness/test_llm_call_turn_id.py && git commit -m "feat(harness): thread turn_id into LLM_DELTA payloads"
```

---

### Task A4: Generate `turn_id` in `AgentHarness` and stamp it on LLM_THINKING / LLM_RESPONSE

**Files:**
- Modify: `surogates/harness/loop.py` — the `wake()` body around line 1467 (where `iteration = 0` is reset), and the LLM_THINKING / LLM_RESPONSE emission sites at lines 1820-1824 and 1971-1975
- Test: `tests/harness/test_loop_turn_id.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/harness/test_loop_turn_id.py`:

```python
"""turn_id and iteration_index appear on LLM_THINKING and LLM_RESPONSE events."""
from __future__ import annotations

import uuid

import pytest

from surogates.session.events import EventType


@pytest.mark.asyncio
async def test_llm_response_payload_has_turn_id_and_iteration_index(fake_harness_turn):
    """fake_harness_turn is a fixture defined in tests/harness/conftest.py
    that runs one fake LLM turn through AgentHarness and returns all
    emitted events. See its existing usage in other tests in this dir."""
    events = await fake_harness_turn(
        responses=[{"role": "assistant", "content": "hi"}],
    )
    llm_response = next(e for e in events if e["type"] == EventType.LLM_RESPONSE.value)
    assert "turn_id" in llm_response["data"]
    assert llm_response["data"]["iteration_index"] == 0
    uuid.UUID(llm_response["data"]["turn_id"])  # validates format


@pytest.mark.asyncio
async def test_two_iterations_share_turn_id_with_increasing_index(fake_harness_turn):
    events = await fake_harness_turn(
        responses=[
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "todo", "arguments": "{\"action\":\"list\"}"}},
            ]},
            {"role": "assistant", "content": "done"},
        ],
    )
    responses = [e for e in events if e["type"] == EventType.LLM_RESPONSE.value]
    assert len(responses) == 2
    assert responses[0]["data"]["turn_id"] == responses[1]["data"]["turn_id"]
    assert responses[0]["data"]["iteration_index"] == 0
    assert responses[1]["data"]["iteration_index"] == 1
```

If `tests/harness/conftest.py` does not yet expose a `fake_harness_turn` fixture, search for the nearest equivalent and either reuse it or copy its pattern into a new fixture in `tests/harness/conftest.py`:

```bash
cd /work/surogates && grep -rln "AgentHarness(" tests/harness/ | head -5
```

Pick the integration test that already constructs an `AgentHarness` with mocks, and lift the shared scaffolding into the `fake_harness_turn` fixture. The fixture must return the full list of events emitted by `await harness.wake(session.id)`.

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates && python -m pytest tests/harness/test_loop_turn_id.py -v
```
Expected: FAIL with `KeyError: 'turn_id'` on the LLM_RESPONSE payload.

- [ ] **Step 3: Generate `turn_id` once per `wake()` and thread it through**

In `surogates/harness/loop.py`:

a. Just above the `iteration = 0` line at 1467, add:

```python
        import uuid as _uuid_mod

        # One stable turn_id per user turn. The harness loop in this
        # wake() services exactly one user turn (it returns on
        # session.complete, session.pause, or session.fail), so a
        # single UUID is correct.
        turn_id = str(_uuid_mod.uuid4())

        iteration = 0
```

b. In the `call_llm_with_retry(...)` invocation around line 1766, add `turn_id=turn_id,` right after `iteration=iteration,`:

```python
                assistant_message, usage_data = await call_llm_with_retry(
                    session=session,
                    create_kwargs=create_kwargs,
                    iteration=iteration,
                    turn_id=turn_id,
                    llm_client=self._llm,
                    ...
                )
```

c. At the LLM_THINKING emission around line 1820, change:

```python
            if reasoning_text:
                await self._store.emit_event(
                    session.id,
                    EventType.LLM_THINKING,
                    {
                        "reasoning": reasoning_text,
                        "turn_id": turn_id,
                        "iteration_index": iteration - 1,
                    },
                )
```

d. At the LLM_RESPONSE emission around line 1971, mutate `response_data` just before the `emit_event` call:

```python
            response_data["turn_id"] = turn_id
            response_data["iteration_index"] = iteration - 1
            event_id = await self._store.emit_event(
                session.id,
                EventType.LLM_RESPONSE,
                response_data,
            )
```

e. There is a second `call_llm_with_retry` invocation at line 3929 (the runaway-iteration summary path). Add `turn_id=turn_id,` there as well. That site does not run inside `_run_iteration`; instead it lives in `_emit_max_iterations_summary` (or similar). Read the surrounding context to confirm a `turn_id` is in scope — if not, either lift one in via a parameter, or generate a fresh one (it's a one-shot completion message, not a real user turn).

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /work/surogates && python -m pytest tests/harness/test_loop_turn_id.py -v
```
Expected: both new tests PASS.

- [ ] **Step 5: Run a broader harness suite to catch regressions**

```bash
cd /work/surogates && python -m pytest tests/harness/ -v 2>&1 | tail -40
```
Expected: all green; no new failures.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates && git add surogates/harness/loop.py tests/harness/test_loop_turn_id.py tests/harness/conftest.py && git commit -m "feat(harness): stamp turn_id and iteration_index on LLM events"
```

---

### Task A5: `TurnSummarizer` module

**Files:**
- Create: `surogates/harness/turn_summarizer.py`
- Test: `tests/harness/test_turn_summarizer.py`

- [ ] **Step 1: Write the failing test**

Create `tests/harness/test_turn_summarizer.py`:

```python
"""TurnSummarizer unit tests with a stubbed summary client."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from surogates.harness.turn_summarizer import (
    TurnArtifact,
    TurnSummarizer,
    TurnSummary,
)


@dataclass
class _StubResponse:
    content: str

    @property
    def choices(self):
        return [type("C", (), {"message": type("M", (), {"content": self.content})()})()]


class _StubChatCompletions:
    def __init__(self, content: str):
        self._content = content
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _StubResponse(self._content)


class _StubChat:
    def __init__(self, content: str):
        self.completions = _StubChatCompletions(content)


class _StubClient:
    def __init__(self, content: str):
        self.chat = _StubChat(content)


@pytest.mark.asyncio
async def test_summarize_iteration_returns_one_line():
    client = _StubClient("Rework hero paragraph to introduce brain/hands metaphor")
    summarizer = TurnSummarizer(summary_client=client, summary_model="cheap-model")
    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="Let me consider the hero text...",
        tool_calls=[
            {"id": "c1", "function": {"name": "patch",
                                       "arguments": '{"path":"landing.html"}'}},
        ],
        prior_iteration_summaries=[],
    )
    assert result == "Rework hero paragraph to introduce brain/hands metaphor"
    assert client.chat.completions.calls[0]["model"] == "cheap-model"


@pytest.mark.asyncio
async def test_summarize_turn_returns_recap_and_artifacts():
    payload = (
        '{"recap": "Reworked the hero around brain/hands.",'
        ' "artifacts": ['
        '   {"kind": "file", "label": "landing.html", "ref": "landing.html"},'
        '   {"kind": "url", "label": "example.com", "ref": "https://example.com"}'
        ' ]}'
    )
    client = _StubClient(payload)
    summarizer = TurnSummarizer(summary_client=client, summary_model="cheap-model")
    result = await summarizer.summarize_turn(
        turn_id="t1",
        user_message="please update the hero",
        iteration_summaries=["Rework hero paragraph"],
        candidate_artifacts=[
            TurnArtifact(kind="file", label="landing.html", ref="landing.html"),
            TurnArtifact(kind="url", label="example.com", ref="https://example.com"),
        ],
    )
    assert isinstance(result, TurnSummary)
    assert result.recap.startswith("Reworked the hero")
    assert len(result.artifacts) == 2
    assert result.artifacts[0].kind == "file"


@pytest.mark.asyncio
async def test_summarize_turn_returns_none_on_invalid_json():
    client = _StubClient("not even close to JSON")
    summarizer = TurnSummarizer(summary_client=client, summary_model="cheap-model")
    result = await summarizer.summarize_turn(
        turn_id="t1",
        user_message="hi",
        iteration_summaries=[],
        candidate_artifacts=[],
    )
    assert result is None


@pytest.mark.asyncio
async def test_summarize_iteration_returns_none_on_empty_response():
    client = _StubClient("")
    summarizer = TurnSummarizer(summary_client=client, summary_model="cheap-model")
    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="",
        tool_calls=[],
        prior_iteration_summaries=[],
    )
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates && python -m pytest tests/harness/test_turn_summarizer.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'surogates.harness.turn_summarizer'`.

- [ ] **Step 3: Create the module**

Create `surogates/harness/turn_summarizer.py`:

```python
"""Per-iteration and per-turn LLM summaries for the Simple chat view."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Per-call timeout; mirrors the title generator's defensive cap.
_SUMMARY_TIMEOUT_SECONDS = 10.0
_MAX_ITERATION_SUMMARY_TOKENS = 64
_MAX_TURN_SUMMARY_TOKENS = 512

TurnArtifactKind = Literal["file", "artifact", "url", "command"]


@dataclass(frozen=True)
class TurnArtifact:
    kind: TurnArtifactKind
    label: str
    ref: str
    meta: dict[str, Any] | None = None


@dataclass(frozen=True)
class TurnSummary:
    recap: str
    artifacts: list[TurnArtifact] = field(default_factory=list)


_ITERATION_PROMPT = (
    "You write one short imperative sentence describing what an agent "
    "just did in this iteration. Be specific and concrete. No quotes, "
    "no period at the end, no leading 'The agent', max 12 words."
)

_TURN_PROMPT = (
    "Summarize what an agent accomplished in this turn for the user. "
    "Return ONLY a JSON object with two fields:\n"
    "  recap: 1-3 short sentences in plain prose, no markdown\n"
    "  artifacts: a curated subset of the candidate artifacts the user "
    "would want quick access to. Drop noisy read-only lookups; keep "
    "files written/edited, created artifacts, fetched URLs, and "
    "notable commands. Each artifact is "
    '{"kind": "file|artifact|url|command", "label": str, "ref": str}.'
)


class TurnSummarizer:
    """Produce one-line iteration summaries and per-turn recaps."""

    def __init__(self, *, summary_client: Any, summary_model: str) -> None:
        self._client = summary_client
        self._model = summary_model

    async def summarize_iteration(
        self,
        *,
        iteration_id: str,
        reasoning: str,
        tool_calls: list[dict[str, Any]],
        prior_iteration_summaries: list[str],
    ) -> str | None:
        if not reasoning and not tool_calls:
            return None
        tool_lines: list[str] = []
        for tc in tool_calls:
            name = (tc.get("function") or {}).get("name") or tc.get("name") or "?"
            args = (tc.get("function") or {}).get("arguments") or tc.get("arguments") or ""
            args_snippet = (args or "")[:200]
            tool_lines.append(f"{name}({args_snippet})")
        user_block_parts: list[str] = []
        if prior_iteration_summaries:
            prior = "\n".join(f"- {s}" for s in prior_iteration_summaries)
            user_block_parts.append(f"Earlier in this turn:\n{prior}")
        if reasoning:
            user_block_parts.append(f"Reasoning:\n{reasoning[:2000]}")
        if tool_lines:
            user_block_parts.append("Tools called:\n" + "\n".join(tool_lines))
        user_block = "\n\n".join(user_block_parts)

        kwargs = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _ITERATION_PROMPT},
                {"role": "user", "content": user_block},
            ],
            "max_tokens": _MAX_ITERATION_SUMMARY_TOKENS,
            "temperature": 0.2,
            "stream": False,
        }
        try:
            response = await asyncio.wait_for(
                self._client.chat.completions.create(**kwargs),
                timeout=_SUMMARY_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning(
                "iteration summarization failed for %s: %r", iteration_id, exc,
            )
            return None
        content = response.choices[0].message.content if response.choices else None
        text = (content or "").strip().strip('"').rstrip(".")
        return text or None

    async def summarize_turn(
        self,
        *,
        turn_id: str,
        user_message: str,
        iteration_summaries: list[str],
        candidate_artifacts: list[TurnArtifact],
    ) -> TurnSummary | None:
        if not iteration_summaries and not candidate_artifacts:
            return None
        cand_lines = "\n".join(
            f"- kind={a.kind} label={a.label!r} ref={a.ref!r}"
            for a in candidate_artifacts
        )
        user_block = (
            f"User asked: {user_message[:1000]}\n\n"
            f"Iteration summaries:\n"
            + "\n".join(f"- {s}" for s in iteration_summaries)
            + (f"\n\nCandidate artifacts:\n{cand_lines}" if cand_lines else "")
        )
        kwargs = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _TURN_PROMPT},
                {"role": "user", "content": user_block},
            ],
            "max_tokens": _MAX_TURN_SUMMARY_TOKENS,
            "temperature": 0.3,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        try:
            response = await asyncio.wait_for(
                self._client.chat.completions.create(**kwargs),
                timeout=_SUMMARY_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning(
                "turn summarization failed for %s: %r", turn_id, exc,
            )
            return None
        content = response.choices[0].message.content if response.choices else None
        if not content:
            return None
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            logger.warning("turn summary returned non-JSON: %r", content[:200])
            return None
        if not isinstance(parsed, dict):
            return None
        recap = str(parsed.get("recap") or "").strip()
        raw_artifacts = parsed.get("artifacts") or []
        artifacts: list[TurnArtifact] = []
        for a in raw_artifacts if isinstance(raw_artifacts, list) else []:
            if not isinstance(a, dict):
                continue
            kind = a.get("kind")
            label = a.get("label")
            ref = a.get("ref")
            if kind not in ("file", "artifact", "url", "command"):
                continue
            if not isinstance(label, str) or not isinstance(ref, str):
                continue
            artifacts.append(TurnArtifact(kind=kind, label=label, ref=ref))
        if not recap and not artifacts:
            return None
        return TurnSummary(recap=recap, artifacts=artifacts)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /work/surogates && python -m pytest tests/harness/test_turn_summarizer.py -v
```
Expected: all four tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates && git add surogates/harness/turn_summarizer.py tests/harness/test_turn_summarizer.py && git commit -m "feat(harness): add TurnSummarizer for iteration/turn summaries"
```

---

### Task A6: Wire `TurnSummarizer` into `AgentHarness` and `worker.py`

**Files:**
- Modify: `surogates/harness/loop.py:837-870` (AgentHarness `__init__`)
- Modify: `surogates/orchestrator/worker.py:830-865` (AgentHarness construction)
- Test: `tests/harness/test_harness_summarizer_wiring.py`

- [ ] **Step 1: Write the failing test**

Create `tests/harness/test_harness_summarizer_wiring.py`:

```python
"""AgentHarness accepts and stores an optional TurnSummarizer."""
from __future__ import annotations

import inspect

from surogates.harness.loop import AgentHarness


def test_agent_harness_accepts_turn_summarizer_kwarg():
    sig = inspect.signature(AgentHarness.__init__)
    assert "turn_summarizer" in sig.parameters
    param = sig.parameters["turn_summarizer"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


def test_agent_harness_stores_turn_summarizer_when_provided():
    from surogates.harness.turn_summarizer import TurnSummarizer

    class _StubClient:
        chat = type("X", (), {"completions": type("Y", (), {"create": lambda **_: None})()})()

    summarizer = TurnSummarizer(summary_client=_StubClient(), summary_model="m")
    harness = _make_minimal_harness(turn_summarizer=summarizer)
    assert harness._turn_summarizer is summarizer


def test_agent_harness_defaults_turn_summarizer_to_none():
    harness = _make_minimal_harness()
    assert harness._turn_summarizer is None


def _make_minimal_harness(**overrides):
    """Construct an AgentHarness with the minimum non-None scaffolding.

    Use whatever minimal stubs the other tests in tests/harness/ already
    rely on; the goal here is only to exercise the constructor.
    """
    from unittest.mock import MagicMock

    base = dict(
        session_store=MagicMock(),
        tool_registry=MagicMock(),
        llm_client=MagicMock(),
        tenant=MagicMock(),
        worker_id="test",
        budget=MagicMock(),
        context_compressor=MagicMock(),
        prompt_builder=MagicMock(),
    )
    base.update(overrides)
    return AgentHarness(**base)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates && python -m pytest tests/harness/test_harness_summarizer_wiring.py -v
```
Expected: FAIL with `assert "turn_summarizer" in sig.parameters`.

- [ ] **Step 3: Add the constructor parameter**

In `surogates/harness/loop.py`, extend `AgentHarness.__init__`:

a. Add to the kwargs block (after `advisor_max_tokens` near line 867):

```python
        advisor_max_tokens: int = 700,
        turn_summarizer: Any | None = None,
    ) -> None:
```

b. Inside `__init__`, after the existing advisor field assignments around line 897, add:

```python
        self._turn_summarizer = turn_summarizer
        # Background tasks for in-flight iteration summaries, keyed by
        # iteration_index for the current turn. Drained at turn end.
        self._pending_iteration_summary_tasks: dict[int, asyncio.Task[Any]] = {}
        self._completed_iteration_summaries: dict[int, str] = {}
        self._pending_turn_summary_task: asyncio.Task[Any] | None = None
```

Also make sure `import asyncio` is at the top of `loop.py` (it should already be — check with grep before adding).

c. In `surogates/orchestrator/worker.py`, modify the `AgentHarness(...)` construction at line 830 to pass the summarizer when the summary auxiliary is available and the setting is on:

```python
        # ── after the existing build_summary_auxiliary_llm call ──
        from surogates.harness.turn_summarizer import TurnSummarizer

        if (
            summary_auxiliary is not None
            and settings.worker.emit_turn_summaries
        ):
            turn_summarizer: TurnSummarizer | None = TurnSummarizer(
                summary_client=summary_auxiliary.client,
                summary_model=summary_auxiliary.model,
            )
        else:
            turn_summarizer = None

        return AgentHarness(
            ...existing kwargs...,
            advisor_max_tokens=settings.llm.advisor_max_tokens,
            turn_summarizer=turn_summarizer,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /work/surogates && python -m pytest tests/harness/test_harness_summarizer_wiring.py -v
```
Expected: all PASS.

- [ ] **Step 5: Re-run the harness suite to catch regressions**

```bash
cd /work/surogates && python -m pytest tests/harness/ -v 2>&1 | tail -30
```
Expected: all green.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates && git add surogates/harness/loop.py surogates/orchestrator/worker.py tests/harness/test_harness_summarizer_wiring.py && git commit -m "feat(harness): wire TurnSummarizer into AgentHarness via worker"
```

---

### Task A7: Emit `iteration.summary` after each iteration's tool batch resolves

**Files:**
- Modify: `surogates/harness/loop.py` — the bottom of `_run_iteration` (search for where `tool.result` events finish and the loop is about to start the next iteration; typically right before the `continue` that loops back to the top of `while self._budget.remaining > 0`)
- Test: `tests/harness/test_iteration_summary_emit.py`

- [ ] **Step 1: Find the right hook point**

```bash
cd /work/surogates && grep -n "EventType.TOOL_RESULT\|# .*iteration\|while self._budget" surogates/harness/loop.py | head -20
```
Locate the spot where the iteration concludes — the last `await self._store.emit_event(... EventType.TOOL_RESULT ...)` before the `while` loop ticks. The summarizer hook fires AFTER all tool_results for the iteration have landed, before the next `iteration += 1`.

- [ ] **Step 2: Write the failing test**

Create `tests/harness/test_iteration_summary_emit.py`:

```python
"""iteration.summary is emitted after each LLM iteration completes."""
from __future__ import annotations

import pytest

from surogates.session.events import EventType


@pytest.mark.asyncio
async def test_iteration_summary_emitted_for_each_iteration(
    fake_harness_turn, stub_turn_summarizer
):
    """Two-iteration turn produces two iteration.summary events."""
    stub_turn_summarizer.iteration_responses = [
        "Outline the patch plan",
        "Apply the hero rewrite",
    ]
    events = await fake_harness_turn(
        turn_summarizer=stub_turn_summarizer,
        responses=[
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "todo", "arguments": "{\"action\":\"list\"}"}},
            ]},
            {"role": "assistant", "content": "done"},
        ],
    )
    summaries = [e for e in events if e["type"] == EventType.ITERATION_SUMMARY.value]
    assert len(summaries) == 2
    assert summaries[0]["data"]["iteration_index"] == 0
    assert summaries[0]["data"]["summary"] == "Outline the patch plan"
    assert summaries[1]["data"]["iteration_index"] == 1
    assert summaries[1]["data"]["summary"] == "Apply the hero rewrite"
    # Both share the turn_id stamped on llm.response
    responses = [e for e in events if e["type"] == EventType.LLM_RESPONSE.value]
    assert summaries[0]["data"]["turn_id"] == responses[0]["data"]["turn_id"]


@pytest.mark.asyncio
async def test_no_iteration_summary_when_summarizer_returns_none(
    fake_harness_turn, stub_turn_summarizer
):
    stub_turn_summarizer.iteration_responses = [None]
    events = await fake_harness_turn(
        turn_summarizer=stub_turn_summarizer,
        responses=[{"role": "assistant", "content": "hi"}],
    )
    summaries = [e for e in events if e["type"] == EventType.ITERATION_SUMMARY.value]
    assert summaries == []


@pytest.mark.asyncio
async def test_no_iteration_summary_when_summarizer_absent(fake_harness_turn):
    events = await fake_harness_turn(
        turn_summarizer=None,
        responses=[{"role": "assistant", "content": "hi"}],
    )
    summaries = [e for e in events if e["type"] == EventType.ITERATION_SUMMARY.value]
    assert summaries == []
```

Add the `stub_turn_summarizer` fixture to `tests/harness/conftest.py`:

```python
import pytest


class _StubTurnSummarizer:
    def __init__(self):
        self.iteration_responses: list[str | None] = []
        self.turn_response: object | None = "MISSING"
        self._iter_idx = 0

    async def summarize_iteration(self, **kwargs):
        if self._iter_idx >= len(self.iteration_responses):
            return None
        out = self.iteration_responses[self._iter_idx]
        self._iter_idx += 1
        return out

    async def summarize_turn(self, **kwargs):
        if self.turn_response == "MISSING":
            return None
        return self.turn_response


@pytest.fixture
def stub_turn_summarizer():
    return _StubTurnSummarizer()
```

The `fake_harness_turn` fixture (introduced in Task A4) must accept an additional optional `turn_summarizer` kwarg and pass it through to the `AgentHarness` constructor. Update it accordingly in `tests/harness/conftest.py`.

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd /work/surogates && python -m pytest tests/harness/test_iteration_summary_emit.py -v
```
Expected: FAIL — no ITERATION_SUMMARY events are emitted yet.

- [ ] **Step 4: Add the hook in `_run_iteration`**

In `surogates/harness/loop.py`, immediately after the iteration's tool batch resolves (after all `tool.result` events have been emitted; identify the spot by tracing through `_run_iteration` from where `assistant_message["tool_calls"]` is iterated). Add a helper method on `AgentHarness`:

```python
    async def _maybe_summarize_iteration(
        self,
        *,
        session_id: UUID,
        turn_id: str,
        iteration_index: int,
        reasoning_text: str,
        tool_calls: list[dict[str, Any]],
        tool_call_ids: list[str],
        started_at: str,
    ) -> None:
        """Fire-and-forget per-iteration summary.

        When the summary resolves, emit ``ITERATION_SUMMARY``. Tracked
        in ``_pending_iteration_summary_tasks`` so the turn drain step
        can await it.
        """
        if self._turn_summarizer is None:
            return
        # Snapshot summaries that have already resolved for earlier
        # iterations. Later summaries may still be pending; those are
        # intentionally excluded so this call never blocks the next LLM
        # iteration.
        prior_summaries = [
            self._completed_iteration_summaries[idx]
            for idx in sorted(self._completed_iteration_summaries)
            if idx < iteration_index
        ]

        async def _run() -> None:
            summary = await self._turn_summarizer.summarize_iteration(
                iteration_id=f"{turn_id}:{iteration_index}",
                reasoning=reasoning_text,
                tool_calls=tool_calls,
                prior_iteration_summaries=prior_summaries,
            )
            if summary is None:
                return
            self._completed_iteration_summaries[iteration_index] = summary
            from datetime import datetime, timezone
            await self._store.emit_event(
                session_id,
                EventType.ITERATION_SUMMARY,
                {
                    "turn_id": turn_id,
                    "iteration_index": iteration_index,
                    "summary": summary,
                    "tool_call_ids": tool_call_ids,
                    "started_at": started_at,
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                },
            )

        task = asyncio.create_task(_run())
        self._pending_iteration_summary_tasks[iteration_index] = task
```

Then, at the iteration-end hook point (after the tool batch resolves; carry forward the local variables `iteration` (one-based), `reasoning_text`, `assistant_message.get("tool_calls") or []`, and an `iteration_started_at` timestamp captured at the top of the iteration), invoke:

```python
            await self._maybe_summarize_iteration(
                session_id=session.id,
                turn_id=turn_id,
                iteration_index=iteration - 1,
                reasoning_text=reasoning_text or "",
                tool_calls=assistant_message.get("tool_calls") or [],
                tool_call_ids=[tc.get("id", "") for tc in (assistant_message.get("tool_calls") or [])],
                started_at=iteration_started_at,
            )
```

Capture `iteration_started_at` at the top of each iteration body:

```python
        while self._budget.remaining > 0:
            iteration += 1
            from datetime import datetime, timezone
            iteration_started_at = datetime.now(timezone.utc).isoformat()
            ...
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /work/surogates && python -m pytest tests/harness/test_iteration_summary_emit.py -v
```
Expected: all three tests PASS. (The first test requires awaiting the background tasks — the `fake_harness_turn` fixture should call `await asyncio.gather(*harness._pending_iteration_summary_tasks.values())` before returning events, so the tests observe the summary events deterministically.)

- [ ] **Step 6: Run the broader harness suite**

```bash
cd /work/surogates && python -m pytest tests/harness/ -v 2>&1 | tail -30
```
Expected: all green.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates && git add surogates/harness/loop.py tests/harness/test_iteration_summary_emit.py tests/harness/conftest.py && git commit -m "feat(harness): emit iteration.summary after each LLM iteration"
```

---

### Task A8: Emit `turn.summary` after the final iteration; drain in `_complete_session`

**Files:**
- Modify: `surogates/harness/loop.py` — `_complete_session` (line 5150) plus the call sites that reach it
- Test: `tests/harness/test_turn_summary_emit.py`

- [ ] **Step 1: Write the failing test**

Create `tests/harness/test_turn_summary_emit.py`:

```python
"""turn.summary is emitted after the final iteration of a successful turn."""
from __future__ import annotations

import pytest

from surogates.harness.turn_summarizer import TurnArtifact, TurnSummary
from surogates.session.events import EventType


@pytest.mark.asyncio
async def test_turn_summary_emitted_after_session_complete(
    fake_harness_turn, stub_turn_summarizer
):
    stub_turn_summarizer.iteration_responses = ["Wrote the hero rewrite"]
    stub_turn_summarizer.turn_response = TurnSummary(
        recap="Reworked the hero around brain/hands.",
        artifacts=[
            TurnArtifact(kind="file", label="landing.html", ref="landing.html"),
        ],
    )
    events = await fake_harness_turn(
        turn_summarizer=stub_turn_summarizer,
        responses=[{"role": "assistant", "content": "done"}],
    )

    turn_summaries = [e for e in events if e["type"] == EventType.TURN_SUMMARY.value]
    assert len(turn_summaries) == 1
    payload = turn_summaries[0]["data"]
    assert payload["recap"].startswith("Reworked the hero")
    assert payload["artifacts"] == [
        {"kind": "file", "label": "landing.html", "ref": "landing.html"},
    ]
    # turn_id matches the llm.response turn_id
    responses = [e for e in events if e["type"] == EventType.LLM_RESPONSE.value]
    assert payload["turn_id"] == responses[0]["data"]["turn_id"]
    # turn.summary must appear in the event log; ordering relative to
    # session.complete is not asserted because the drain pattern may emit
    # turn.summary either just before or just after session.complete.


@pytest.mark.asyncio
async def test_no_turn_summary_when_summarizer_returns_none(
    fake_harness_turn, stub_turn_summarizer
):
    stub_turn_summarizer.iteration_responses = ["x"]
    stub_turn_summarizer.turn_response = None
    events = await fake_harness_turn(
        turn_summarizer=stub_turn_summarizer,
        responses=[{"role": "assistant", "content": "done"}],
    )
    assert not any(e["type"] == EventType.TURN_SUMMARY.value for e in events)


@pytest.mark.asyncio
async def test_no_turn_summary_when_summarizer_absent(fake_harness_turn):
    events = await fake_harness_turn(
        turn_summarizer=None,
        responses=[{"role": "assistant", "content": "done"}],
    )
    assert not any(e["type"] == EventType.TURN_SUMMARY.value for e in events)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /work/surogates && python -m pytest tests/harness/test_turn_summary_emit.py -v
```
Expected: FAIL — no TURN_SUMMARY events emitted.

- [ ] **Step 3: Add the turn-drain and emission logic**

In `surogates/harness/loop.py`, add a helper:

```python
    async def _drain_and_emit_turn_summary(
        self,
        *,
        session_id: UUID,
        turn_id: str,
        user_message: str,
    ) -> None:
        """Drain pending iteration-summary tasks, then run turn summary.

        Soft 10s cap per pending task — timed-out summaries are silently
        omitted (their iteration row stays expanded client-side).
        """
        if self._turn_summarizer is None:
            return

        pending = list(self._pending_iteration_summary_tasks.values())
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Iteration summary drain timed out for turn %s", turn_id,
                )

        # Read back the actually-emitted iteration summaries by querying the
        # event store (they may be sparse if some timed out / returned None).
        iter_events = await self._store.get_events(
            session_id,
            types=[EventType.ITERATION_SUMMARY],
        )
        ordered = sorted(
            (e for e in iter_events if (e.data or {}).get("turn_id") == turn_id),
            key=lambda e: (e.data or {}).get("iteration_index", 0),
        )
        iteration_summaries = [(e.data or {}).get("summary", "") for e in ordered]
        candidate_artifacts = await self._collect_candidate_artifacts(
            session_id=session_id, turn_id=turn_id,
        )

        try:
            result = await asyncio.wait_for(
                self._turn_summarizer.summarize_turn(
                    turn_id=turn_id,
                    user_message=user_message,
                    iteration_summaries=iteration_summaries,
                    candidate_artifacts=candidate_artifacts,
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Turn summary timed out for %s", turn_id)
            return
        if result is None:
            return

        await self._store.emit_event(
            session_id,
            EventType.TURN_SUMMARY,
            {
                "turn_id": turn_id,
                "recap": result.recap,
                "artifacts": [
                    {"kind": a.kind, "label": a.label, "ref": a.ref}
                    for a in result.artifacts
                ],
            },
        )

    async def _collect_candidate_artifacts(
        self, *, session_id: UUID, turn_id: str,
    ) -> list[Any]:
        """Pull notable tool calls / artifacts emitted during this turn.

        Returns ``TurnArtifact``-shaped objects from
        ``surogates.harness.turn_summarizer``.
        """
        from surogates.harness.turn_summarizer import TurnArtifact

        artifacts: list[TurnArtifact] = []
        events = await self._store.get_events(session_id)
        in_turn = False
        for e in events:
            data = e.data or {}
            if data.get("turn_id") == turn_id:
                in_turn = True
            if not in_turn:
                continue
            etype = e.type
            if etype == EventType.TOOL_CALL.value:
                name = data.get("name") or ""
                args = data.get("arguments") or {}
                if name in {"write_file", "patch", "create_artifact"}:
                    path = args.get("path") or args.get("file_path") or args.get("name") or ""
                    if path:
                        artifacts.append(TurnArtifact(
                            kind="file" if name != "create_artifact" else "artifact",
                            label=path, ref=path,
                        ))
                elif name in {"web_extract", "web_crawl"}:
                    url = args.get("url") or ""
                    if url:
                        artifacts.append(TurnArtifact(kind="url", label=url, ref=url))
                elif name == "terminal":
                    cmd = args.get("command") or ""
                    tc_id = data.get("tool_call_id") or ""
                    if cmd and tc_id:
                        artifacts.append(TurnArtifact(
                            kind="command", label=cmd[:80], ref=tc_id,
                        ))
        return artifacts
```

Then call `_drain_and_emit_turn_summary` from `_complete_session`. Add to `_complete_session` (in the parameter block, then near the top of the body, before `await self._store.emit_event(session.id, EventType.SESSION_COMPLETE, complete_data)`):

```python
    async def _complete_session(
        self,
        session: Session,
        messages: list[dict],
        lease: SessionLease,
        *,
        reason: str,
        through_event_id: int | None = None,
        cost_tracker: SessionCostTracker | None = None,
        turn_id: str | None = None,
        user_message: str = "",
    ) -> None:
        # ...existing cleanup...

        if turn_id is not None and reason in {"stop", "done", "complete", "completed"}:
            try:
                await self._drain_and_emit_turn_summary(
                    session_id=session.id,
                    turn_id=turn_id,
                    user_message=user_message,
                )
            except Exception:
                logger.exception("Turn summary drain failed for %s", session.id)

        complete_data: dict[str, Any] = {
            ...existing...
        }
```

Every call site for `_complete_session` in `loop.py` (lines 2012, 2206, 2264, 2418, 3993) needs to pass `turn_id=turn_id` and `user_message=<latest user message text in scope>`. The latest user message can be read from `messages[-N]` where `messages` is the in-scope conversation list — find the most recent `{"role": "user", "content": ...}` entry and pass `content` as a string.

You'll also need to ensure `_pending_iteration_summary_tasks` is cleared at the start of `wake()` (before the loop) so a paused-and-resumed session doesn't reuse stale tasks:

```python
        # Inside wake(), just before the `iteration = 0` line:
        self._pending_iteration_summary_tasks = {}
        self._completed_iteration_summaries = {}
```

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
cd /work/surogates && python -m pytest tests/harness/test_turn_summary_emit.py -v
```
Expected: all three tests PASS.

- [ ] **Step 5: Run the full harness suite to catch regressions**

```bash
cd /work/surogates && python -m pytest tests/harness/ -v 2>&1 | tail -40
```
Expected: all green.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates && git add surogates/harness/loop.py tests/harness/test_turn_summary_emit.py && git commit -m "feat(harness): emit turn.summary on session completion"
```

---

### Task A9: Gate everything on the `emit_turn_summaries` setting (end-to-end integration test)

**Files:**
- Test: `tests/harness/test_emit_turn_summaries_gate.py`

- [ ] **Step 1: Write the failing test**

Create `tests/harness/test_emit_turn_summaries_gate.py`:

```python
"""When emit_turn_summaries=False, the worker builds no TurnSummarizer."""
from __future__ import annotations

import pytest

# NOTE: this test exercises the worker.py wiring rather than the harness
# directly. If your test infra has a ``build_harness_for_settings`` style
# fixture, use it; otherwise, monkeypatch settings.worker.emit_turn_summaries
# and assert that ``AgentHarness._turn_summarizer is None`` on the
# resulting harness.


@pytest.mark.asyncio
async def test_emit_turn_summaries_false_disables_summarizer(
    worker_harness_for_settings, settings_with_summary_model,
):
    settings_with_summary_model.worker.emit_turn_summaries = False
    harness = await worker_harness_for_settings(settings_with_summary_model)
    assert harness._turn_summarizer is None


@pytest.mark.asyncio
async def test_emit_turn_summaries_true_builds_summarizer(
    worker_harness_for_settings, settings_with_summary_model,
):
    settings_with_summary_model.worker.emit_turn_summaries = True
    harness = await worker_harness_for_settings(settings_with_summary_model)
    assert harness._turn_summarizer is not None
```

If `worker_harness_for_settings` and `settings_with_summary_model` fixtures don't already exist in `tests/harness/conftest.py` or `tests/conftest.py`, add them as minimal scaffolds that construct the harness via the same code path `worker.py` uses (`harness_factory`).

- [ ] **Step 2: Run tests to verify they pass**

```bash
cd /work/surogates && python -m pytest tests/harness/test_emit_turn_summaries_gate.py -v
```
Expected: both PASS (the gating logic was added in Task A6).

- [ ] **Step 3: Commit**

```bash
cd /work/surogates && git add tests/harness/test_emit_turn_summaries_gate.py tests/harness/conftest.py && git commit -m "test(harness): verify emit_turn_summaries gate"
```

---

## Phase B — SDK changes

All paths in this phase are relative to `/work/surogates/sdk/agent-chat-react/`.

### Task B1: Add new event types to SDK types + listened events

**Files:**
- Modify: `src/types.ts:357-389` (AgentChatEventType union)
- Modify: `src/runtime/events.ts:11-44` (AGENT_CHAT_LISTENED_EVENTS)
- Test: `tests/events.test.ts` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/events.test.ts`:

```ts
import { describe, it, expect } from "vitest";

import { AGENT_CHAT_LISTENED_EVENTS } from "../src/runtime/events";

describe("AGENT_CHAT_LISTENED_EVENTS", () => {
  it("includes iteration.summary", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("iteration.summary");
  });

  it("includes turn.summary", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("turn.summary");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/events.test.ts
```
Expected: FAIL — values missing.

- [ ] **Step 3: Add the event types**

In `src/types.ts`, extend the `AgentChatEventType` union (line 389 currently ends with `"clarify.response";`):

```ts
export type AgentChatEventType =
  | "user.message"
  ...existing...
  | "clarify.response"
  | "iteration.summary"
  | "turn.summary";
```

In `src/runtime/events.ts`, append to `AGENT_CHAT_LISTENED_EVENTS`:

```ts
export const AGENT_CHAT_LISTENED_EVENTS = [
  ...existing entries...
  "clarify.response",
  "iteration.summary",
  "turn.summary",
] as const satisfies readonly AgentChatEventType[];
```

- [ ] **Step 4: Run tests and typecheck to verify**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/events.test.ts && npm run typecheck
```
Expected: PASS, typecheck clean.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates/sdk/agent-chat-react && git add src/types.ts src/runtime/events.ts tests/events.test.ts && git commit -m "feat(types): add iteration.summary and turn.summary event types"
```

---

### Task B2: Add summary types and extend `AgentChatMessage` / `AgentChatState`

**Files:**
- Modify: `src/types.ts` (insert after the existing tool/error types, around line 127; extend `AgentChatMessage` at lines 61-76; extend `AgentChatState` at lines 418-430)
- Test: covered by later tasks' tests; no dedicated test needed for type additions

- [ ] **Step 1: Add the new types**

In `src/types.ts`, add after the `AgentChatErrorInfo` interface (around line 126):

```ts
export interface AgentChatIterationSummary {
  iterationIndex: number;
  summary: string;
  toolCallIds: string[];
  startedAt: string;
  endedAt: string;
}

export type AgentChatTurnArtifactKind = "file" | "artifact" | "url" | "command";

export interface AgentChatTurnArtifactRef {
  kind: AgentChatTurnArtifactKind;
  label: string;
  ref: string;
  meta?: Record<string, unknown>;
}

export interface AgentChatTurnSummary {
  turnId: string;
  recap: string;
  artifacts: AgentChatTurnArtifactRef[];
}
```

Extend `AgentChatMessage` (lines 61-76) by adding fields:

```ts
export interface AgentChatMessage {
  id: string;
  role: AgentChatRole;
  content: string;
  createdAt: Date;
  status: AgentChatMessageStatus;
  toolCalls?: AgentChatToolCallInfo[];
  reasoning?: string;
  systemKind?: AgentChatSystemKind;
  systemMeta?: Record<string, unknown>;
  errorInfo?: AgentChatErrorInfo;
  images?: AgentChatImageAttachment[];
  attachments?: AgentChatDisplayAttachment[];
  llmResponseEventId?: number;
  userFeedback?: { rating: "up" | "down"; reason?: string };
  turnId?: string;
  iterationIndex?: number;
  iterationSummary?: AgentChatIterationSummary;
  turnSummary?: AgentChatTurnSummary;
}
```

Extend `AgentChatState` (lines 418-430):

```ts
export interface AgentChatState {
  messages: AgentChatMessage[];
  isRunning: boolean;
  isLoadingHistory: boolean;
  tokenUsage: AgentChatTokenUsage;
  retryIndicator: AgentChatRetryIndicator | null;
  lastEventId: number;
  sessionDone: boolean;
  hadDeltas: boolean;
  terminal: boolean;
  workspaceRefreshKey: number;
  browser: AgentChatBrowserState | null;
  viewMode: "simple" | "expert";
}
```

Add convenience re-exports at the bottom of `src/types.ts` (near the `ChatMessage = AgentChatMessage` block around line 729):

```ts
export type IterationSummary = AgentChatIterationSummary;
export type TurnSummary = AgentChatTurnSummary;
export type TurnArtifactRef = AgentChatTurnArtifactRef;
export type TurnArtifactKind = AgentChatTurnArtifactKind;
```

- [ ] **Step 2: Update `createInitialAgentChatState`**

In `src/runtime/reducer.ts`, find `createInitialAgentChatState` and add `viewMode: "simple"` to the returned state (the default is "simple"; the runtime hook will overwrite it once it loads a persisted preference).

```bash
cd /work/surogates/sdk/agent-chat-react && grep -n "createInitialAgentChatState" src/runtime/reducer.ts | head -5
```
Edit the function body so the returned object includes `viewMode: "simple"`.

- [ ] **Step 3: Typecheck**

```bash
cd /work/surogates/sdk/agent-chat-react && npm run typecheck
```
Expected: clean (existing call sites still compile because the new fields are optional / the discriminated union is preserved).

- [ ] **Step 4: Commit**

```bash
cd /work/surogates/sdk/agent-chat-react && git add src/types.ts src/runtime/reducer.ts && git commit -m "feat(types): add IterationSummary, TurnSummary, viewMode"
```

---

### Task B3: Reducer — stamp `turnId` and `iterationIndex` on assistant messages

**Files:**
- Modify: `src/runtime/reducer.ts` — `applyLlmDelta` (line 385), `applyLlmThinking`, `applyLlmResponse` (line 433)
- Test: `tests/reducer.test.ts` (extend)

- [ ] **Step 1: Write the failing test**

Extend `tests/reducer.test.ts` (find an appropriate `describe` block to nest under — look at the existing structure first):

```bash
cd /work/surogates/sdk/agent-chat-react && grep -n "describe\|llm.delta\|llm.response" tests/reducer.test.ts | head -20
```

Add a new describe block:

```ts
describe("llm event metadata stamping", () => {
  it("stamps turnId and iterationIndex from llm.delta on the streaming message", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.delta",
      eventId: 1,
      data: {
        content: "hi",
        turn_id: "t-1",
        iteration_index: 0,
      },
    });
    expect(state.messages.at(-1)?.turnId).toBe("t-1");
    expect(state.messages.at(-1)?.iterationIndex).toBe(0);
  });

  it("stamps turnId and iterationIndex from llm.response", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 1,
      data: {
        message: { role: "assistant", content: "hi", tool_calls: [] },
        turn_id: "t-2",
        iteration_index: 3,
      },
    });
    expect(state.messages.at(-1)?.turnId).toBe("t-2");
    expect(state.messages.at(-1)?.iterationIndex).toBe(3);
  });

  it("stamps turnId from llm.thinking on the message under construction", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.thinking",
      eventId: 1,
      data: {
        reasoning: "let me think",
        turn_id: "t-3",
        iteration_index: 0,
      },
    });
    expect(state.messages.at(-1)?.turnId).toBe("t-3");
    expect(state.messages.at(-1)?.iterationIndex).toBe(0);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/reducer.test.ts -t "llm event metadata stamping"
```
Expected: FAIL — fields undefined.

- [ ] **Step 3: Patch the reducer**

In `src/runtime/reducer.ts`, near the top, add a helper:

```ts
function readTurnMeta(data: Record<string, unknown>): {
  turnId?: string;
  iterationIndex?: number;
} {
  const turnId = typeof data.turn_id === "string" ? data.turn_id : undefined;
  const iterationIndex = typeof data.iteration_index === "number"
    ? data.iteration_index
    : undefined;
  return { turnId, iterationIndex };
}
```

In `applyLlmDelta` (line 385), inside the `if (canAppend && lastMsg)` branch and the `else` (new-message) branch, attach the metadata:

```ts
  const { turnId, iterationIndex } = readTurnMeta(event.data);

  if (canAppend && lastMsg) {
    messages[lastIdx] = {
      ...lastMsg,
      content: deltaContent ? lastMsg.content + deltaContent : lastMsg.content,
      reasoning: deltaReasoning
        ? (lastMsg.reasoning ?? "") + deltaReasoning
        : lastMsg.reasoning,
      turnId: turnId ?? lastMsg.turnId,
      iterationIndex: iterationIndex ?? lastMsg.iterationIndex,
    };
  } else {
    messages.push({
      id: `evt-${event.eventId}`,
      role: "assistant",
      content: deltaContent,
      reasoning: deltaReasoning || undefined,
      createdAt: new Date(),
      status: "streaming",
      turnId,
      iterationIndex,
    });
  }
```

Apply the same `turnId ?? lastMsg.turnId` merge pattern in `applyLlmThinking` and `applyLlmResponse`. In `applyLlmResponse`, after `messages[idx] = { ...current, ...}` in both the tool-calls branch and the completion branch, add:

```ts
      messages[idx] = {
        ...messages[idx],
        turnId: turnId ?? messages[idx].turnId,
        iterationIndex: iterationIndex ?? messages[idx].iterationIndex,
      };
```

Critically, **do not** overwrite an existing `iterationSummary` or `turnSummary` when a later `llm.response` rehydrates the same message (the `matchesExistingToolTurn` path). Verify the existing `{ ...current, ... }` spread preserves these — it should, because the spread copies all fields and only the explicitly-named ones change. Add a regression test:

```ts
  it("preserves an attached iterationSummary across llm.response rehydration", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 1,
      data: {
        message: { role: "assistant", content: "", tool_calls: [
          { id: "c1", type: "function",
            function: { name: "todo", arguments: "{}" } },
        ] },
        turn_id: "t-1",
        iteration_index: 0,
      },
    });
    state = applyAgentChatEvent(state, {
      type: "iteration.summary",
      eventId: 2,
      data: {
        turn_id: "t-1",
        iteration_index: 0,
        summary: "Outlined the plan",
        tool_call_ids: ["c1"],
        started_at: "2026-05-24T00:00:00Z",
        ended_at: "2026-05-24T00:00:01Z",
      },
    });
    // Simulate the matchesExistingToolTurn rehydration path:
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 3,
      data: {
        message: { role: "assistant", content: "", tool_calls: [
          { id: "c1", type: "function",
            function: { name: "todo", arguments: "{}" } },
        ] },
        turn_id: "t-1",
        iteration_index: 0,
      },
    });
    expect(state.messages.at(-1)?.iterationSummary?.summary).toBe("Outlined the plan");
  });
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/reducer.test.ts
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates/sdk/agent-chat-react && git add src/runtime/reducer.ts tests/reducer.test.ts && git commit -m "feat(reducer): stamp turnId and iterationIndex on assistant messages"
```

---

### Task B4: Reducer handlers for `iteration.summary` and `turn.summary`

**Files:**
- Modify: `src/runtime/reducer.ts` (dispatch switch around line 95, plus new helper functions)
- Test: `tests/reducer.test.ts` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/reducer.test.ts`:

```ts
describe("iteration.summary and turn.summary events", () => {
  it("attaches iteration.summary to the matching assistant message", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 1,
      data: {
        message: { role: "assistant", content: "hi", tool_calls: [] },
        turn_id: "t-1",
        iteration_index: 0,
      },
    });
    state = applyAgentChatEvent(state, {
      type: "iteration.summary",
      eventId: 2,
      data: {
        turn_id: "t-1",
        iteration_index: 0,
        summary: "Said hi",
        tool_call_ids: [],
        started_at: "2026-05-24T00:00:00Z",
        ended_at: "2026-05-24T00:00:01Z",
      },
    });
    expect(state.messages.at(-1)?.iterationSummary?.summary).toBe("Said hi");
  });

  it("handles iteration.summary arriving before the matching message (no-op)", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "iteration.summary",
      eventId: 1,
      data: {
        turn_id: "t-1",
        iteration_index: 0,
        summary: "Stale",
        tool_call_ids: [],
        started_at: "2026-05-24T00:00:00Z",
        ended_at: "2026-05-24T00:00:01Z",
      },
    });
    expect(state.messages).toHaveLength(0);
  });

  it("attaches turn.summary to the last assistant message with the turn_id", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 1,
      data: {
        message: { role: "assistant", content: "", tool_calls: [
          { id: "c1", type: "function",
            function: { name: "todo", arguments: "{}" } },
        ] },
        turn_id: "t-1",
        iteration_index: 0,
      },
    });
    // Simulate a second iteration: a fresh assistant message gets pushed
    // because the prior one has a tool batch attached.
    state = applyAgentChatEvent(state, {
      type: "tool.result",
      eventId: 2,
      data: { tool_call_id: "c1", content: "{}" },
    });
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 3,
      data: {
        message: { role: "assistant", content: "done", tool_calls: [] },
        turn_id: "t-1",
        iteration_index: 1,
      },
    });
    state = applyAgentChatEvent(state, {
      type: "turn.summary",
      eventId: 4,
      data: {
        turn_id: "t-1",
        recap: "Did the thing.",
        artifacts: [{ kind: "file", label: "x.md", ref: "x.md" }],
      },
    });
    // turn.summary attaches to the tail message with turnId="t-1"
    const tail = state.messages.at(-1);
    expect(tail?.turnSummary?.recap).toBe("Did the thing.");
    expect(tail?.turnSummary?.artifacts).toHaveLength(1);
    // Earlier message in the same turn does not get turnSummary
    expect(state.messages[0]?.turnSummary).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/reducer.test.ts -t "iteration.summary and turn.summary"
```
Expected: FAIL — events not handled.

- [ ] **Step 3: Add the handlers**

In `src/runtime/reducer.ts`, add cases to the main dispatch switch (insert near the other `case "..."` lines around line 130):

```ts
    case "iteration.summary":
      return applyIterationSummary(nextState, event);

    case "turn.summary":
      return applyTurnSummary(nextState, event);
```

Add helper functions at the bottom of the file:

```ts
function applyIterationSummary(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  const turnId = stringValue(event.data.turn_id);
  const iterationIndex = optionalNumberValue(event.data.iteration_index);
  const summary = stringValue(event.data.summary);
  if (!turnId || iterationIndex === null || !summary) return state;
  const idx = state.messages.findIndex(
    (m) =>
      m.role === "assistant" &&
      m.turnId === turnId &&
      m.iterationIndex === iterationIndex,
  );
  if (idx < 0) return state;
  const messages = [...state.messages];
  messages[idx] = {
    ...messages[idx],
    iterationSummary: {
      iterationIndex,
      summary,
      toolCallIds: Array.isArray(event.data.tool_call_ids)
        ? (event.data.tool_call_ids as unknown[]).map((x) => String(x))
        : [],
      startedAt: stringValue(event.data.started_at) ?? "",
      endedAt: stringValue(event.data.ended_at) ?? "",
    },
  };
  return { ...state, messages };
}

function applyTurnSummary(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  const turnId = stringValue(event.data.turn_id);
  if (!turnId) return state;
  const recap = stringValue(event.data.recap) ?? "";
  const rawArtifacts = Array.isArray(event.data.artifacts)
    ? (event.data.artifacts as unknown[])
    : [];
  const artifacts: AgentChatTurnArtifactRef[] = [];
  for (const a of rawArtifacts) {
    if (!a || typeof a !== "object") continue;
    const obj = a as Record<string, unknown>;
    const kind = obj.kind;
    if (kind !== "file" && kind !== "artifact" && kind !== "url" && kind !== "command") continue;
    const label = typeof obj.label === "string" ? obj.label : "";
    const ref = typeof obj.ref === "string" ? obj.ref : "";
    if (!label || !ref) continue;
    artifacts.push({ kind, label, ref });
  }
  // Find the LAST assistant message with this turnId.
  let idx = -1;
  for (let i = state.messages.length - 1; i >= 0; i--) {
    const m = state.messages[i];
    if (m.role === "assistant" && m.turnId === turnId) { idx = i; break; }
  }
  if (idx < 0) return state;
  const messages = [...state.messages];
  messages[idx] = {
    ...messages[idx],
    turnSummary: { turnId, recap, artifacts },
  };
  return { ...state, messages };
}

function optionalNumberValue(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}
```

If `stringValue` doesn't already exist as a helper, find its existing implementation in the file (grep) and reuse it; otherwise add the standard `typeof v === "string" ? v : null` shape.

Also import the new type:

```ts
import type {
  AgentChatTurnArtifactRef,
  // ...existing imports...
} from "../types";
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/reducer.test.ts
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates/sdk/agent-chat-react && git add src/runtime/reducer.ts tests/reducer.test.ts && git commit -m "feat(reducer): handle iteration.summary and turn.summary events"
```

---

### Task B5: View-mode runtime state, adapter methods, localStorage fallback

**Files:**
- Modify: `src/types.ts` — `AgentChatAdapter` interface (around line 563); `AgentChatRuntimeApi` (line 709)
- Modify: `src/runtime/use-agent-chat-runtime.ts`
- Test: `tests/view-mode.test.tsx` (new)

- [ ] **Step 1: Add adapter methods + runtime API field**

In `src/types.ts`, add to `AgentChatAdapter`:

```ts
export interface AgentChatAdapter {
  // ...existing methods...
  closeBrowserSession?(sessionId: string): Promise<void>;
  getChatViewMode?(): Promise<"simple" | "expert" | null>;
  setChatViewMode?(mode: "simple" | "expert"): Promise<void>;
}
```

Extend `AgentChatRuntimeApi` (line 709) with:

```ts
export interface AgentChatRuntimeApi {
  // ...existing fields...
  markSendError(errorText: string): void;
  viewMode: "simple" | "expert";
  setViewMode(mode: "simple" | "expert"): void;
}
```

- [ ] **Step 2: Write the failing test**

Create `tests/view-mode.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

import { useAgentChatRuntime } from "../src/runtime/use-agent-chat-runtime";
import type { AgentChatAdapter } from "../src/types";

const VIEW_MODE_KEY = "@invergent/agent-chat-react:viewMode";

function makeAdapter(overrides: Partial<AgentChatAdapter> = {}): AgentChatAdapter {
  return {
    listSessions: vi.fn().mockResolvedValue({ sessions: [], total: 0 }),
    createSession: vi.fn(),
    getSession: vi.fn(),
    sendMessage: vi.fn(),
    openEventStream: vi.fn(() => ({
      addEventListener: vi.fn(),
      close: vi.fn(),
      onerror: null,
    })),
    getSessionEvents: vi.fn().mockResolvedValue({ events: [], total: 0 }),
    ...overrides,
  } as unknown as AgentChatAdapter;
}

describe("view mode", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("defaults to simple when no preference is stored", () => {
    const { result } = renderHook(() =>
      useAgentChatRuntime({ adapter: makeAdapter(), sessionId: null }),
    );
    expect(result.current.viewMode).toBe("simple");
  });

  it("falls back to localStorage when the adapter does not implement the method", () => {
    window.localStorage.setItem(VIEW_MODE_KEY, "expert");
    const { result } = renderHook(() =>
      useAgentChatRuntime({ adapter: makeAdapter(), sessionId: null }),
    );
    expect(result.current.viewMode).toBe("expert");
  });

  it("prefers the adapter when implemented", async () => {
    const adapter = makeAdapter({
      getChatViewMode: vi.fn().mockResolvedValue("expert"),
      setChatViewMode: vi.fn().mockResolvedValue(undefined),
    });
    const { result } = renderHook(() =>
      useAgentChatRuntime({ adapter, sessionId: null }),
    );
    // wait a tick for the async load
    await act(async () => { await Promise.resolve(); });
    expect(result.current.viewMode).toBe("expert");
  });

  it("setViewMode writes through to the adapter when present, localStorage otherwise", async () => {
    const setChatViewMode = vi.fn().mockResolvedValue(undefined);
    const adapter = makeAdapter({ setChatViewMode });
    const { result } = renderHook(() =>
      useAgentChatRuntime({ adapter, sessionId: null }),
    );
    act(() => {
      result.current.setViewMode("expert");
    });
    expect(result.current.viewMode).toBe("expert");
    expect(setChatViewMode).toHaveBeenCalledWith("expert");
  });

  it("setViewMode falls back to localStorage when the adapter lacks the method", () => {
    const { result } = renderHook(() =>
      useAgentChatRuntime({ adapter: makeAdapter(), sessionId: null }),
    );
    act(() => {
      result.current.setViewMode("expert");
    });
    expect(window.localStorage.getItem(VIEW_MODE_KEY)).toBe("expert");
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/view-mode.test.tsx
```
Expected: FAIL — `viewMode` undefined on the runtime API.

- [ ] **Step 4: Implement viewMode in the runtime hook**

In `src/runtime/use-agent-chat-runtime.ts`, add at the top of the file:

```ts
const VIEW_MODE_KEY = "@invergent/agent-chat-react:viewMode";

function readPersistedViewModeSync(): "simple" | "expert" {
  if (typeof window === "undefined") return "simple";
  const raw = window.localStorage.getItem(VIEW_MODE_KEY);
  return raw === "expert" ? "expert" : "simple";
}
```

In the hook body, initialise from localStorage so SSR/first-render is stable, then asynchronously upgrade to the adapter's value:

```ts
  const [viewMode, setViewModeState] = useState<"simple" | "expert">(() =>
    readPersistedViewModeSync(),
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!adapter.getChatViewMode) return;
      try {
        const persisted = await adapter.getChatViewMode();
        if (cancelled) return;
        if (persisted === "simple" || persisted === "expert") {
          setViewModeState(persisted);
        }
      } catch {
        /* swallow — fall through to localStorage / default */
      }
    })();
    return () => { cancelled = true; };
  }, [adapter]);

  const setViewMode = useCallback(
    (mode: "simple" | "expert") => {
      setViewModeState(mode);
      if (typeof window !== "undefined") {
        window.localStorage.setItem(VIEW_MODE_KEY, mode);
      }
      if (adapter.setChatViewMode) {
        adapter.setChatViewMode(mode).catch(() => {
          /* persistence failure is non-fatal */
        });
      }
    },
    [adapter],
  );
```

Add both to the returned API object:

```ts
  return {
    ...existingFields,
    viewMode,
    setViewMode,
  };
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/view-mode.test.tsx
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates/sdk/agent-chat-react && git add src/types.ts src/runtime/use-agent-chat-runtime.ts tests/view-mode.test.tsx && git commit -m "feat(runtime): persisted simple/expert viewMode with adapter+localStorage"
```

---

### Task B6: `IterationGroup` component

**Files:**
- Create: `src/components/chat/iteration-group.tsx`
- Test: `tests/iteration-group.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `tests/iteration-group.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { IterationGroup } from "../src/components/chat/iteration-group";
import type { AgentChatMessage } from "../src/types";

function makeMessage(overrides: Partial<AgentChatMessage> = {}): AgentChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "",
    createdAt: new Date(),
    status: "complete",
    ...overrides,
  };
}

describe("IterationGroup", () => {
  it("renders the iteration summary when present (collapsed)", () => {
    render(
      <IterationGroup
        message={makeMessage({
          turnId: "t-1",
          iterationIndex: 0,
          iterationSummary: {
            iterationIndex: 0,
            summary: "Rework hero paragraph",
            toolCallIds: ["c1"],
            startedAt: "",
            endedAt: "",
          },
          toolCalls: [{ id: "c1", toolName: "patch", args: "{}", status: "complete" }],
          reasoning: "long reasoning text",
        })}
        sessionId="s-1"
      />,
    );
    expect(screen.getByText("Rework hero paragraph")).toBeTruthy();
    // The reasoning content should not be visible until expanded
    expect(screen.queryByText(/long reasoning text/)).toBeNull();
  });

  it("expands to show reasoning and tool entries when clicked", () => {
    render(
      <IterationGroup
        message={makeMessage({
          turnId: "t-1",
          iterationIndex: 0,
          iterationSummary: {
            iterationIndex: 0, summary: "S",
            toolCallIds: [], startedAt: "", endedAt: "",
          },
          reasoning: "some reasoning",
        })}
        sessionId="s-1"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /S/ }));
    expect(screen.getByText("some reasoning")).toBeTruthy();
  });

  it("renders an animated 'Thinking…' placeholder while no summary and no tools", () => {
    render(
      <IterationGroup
        message={makeMessage({
          turnId: "t-1",
          iterationIndex: 0,
          status: "streaming",
        })}
        sessionId="s-1"
      />,
    );
    expect(screen.getByText(/Thinking/i)).toBeTruthy();
  });

  it("renders 'Working… (N tools)' while tools are running", () => {
    render(
      <IterationGroup
        message={makeMessage({
          turnId: "t-1",
          iterationIndex: 0,
          status: "streaming",
          toolCalls: [
            { id: "c1", toolName: "patch", args: "{}", status: "running" },
            { id: "c2", toolName: "read_file", args: "{}", status: "running" },
            { id: "c3", toolName: "todo", args: "{}", status: "running" },
          ],
        })}
        sessionId="s-1"
      />,
    );
    expect(screen.getByText(/Working/i)).toBeTruthy();
    expect(screen.getByText(/3 tools/)).toBeTruthy();
  });

  it("stays permanently expanded (no collapse trigger) when no summary and complete", () => {
    render(
      <IterationGroup
        message={makeMessage({
          turnId: "t-1",
          iterationIndex: 0,
          reasoning: "post-mortem reasoning",
        })}
        sessionId="s-1"
      />,
    );
    expect(screen.getByText("post-mortem reasoning")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/iteration-group.test.tsx
```
Expected: FAIL — module does not exist.

- [ ] **Step 3: Create the component**

Create `src/components/chat/iteration-group.tsx`:

```tsx
import { useState } from "react";

import { Shimmer } from "../ai-elements/shimmer";
import { Reasoning, ReasoningContent, ReasoningTrigger } from "../ai-elements/reasoning";
import { cn } from "../../lib/utils";
import { ChevronRight } from "lucide-react";
import { ToolCallBlock } from "./tool-call-block";
import { effectiveStatus } from "./tools/shared";
import type { AgentChatMessage } from "../../types";

export interface IterationGroupProps {
  message: AgentChatMessage;
  sessionId: string | null;
  onFileSelect?: (path: string) => void;
}

function worstStatus(message: AgentChatMessage): "running" | "complete" | "error" | "cancelled" {
  const calls = message.toolCalls ?? [];
  if (calls.length === 0) {
    if (message.status === "streaming") return "running";
    if (message.status === "error") return "error";
    return "complete";
  }
  let any_error = false;
  let any_running = false;
  for (const tc of calls) {
    const s = effectiveStatus(tc);
    if (s === "error") any_error = true;
    if (s === "running") any_running = true;
  }
  if (any_error) return "error";
  if (any_running) return "running";
  return "complete";
}

function dotClass(status: "running" | "complete" | "error" | "cancelled"): string {
  if (status === "running") return "bg-primary animate-pulse";
  if (status === "error") return "bg-red-500";
  if (status === "cancelled") return "bg-muted-foreground/40";
  return "bg-emerald-500";
}

export function IterationGroup({ message, sessionId, onFileSelect }: IterationGroupProps) {
  const summary = message.iterationSummary?.summary;
  const isStreaming = message.status === "streaming";
  const runningToolCount = (message.toolCalls ?? []).filter((tc) => tc.status === "running").length;
  // The group renders three exclusive layouts:
  //   1. Live + no summary yet → animated placeholder, always expanded.
  //   2. Summary present       → collapsible row (default collapsed).
  //   3. No summary, not live  → permanently expanded (fallback for replay /
  //                              summary-failed iterations).
  const [open, setOpen] = useState(false);
  const dot = dotClass(worstStatus(message));

  if (summary) {
    return (
      <div className="space-y-2">
        <button
          type="button"
          onClick={() => setOpen((p) => !p)}
          className="flex items-center gap-2 text-sm text-left w-full hover:bg-muted/30 rounded px-1 py-0.5"
        >
          <span className={cn("size-2 rounded-full", dot)} />
          <span className="text-foreground flex-1 truncate">{summary}</span>
          <ChevronRight
            className={cn("size-3 text-muted-foreground transition-transform", open && "rotate-90")}
          />
        </button>
        {open && (
          <div className="ml-4 space-y-2">
            <ExpandedContent message={message} sessionId={sessionId} onFileSelect={onFileSelect} />
          </div>
        )}
      </div>
    );
  }

  if (isStreaming) {
    const label = runningToolCount > 0
      ? `Working… (${runningToolCount} tool${runningToolCount === 1 ? "" : "s"})`
      : "Thinking…";
    return (
      <div className="space-y-2">
        <div className="flex items-center gap-2 text-sm">
          <span className={cn("size-2 rounded-full", dot)} />
          <Shimmer duration={3} spread={3} className="text-sm">{label}</Shimmer>
        </div>
        <div className="ml-4 space-y-2">
          <ExpandedContent message={message} sessionId={sessionId} onFileSelect={onFileSelect} />
        </div>
      </div>
    );
  }

  // Permanently expanded fallback (no summary, not streaming).
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-sm">
        <span className={cn("size-2 rounded-full", dot)} />
        <span className="text-muted-foreground italic">(no summary)</span>
      </div>
      <div className="ml-4 space-y-2">
        <ExpandedContent message={message} sessionId={sessionId} onFileSelect={onFileSelect} />
      </div>
    </div>
  );
}

function ExpandedContent({
  message,
  sessionId: _sessionId,
  onFileSelect,
}: IterationGroupProps) {
  return (
    <>
      {message.reasoning && (
        <Reasoning isStreaming={false} defaultOpen>
          <ReasoningTrigger />
          <ReasoningContent>{message.reasoning}</ReasoningContent>
        </Reasoning>
      )}
      {(message.toolCalls ?? []).map((tc) => (
        <ToolCallBlock key={tc.id} tc={tc} onFileSelect={onFileSelect} />
      ))}
    </>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/iteration-group.test.tsx
```
Expected: all PASS. The "permanently expanded" test requires `<ReasoningContent>` to render its text — verify by reading the existing test scaffolding for Reasoning, and if necessary mock the streamdown plugin or read the raw text from the DOM directly.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates/sdk/agent-chat-react && git add src/components/chat/iteration-group.tsx tests/iteration-group.test.tsx && git commit -m "feat(chat): IterationGroup component for Simple view"
```

---

### Task B7: `TurnSummaryCard` component

**Files:**
- Create: `src/components/chat/turn-summary-card.tsx`
- Test: `tests/turn-summary-card.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `tests/turn-summary-card.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { TurnSummaryCard } from "../src/components/chat/turn-summary-card";
import type { AgentChatTurnSummary } from "../src/types";

function makeSummary(over: Partial<AgentChatTurnSummary> = {}): AgentChatTurnSummary {
  return {
    turnId: "t-1",
    recap: "Reworked the hero around brain/hands.",
    artifacts: [],
    ...over,
  };
}

describe("TurnSummaryCard", () => {
  it("renders nothing when recap is empty and artifacts is empty", () => {
    const { container } = render(
      <TurnSummaryCard summary={makeSummary({ recap: "" })} sessionId="s-1" />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders the recap text", () => {
    render(<TurnSummaryCard summary={makeSummary()} sessionId="s-1" />);
    expect(screen.getByText(/Reworked the hero/)).toBeTruthy();
  });

  it("renders a file artifact as a clickable link that calls onFileSelect", () => {
    const onFileSelect = vi.fn();
    render(
      <TurnSummaryCard
        summary={makeSummary({
          artifacts: [{ kind: "file", label: "landing.html", ref: "landing.html" }],
        })}
        sessionId="s-1"
        onFileSelect={onFileSelect}
      />,
    );
    fireEvent.click(screen.getByText("landing.html"));
    expect(onFileSelect).toHaveBeenCalledWith("landing.html");
  });

  it("renders a url artifact as an external anchor with noopener", () => {
    render(
      <TurnSummaryCard
        summary={makeSummary({
          artifacts: [{ kind: "url", label: "example.com", ref: "https://example.com" }],
        })}
        sessionId="s-1"
      />,
    );
    const anchor = screen.getByText("example.com").closest("a");
    expect(anchor?.getAttribute("href")).toBe("https://example.com");
    expect(anchor?.getAttribute("rel")).toContain("noopener");
    expect(anchor?.getAttribute("target")).toBe("_blank");
  });

  it("renders a command artifact as plain non-clickable text when no resolver is wired", () => {
    render(
      <TurnSummaryCard
        summary={makeSummary({
          artifacts: [{ kind: "command", label: "ls -la", ref: "tc-1" }],
        })}
        sessionId="s-1"
      />,
    );
    expect(screen.getByText("ls -la")).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/turn-summary-card.test.tsx
```
Expected: FAIL — module missing.

- [ ] **Step 3: Create the component**

Create `src/components/chat/turn-summary-card.tsx`:

```tsx
import type { AgentChatTurnArtifactRef, AgentChatTurnSummary } from "../../types";

export interface TurnSummaryCardProps {
  summary: AgentChatTurnSummary;
  sessionId: string | null;
  onFileSelect?: (path: string) => void;
  onCommandSelect?: (toolCallId: string) => void;
}

export function TurnSummaryCard({
  summary,
  sessionId: _sessionId,
  onFileSelect,
  onCommandSelect,
}: TurnSummaryCardProps) {
  const hasRecap = summary.recap.trim().length > 0;
  const hasArtifacts = summary.artifacts.length > 0;
  if (!hasRecap && !hasArtifacts) return null;

  return (
    <div className="mt-3 rounded border border-border bg-muted/20 px-3 py-2 text-sm">
      {hasRecap && (
        <p className="mb-2 text-foreground">{summary.recap}</p>
      )}
      {hasArtifacts && (
        <ul className="space-y-1">
          {summary.artifacts.map((a, i) => (
            <li key={`${a.kind}:${a.ref}:${i}`} className="flex items-baseline gap-2 text-sm">
              <span className="text-muted-foreground">•</span>
              <ArtifactRow
                artifact={a}
                onFileSelect={onFileSelect}
                onCommandSelect={onCommandSelect}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ArtifactRow({
  artifact,
  onFileSelect,
  onCommandSelect,
}: {
  artifact: AgentChatTurnArtifactRef;
  onFileSelect?: (path: string) => void;
  onCommandSelect?: (toolCallId: string) => void;
}) {
  if (artifact.kind === "file" && onFileSelect) {
    return (
      <button
        type="button"
        onClick={() => onFileSelect(artifact.ref)}
        className="text-primary hover:underline truncate cursor-pointer"
      >
        {artifact.label}
      </button>
    );
  }
  if (artifact.kind === "url") {
    return (
      <a
        href={artifact.ref}
        target="_blank"
        rel="noopener noreferrer"
        className="text-primary hover:underline truncate"
      >
        {artifact.label}
      </a>
    );
  }
  if (artifact.kind === "command" && onCommandSelect) {
    return (
      <button
        type="button"
        onClick={() => onCommandSelect(artifact.ref)}
        className="text-primary hover:underline truncate cursor-pointer text-left"
      >
        {artifact.label}
      </button>
    );
  }
  // Fallback: non-clickable text (artifact kind, or no resolver).
  return <span className="text-muted-foreground truncate">{artifact.label}</span>;
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/turn-summary-card.test.tsx
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates/sdk/agent-chat-react && git add src/components/chat/turn-summary-card.tsx tests/turn-summary-card.test.tsx && git commit -m "feat(chat): TurnSummaryCard component for end-of-turn recap"
```

---

### Task B8: Add Simple/Expert toggle to the composer's tools row

**Files:**
- Modify: `src/components/chat/chat-composer.tsx` (props block around line 122-132; renderer around line 655)
- Modify: `src/components/chat/chat-thread.tsx` (props passthrough around lines 86-91 + 977-991)
- Modify: `src/agent-chat.tsx` (wire `viewMode` and `setViewMode` from the runtime hook through to `ChatThread`)
- Test: `tests/chat-composer-view-toggle.test.tsx` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/chat-composer-view-toggle.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { ChatComposer } from "../src/components/chat/chat-composer";

describe("ChatComposer view-mode toggle", () => {
  it("renders Simple and Expert segments and dispatches onViewModeChange", () => {
    const onViewModeChange = vi.fn();
    render(
      <ChatComposer
        onSend={vi.fn()}
        onStop={vi.fn()}
        isRunning={false}
        viewMode="simple"
        onViewModeChange={onViewModeChange}
      />,
    );
    const expertBtn = screen.getByRole("button", { name: /Expert/i });
    fireEvent.click(expertBtn);
    expect(onViewModeChange).toHaveBeenCalledWith("expert");
  });

  it("shows the current mode as pressed", () => {
    render(
      <ChatComposer
        onSend={vi.fn()}
        onStop={vi.fn()}
        isRunning={false}
        viewMode="expert"
        onViewModeChange={vi.fn()}
      />,
    );
    const expertBtn = screen.getByRole("button", { name: /Expert/i });
    expect(expertBtn.getAttribute("aria-pressed")).toBe("true");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/chat-composer-view-toggle.test.tsx
```
Expected: FAIL — props unknown.

- [ ] **Step 3: Add the props and the toggle UI**

In `src/components/chat/chat-composer.tsx`, add to the props interface (around line 122-132):

```ts
  viewMode?: "simple" | "expert";
  onViewModeChange?: (mode: "simple" | "expert") => void;
```

And in the destructuring (around line 286-291):

```ts
  viewMode = "simple",
  onViewModeChange,
```

In the rendered tools row (just before `{canShowBrowser && onToggleBrowser && (...)}` at line 655), add:

```tsx
              {onViewModeChange && (
                <div className="inline-flex rounded-md border border-border overflow-hidden" role="group" aria-label="Chat view mode">
                  <button
                    type="button"
                    onClick={() => onViewModeChange("simple")}
                    aria-pressed={viewMode === "simple"}
                    className={
                      viewMode === "simple"
                        ? "bg-accent text-foreground px-2 py-1 text-xs"
                        : "text-muted-foreground hover:text-foreground px-2 py-1 text-xs"
                    }
                  >
                    Simple
                  </button>
                  <button
                    type="button"
                    onClick={() => onViewModeChange("expert")}
                    aria-pressed={viewMode === "expert"}
                    className={
                      viewMode === "expert"
                        ? "bg-accent text-foreground px-2 py-1 text-xs"
                        : "text-muted-foreground hover:text-foreground px-2 py-1 text-xs"
                    }
                  >
                    Expert
                  </button>
                </div>
              )}
```

In `src/components/chat/chat-thread.tsx`, add the same two props to `ChatThreadProps` (around line 86) and to the destructured args (around line 823); pass them through to `<ChatComposer>` (around line 977):

```tsx
          <ChatComposer
            ...existing props...
            canShowWorkspace={canShowWorkspace}
            viewMode={viewMode}
            onViewModeChange={onViewModeChange}
          />
```

In `src/agent-chat.tsx`, read `viewMode` and `setViewMode` from the runtime hook and pass them into the `<ChatThread>` props.

```bash
cd /work/surogates/sdk/agent-chat-react && grep -n "ChatThread\|useAgentChatRuntime\b" src/agent-chat.tsx | head -10
```
At the `<ChatThread .../>` render site, add `viewMode={runtime.viewMode}` and `onViewModeChange={runtime.setViewMode}`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/chat-composer-view-toggle.test.tsx
```
Expected: PASS.

- [ ] **Step 5: Re-run all SDK tests + typecheck**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test && npm run typecheck
```
Expected: all green.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates/sdk/agent-chat-react && git add src/components/chat/chat-composer.tsx src/components/chat/chat-thread.tsx src/agent-chat.tsx tests/chat-composer-view-toggle.test.tsx && git commit -m "feat(composer): Simple/Expert view mode toggle in tools row"
```

---

### Task B9: Render `IterationGroup` and `TurnSummaryCard` in Simple mode

**Files:**
- Modify: `src/components/chat/chat-thread.tsx` — `AssistantGroup` (lines 680-772), threading `viewMode` down
- Modify: existing `ChatThreadProps` + props passthrough so `AssistantGroup` knows the mode
- Test: `tests/simple-mode-render.test.tsx` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/simple-mode-render.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import { ChatThread } from "../src/components/chat/chat-thread";
import type { ChatMessage } from "../src/types";

function assistantMessage(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "Here's what I did.",
    createdAt: new Date(),
    status: "complete",
    turnId: "t-1",
    iterationIndex: 0,
    iterationSummary: {
      iterationIndex: 0,
      summary: "Reworked the hero copy",
      toolCallIds: ["c1"],
      startedAt: "", endedAt: "",
    },
    toolCalls: [
      { id: "c1", toolName: "patch", args: "{}", status: "complete", result: "{}" },
    ],
    turnSummary: {
      turnId: "t-1",
      recap: "Reworked the hero around brain/hands.",
      artifacts: [{ kind: "file", label: "landing.html", ref: "landing.html" }],
    },
    ...over,
  };
}

const noop = () => Promise.resolve();

describe("Simple mode rendering", () => {
  it("shows the iteration summary line, not raw per-tool entries", () => {
    render(
      <ChatThread
        sessionId="s-1"
        messages={[assistantMessage()]}
        isRunning={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    expect(screen.getByText("Reworked the hero copy")).toBeTruthy();
    // Per-tool 'Patch' label is hidden (collapsed inside the iteration row)
    expect(screen.queryByText(/^Patch$/)).toBeNull();
    // Turn summary card visible
    expect(screen.getByText(/Reworked the hero around brain\/hands/)).toBeTruthy();
    expect(screen.getByText("landing.html")).toBeTruthy();
  });

  it("Expert mode still renders the per-tool blocks", () => {
    render(
      <ChatThread
        sessionId="s-1"
        messages={[assistantMessage()]}
        isRunning={false}
        onSend={noop}
        onStop={noop}
        viewMode="expert"
      />,
    );
    expect(screen.getByText(/Patch/)).toBeTruthy();
    // No TurnSummaryCard in Expert mode
    expect(screen.queryByText(/Reworked the hero around brain\/hands/)).toBeNull();
  });

  it("falls back gracefully when an iteration has no summary (Simple mode)", () => {
    const msg = assistantMessage({ iterationSummary: undefined });
    render(
      <ChatThread
        sessionId="s-1"
        messages={[msg]}
        isRunning={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    // No 'Reworked the hero copy' (the summary is gone) but the per-tool
    // content is rendered inline because IterationGroup falls through to
    // the permanently-expanded layout.
    expect(screen.queryByText("Reworked the hero copy")).toBeNull();
    expect(screen.getByText(/Patch/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/simple-mode-render.test.tsx
```
Expected: FAIL — Simple mode not yet implemented in `AssistantGroup`.

- [ ] **Step 3: Add Simple-mode rendering in `AssistantGroup`**

In `src/components/chat/chat-thread.tsx`:

a. Add `viewMode?: "simple" | "expert"` to `ChatThreadProps` (already added in Task B8 — verify), and pass through to `AssistantGroup`:

```tsx
                return (
                  <AssistantGroup
                    key={group.messages[0].id}
                    ...existing props...
                    viewMode={viewMode}
                    onRetry={groupRetry}
                  />
                );
```

b. Extend `AssistantGroup`'s props and body:

```tsx
function AssistantGroup({
  messages,
  lastGlobalIndex,
  totalMessages,
  isRunning,
  sessionId,
  artifactFallbacks,
  onFileSelect,
  onRetry,
  viewMode = "simple",
}: {
  messages: ChatMessageType[];
  lastGlobalIndex: number;
  totalMessages: number;
  isRunning: boolean;
  sessionId: string | null;
  artifactFallbacks: Record<string, string>;
  onFileSelect?: (path: string) => void;
  onRetry?: () => Promise<void>;
  viewMode?: "simple" | "expert";
}) {
  if (viewMode === "simple") {
    return (
      <SimpleAssistantGroup
        messages={messages}
        sessionId={sessionId}
        onFileSelect={onFileSelect}
      />
    );
  }
  // ...existing Expert body unchanged...
}
```

c. Add `SimpleAssistantGroup` below:

```tsx
function SimpleAssistantGroup({
  messages,
  sessionId,
  onFileSelect,
}: {
  messages: ChatMessageType[];
  sessionId: string | null;
  onFileSelect?: (path: string) => void;
}) {
  const assistantMessages = messages.filter((m) => m.role === "assistant");
  const tail = assistantMessages[assistantMessages.length - 1];
  const finalText = tail && tail.content && tail.status === "complete"
    ? tail.content
    : "";

  return (
    <Message from="assistant">
      <MessageContent>
        <div className="space-y-3">
          {assistantMessages.map((m) => (
            <IterationGroup
              key={m.id}
              message={m}
              sessionId={sessionId}
              onFileSelect={onFileSelect}
            />
          ))}
        </div>
        {finalText && (
          <div className="mt-3">
            <MessageResponse>{finalText}</MessageResponse>
          </div>
        )}
        {tail?.turnSummary && (
          <TurnSummaryCard
            summary={tail.turnSummary}
            sessionId={sessionId}
            onFileSelect={onFileSelect}
          />
        )}
        {tail && tail.status === "complete" && finalText && (
          <TurnFeedback msg={tail} />
        )}
      </MessageContent>
    </Message>
  );
}
```

Add the imports at the top of `chat-thread.tsx`:

```ts
import { IterationGroup } from "./iteration-group";
import { TurnSummaryCard } from "./turn-summary-card";
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test -- tests/simple-mode-render.test.tsx
```
Expected: PASS.

- [ ] **Step 5: Run all SDK tests + typecheck**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test && npm run typecheck
```
Expected: all green.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates/sdk/agent-chat-react && git add src/components/chat/chat-thread.tsx tests/simple-mode-render.test.tsx && git commit -m "feat(chat): render IterationGroup + TurnSummaryCard in Simple mode"
```

---

### Task B10: Update existing tests that assume Expert-mode layout

**Files:**
- Modify: any existing `tests/*.test.tsx` that asserts per-tool labels and instantiates `<ChatThread>`, `<AgentChat>`, or `<AssistantGroup>` without specifying `viewMode`.

- [ ] **Step 1: Identify affected tests**

```bash
cd /work/surogates/sdk/agent-chat-react && grep -lE "ChatThread|AgentChat\b|AssistantGroup" tests/ | head -20
```

Each test that exercises today's per-tool rendering needs to opt into Expert mode now that Simple is the default. Add `viewMode="expert"` to those render calls (or wrap in a router/state where applicable).

- [ ] **Step 2: Re-run the full test suite**

```bash
cd /work/surogates/sdk/agent-chat-react && npm test 2>&1 | tail -50
```
Address any failures by adding `viewMode="expert"` to the broken tests' render calls. Don't change assertions — the goal is to preserve the existing test coverage of the Expert layout, not to convert tests.

- [ ] **Step 3: Commit**

```bash
cd /work/surogates/sdk/agent-chat-react && git add tests/ && git commit -m "test(chat): opt existing per-tool tests into Expert view mode"
```

---

## Phase C — Verification

### Task C1: End-to-end manual verification

**Files:** None — manual run on a real session.

- [ ] **Step 1: Start the backend**

```bash
cd /work/surogate-ops && surogate-ops server ~/.surogate/config.yaml
```

Verify `~/.surogate/config.yaml` contains a `summary_model` setting; without it the harness's `summary_auxiliary` is `None` and no summaries are emitted.

- [ ] **Step 2: Start the frontend dev server**

```bash
cd /work/surogate-ops/frontend && npm run dev
```

- [ ] **Step 3: Open a chat session in the browser**

Open `http://localhost:5173`, start a new agent chat, send a multi-step prompt (e.g. "create a hello.html landing page with a brain/hands metaphor in the hero, then list the files you created").

- [ ] **Step 4: Verify Simple mode**

In the composer's tools row, confirm the `Simple / Expert` toggle is visible and `Simple` is highlighted.

While the turn streams, expect:
- A live iteration row reading `Thinking…` then `Working… (N tools)`.
- When the iteration ends, the row collapses to a one-line summary.

When the turn ends:
- A `TurnSummaryCard` appears below the final answer with at least one artifact link (the file that was created). Clicking the file link should open the workspace file viewer.

- [ ] **Step 5: Verify Expert mode**

Toggle to `Expert`. The same conversation should now render today's per-tool timeline (Reasoning rows, Patch rows, etc.) with no `TurnSummaryCard`.

- [ ] **Step 6: Verify replay**

Reload the page. The session should re-load with summaries intact (Simple mode) — they came from persisted events, not regenerated on render.

- [ ] **Step 7: Verify kill switch**

Stop the backend. Set `SUROGATES_WORKER_EMIT_TURN_SUMMARIES=false` in the environment and restart:

```bash
SUROGATES_WORKER_EMIT_TURN_SUMMARIES=false surogate-ops server ~/.surogate/config.yaml
```

Start a fresh session. Simple mode should fall back to the live-state rendering (iterations stay expanded, no `TurnSummaryCard` after the answer). No errors in the browser console.

- [ ] **Step 8: Note any deviations**

If any of the steps above produce a different result than described, file a follow-up task (do not modify the spec / plan after the fact unless the spec was wrong).

---

## Self-review checklist (for the plan author)

- All spec sections mapped to a task: goals → A4–A8, B5–B9; non-goals respected (no changes to per-tool renderers, no rework of browser/web-search grouping); harness side → A1–A9; SDK side → B1–B10; edge cases → covered by tests in B6–B10 (live state, fallback, Expert mode, replay).
- No placeholders: every code step contains the literal code to be added; commands include the exact `npm` / `pytest` invocations with the expected output.
- Type consistency: `turnId`, `iterationIndex`, `iterationSummary`, `turnSummary` field names used identically across reducer (B3, B4), types (B2), `IterationGroup` (B6), `TurnSummaryCard` (B7), and the rendering integration (B9). Harness emits `turn_id` / `iteration_index` (snake_case) — the SDK reducer's `readTurnMeta` helper bridges to camelCase.
