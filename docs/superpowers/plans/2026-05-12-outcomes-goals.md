# Outcome Goals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `user.define_outcome` event plus `/goal` convenience command that start an outcome-oriented session loop: the agent works, a separate evaluator grades the result against outcome criteria, and the harness keeps iterating until satisfied, paused, interrupted, failed, or the iteration budget is reached.

**Architecture:** Model the core after Claude Managed Agents outcomes: callers define work by emitting `user.define_outcome`, the harness grades each iteration in a separate evaluator context, and structured `span.outcome_evaluation_*` events drive the loop. Keep Hermes' slash-command UX as a thin wrapper over the same internal outcome-definition path. Outcome state lives in `sessions.config["outcome"]`; continuation is represented as synthetic `user.message` events in Surogates' durable event log, then the normal Redis queue wakes the same session.

**Tech Stack:** Python 3.12, FastAPI session/event model, SQLAlchemy async session store, OpenAI-compatible chat completions, pytest/pytest-asyncio.

---

## Implementation Todo

- [x] **Task 1: Add Outcome Core Module** — completed
- [x] **Task 2: Add Evaluator Decision Logic** — completed
- [x] **Task 3: Add Outcome Event Types** — completed
- [x] **Task 4: Add SessionStore Outcome Persistence** — completed
- [x] **Task 5: Reserve `/goal` From Dynamic Skills** — completed
- [x] **Task 6: Add Outcome Settings** — completed
- [ ] **Task 7: Add `user.define_outcome` API Event** — in_progress
- [ ] **Task 8: Add `/goal` Command Handler** — pending
- [ ] **Task 9: Add Post-Turn Outcome Evaluation** — pending
- [ ] **Task 10: Prevent Completed Status During Active Continuation** — pending
- [ ] **Task 11: Document Outcome API and `/goal` Behavior** — pending
- [ ] **Task 12: Run Focused Verification** — pending

---

## File Structure

- Create `surogates/harness/outcomes.py`: pure outcome state, command parsing, continuation prompt construction, evaluator prompt construction, evaluator response parsing, and decision logic. No database or Redis dependency.
- Modify `surogates/session/events.py`: add `user.define_outcome` and Claude-shaped `span.outcome_evaluation_*` event types.
- Modify `surogates/session/store.py`: add atomic helpers for updating `sessions.config["outcome"]`, appending synthetic user messages, and retrieving outcome evaluation status from events/config.
- Modify `surogates/harness/slash_skill.py`: reserve `/goal` so it is never treated as a dynamic skill.
- Modify `surogates/harness/loop.py`: process pending `user.define_outcome` events, handle `/goal` commands before skill expansion, evaluate active outcomes after final responses, and enqueue continuation turns.
- Modify `surogates/config.py`: add `OutcomeSettings` under `settings.outcomes` with Claude-compatible default `max_iterations=3` and max `20`.
- Modify `config.yaml.example`: document default outcome settings.
- Modify `surogates/api/routes/events.py`: add a narrow `POST /sessions/{id}/events` endpoint for `user.define_outcome` only.
- Modify `docs/commands/index.md` and `docs/appendices/api-reference.md`: document `/goal`, `user.define_outcome`, and outcome evaluation behavior.
- Add `tests/test_outcomes.py`: unit tests for state, parsing, evaluator parsing, and decision logic.
- Add `tests/test_outcome_store.py`: session config persistence and synthetic continuation event tests.
- Add `tests/test_outcome_harness.py`: harness command and post-turn integration tests with fake store/LLM/Redis.
- Modify `tests/test_slash_skill.py` and `tests/test_loop_command.py`: assert `/goal` is reserved.

## Semantics

Outcome state shape in `sessions.config["outcome"]`:

```python
{
    "id": "outc_<uuidhex>",
    "description": "Fix all failing tests",
    "rubric": "The final answer says pytest passed...",
    "status": "active",  # active | paused | satisfied | failed | cleared | max_iterations_reached
    "iteration": 0,
    "max_iterations": 3,
    "created_at": "2026-05-12T10:00:00Z",
    "updated_at": "2026-05-12T10:00:00Z",
    "last_result": None,  # satisfied | needs_revision | failed | interrupted | max_iterations_reached
    "last_explanation": None,
    "last_feedback": None,
    "paused_reason": None,
}
```

Default behavior:

- `user.define_outcome` is the canonical API shape. It creates a normal session outcome and starts work without requiring a separate user message.
- `/goal <description>` is a chat convenience wrapper over the same outcome-definition helper.
- Rubrics are required for `user.define_outcome`. `/goal <description>` may use a generated/default rubric for ergonomics, but docs should recommend explicit `Rubric:` criteria.
- `/goal <description>\n\nRubric:\n...` or `/goal <description>\n\nCriteria:\n...` captures the text after the heading as the rubric.
- `/goal status` shows active/paused/satisfied state, iteration count, description, and last evaluator explanation.
- `/goal pause` pauses the outcome and stops future synthetic continuations.
- `/goal resume` reactivates the paused outcome without resetting iteration count.
- `/goal clear` marks the state cleared.
- Only one active outcome is supported per session. Setting a new `/goal <description>` replaces the previous active/paused outcome.
- Default `max_iterations` is `3`; config allows `1..20`, matching Claude's default and cap.
- Evaluator failures fail open as `needs_revision` until `max_iterations` is reached.
- Evaluator parse failures pause after `outcomes.max_parse_failures` consecutive failures.
- A user message sent while an outcome is active is treated as normal steering input. The evaluator still runs after that turn.
- Synthetic continuation prompts are normal `user.message` events with `data["synthetic"] == "outcome_continuation"` and `data["outcome_id"] == state.id`.
- Evaluation events use Claude-compatible names: `span.outcome_evaluation_start`, `span.outcome_evaluation_ongoing`, and `span.outcome_evaluation_end`.

## Task 1: Add Outcome Core Module

**Files:**
- Create: `surogates/harness/outcomes.py`
- Test: `tests/test_outcomes.py`

- [ ] **Step 1: Write failing tests for command parsing and default rubric**

Create `tests/test_outcomes.py`:

```python
from __future__ import annotations

import json

from surogates.harness.outcomes import (
    DEFAULT_OUTCOME_RUBRIC,
    OutcomeCommand,
    OutcomeState,
    build_continuation_prompt,
    parse_goal_command,
    parse_outcome_evaluation,
    start_outcome,
)


def test_parse_goal_status_defaults_for_empty_args() -> None:
    assert parse_goal_command("") == OutcomeCommand(action="status", text="", rubric="")
    assert parse_goal_command("status") == OutcomeCommand(action="status", text="", rubric="")


def test_parse_goal_controls() -> None:
    assert parse_goal_command("pause").action == "pause"
    assert parse_goal_command("resume").action == "resume"
    assert parse_goal_command("clear").action == "clear"


def test_parse_goal_description_with_rubric_heading() -> None:
    command = parse_goal_command(
        "Build a DCF model\n\nRubric:\n- Creates an xlsx file\n- Includes sensitivity analysis"
    )
    assert command.action == "set"
    assert command.text == "Build a DCF model"
    assert "Creates an xlsx file" in command.rubric


def test_start_outcome_uses_default_rubric_when_missing() -> None:
    state = start_outcome("Fix tests", rubric="", max_iterations=3, now_iso="2026-05-12T10:00:00Z")
    assert state.description == "Fix tests"
    assert state.rubric == DEFAULT_OUTCOME_RUBRIC
    assert state.status == "active"
    assert state.max_iterations == 3
    assert state.iteration == 0
    assert state.id.startswith("outc_")


def test_start_outcome_clamps_max_iterations_to_twenty() -> None:
    state = start_outcome("Fix tests", rubric="", max_iterations=99, now_iso="2026-05-12T10:00:00Z")
    assert state.max_iterations == 20


def test_outcome_state_round_trips_from_config() -> None:
    state = start_outcome("Fix tests", rubric="Must pass pytest", max_iterations=3, now_iso="2026-05-12T10:00:00Z")
    loaded = OutcomeState.from_config(state.to_config())
    assert loaded == state


def test_build_continuation_prompt_includes_feedback_and_rubric() -> None:
    state = start_outcome("Fix tests", rubric="Must pass pytest", max_iterations=3, now_iso="2026-05-12T10:00:00Z")
    state.last_feedback = "pytest still fails in tests/test_api.py"
    prompt = build_continuation_prompt(state)
    assert "[Continuing toward your defined outcome]" in prompt
    assert "Fix tests" in prompt
    assert "Must pass pytest" in prompt
    assert "pytest still fails" in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_outcomes.py -q
```

Expected: import failure for `surogates.harness.outcomes`.

- [ ] **Step 3: Implement minimal outcome dataclasses and parsing**

Create `surogates/harness/outcomes.py`:

```python
"""Outcome-oriented goal state and evaluation helpers."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4

DEFAULT_MAX_ITERATIONS = 3
MAX_MAX_ITERATIONS = 20
DEFAULT_OUTCOME_RUBRIC = (
    "The outcome is satisfied only when the assistant's latest response "
    "explicitly confirms the requested work is complete, clearly presents "
    "the final deliverable, or clearly explains that the work is blocked or "
    "unachievable and what remains outside the agent's control."
)

_RUBRIC_RE = re.compile(r"\n\s*(?:rubric|criteria)\s*:\s*\n", re.IGNORECASE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class OutcomeCommand:
    action: str
    text: str = ""
    rubric: str = ""


@dataclass
class OutcomeState:
    id: str
    description: str
    rubric: str
    status: str = "active"
    iteration: int = 0
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    created_at: str = ""
    updated_at: str = ""
    last_result: str | None = None
    last_explanation: str | None = None
    last_feedback: str | None = None
    paused_reason: str | None = None
    consecutive_parse_failures: int = 0

    def to_config(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_config(cls, data: Any) -> "OutcomeState | None":
        if not isinstance(data, dict):
            return None
        description = str(data.get("description") or "").strip()
        if not description:
            return None
        return cls(
            id=str(data.get("id") or f"outc_{uuid4().hex}"),
            description=description,
            rubric=str(data.get("rubric") or DEFAULT_OUTCOME_RUBRIC),
            status=str(data.get("status") or "active"),
            iteration=int(data.get("iteration") or 0),
            max_iterations=_clamp_max_iterations(data.get("max_iterations")),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            last_result=data.get("last_result"),
            last_explanation=data.get("last_explanation"),
            last_feedback=data.get("last_feedback"),
            paused_reason=data.get("paused_reason"),
            consecutive_parse_failures=int(data.get("consecutive_parse_failures") or 0),
        )


@dataclass(frozen=True)
class OutcomeEvaluation:
    result: str
    explanation: str
    feedback: str
    parse_failed: bool = False


def _clamp_max_iterations(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_ITERATIONS
    return min(MAX_MAX_ITERATIONS, max(1, parsed))


def parse_goal_command(args: str) -> OutcomeCommand:
    text = (args or "").strip()
    lower = text.lower()
    if not text or lower == "status":
        return OutcomeCommand(action="status")
    if lower in {"pause", "resume", "clear"}:
        return OutcomeCommand(action=lower)
    match = _RUBRIC_RE.search(text)
    if match is None:
        return OutcomeCommand(action="set", text=text, rubric="")
    return OutcomeCommand(
        action="set",
        text=text[: match.start()].strip(),
        rubric=text[match.end() :].strip(),
    )


def start_outcome(
    description: str,
    *,
    rubric: str,
    max_iterations: int,
    now_iso: str,
) -> OutcomeState:
    cleaned = (description or "").strip()
    if not cleaned:
        raise ValueError("goal text is empty")
    return OutcomeState(
        id=f"outc_{uuid4().hex}",
        description=cleaned,
        rubric=(rubric or "").strip() or DEFAULT_OUTCOME_RUBRIC,
        max_iterations=_clamp_max_iterations(max_iterations),
        created_at=now_iso,
        updated_at=now_iso,
    )


def build_continuation_prompt(state: OutcomeState) -> str:
    feedback = state.last_feedback or state.last_explanation or "Continue with the next concrete revision."
    return (
        "[Continuing toward your defined outcome]\n"
        f"Outcome: {state.description}\n\n"
        f"Rubric:\n{state.rubric}\n\n"
        f"Evaluator feedback:\n{feedback}\n\n"
        "Revise the work to satisfy the outcome. Take the next concrete step. "
        "If the outcome is now satisfied, state that explicitly and stop. "
        "If you are blocked and need user input, say so clearly and stop."
    )


def parse_outcome_evaluation(raw: str) -> OutcomeEvaluation:
    if not (raw or "").strip():
        return OutcomeEvaluation(
            result="needs_revision",
            explanation="evaluator returned empty response",
            feedback="Continue working toward the outcome.",
            parse_failed=True,
        )
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        newline = text.find("\n")
        if newline >= 0:
            text = text[newline + 1 :]
    try:
        data = json.loads(text)
    except Exception:
        match = _JSON_OBJECT_RE.search(text)
        data = None
        if match is not None:
            try:
                data = json.loads(match.group(0))
            except Exception:
                data = None
    if not isinstance(data, dict):
        return OutcomeEvaluation(
            result="needs_revision",
            explanation=f"evaluator response was not JSON: {raw[:200]!r}",
            feedback="Continue working toward the outcome.",
            parse_failed=True,
        )
    result = str(data.get("result") or "needs_revision").strip()
    if result not in {"satisfied", "needs_revision", "failed"}:
        result = "needs_revision"
    explanation = str(data.get("explanation") or "no explanation provided").strip()
    feedback = str(data.get("feedback") or explanation).strip()
    return OutcomeEvaluation(result=result, explanation=explanation, feedback=feedback)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_outcomes.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/outcomes.py tests/test_outcomes.py
git commit -m "feat(outcomes): add outcome state helpers"
```

## Task 2: Add Evaluator Decision Logic

**Files:**
- Modify: `surogates/harness/outcomes.py`
- Test: `tests/test_outcomes.py`

- [ ] **Step 1: Add failing tests for evaluation parsing and decisions**

Append to `tests/test_outcomes.py`:

```python
from surogates.harness.outcomes import (
    apply_evaluation,
    build_evaluator_messages,
)


def test_parse_outcome_evaluation_accepts_json() -> None:
    evaluation = parse_outcome_evaluation(
        json.dumps({
            "result": "needs_revision",
            "explanation": "One criterion missing",
            "feedback": "Add the sensitivity table",
        })
    )
    assert evaluation.result == "needs_revision"
    assert evaluation.explanation == "One criterion missing"
    assert evaluation.feedback == "Add the sensitivity table"
    assert evaluation.parse_failed is False


def test_parse_outcome_evaluation_extracts_fenced_json() -> None:
    evaluation = parse_outcome_evaluation(
        '```json\n{"result":"satisfied","explanation":"All criteria met","feedback":""}\n```'
    )
    assert evaluation.result == "satisfied"
    assert evaluation.parse_failed is False


def test_parse_outcome_evaluation_marks_bad_output_as_parse_failure() -> None:
    evaluation = parse_outcome_evaluation("I think it is done")
    assert evaluation.result == "needs_revision"
    assert evaluation.parse_failed is True


def test_build_evaluator_messages_contains_outcome_rubric_and_response() -> None:
    state = start_outcome("Fix tests", rubric="pytest passes", max_iterations=3, now_iso="2026-05-12T10:00:00Z")
    messages = build_evaluator_messages(state, "I fixed one file")
    joined = "\n".join(m["content"] for m in messages)
    assert "Fix tests" in joined
    assert "pytest passes" in joined
    assert "I fixed one file" in joined
    assert '"result"' in joined


def test_apply_evaluation_satisfied_marks_state_satisfied() -> None:
    state = start_outcome("Fix tests", rubric="", max_iterations=3, now_iso="2026-05-12T10:00:00Z")
    decision = apply_evaluation(
        state,
        parse_outcome_evaluation('{"result":"satisfied","explanation":"All good","feedback":""}'),
        now_iso="2026-05-12T10:01:00Z",
        max_parse_failures=3,
    )
    assert state.status == "satisfied"
    assert decision.should_continue is False
    assert decision.result == "satisfied"


def test_apply_evaluation_needs_revision_continues_before_budget() -> None:
    state = start_outcome("Fix tests", rubric="", max_iterations=3, now_iso="2026-05-12T10:00:00Z")
    decision = apply_evaluation(
        state,
        parse_outcome_evaluation('{"result":"needs_revision","explanation":"Missing test","feedback":"Run pytest"}'),
        now_iso="2026-05-12T10:01:00Z",
        max_parse_failures=3,
    )
    assert state.status == "active"
    assert state.iteration == 1
    assert state.last_feedback == "Run pytest"
    assert decision.should_continue is True


def test_apply_evaluation_pauses_after_parse_failures() -> None:
    state = start_outcome("Fix tests", rubric="", max_iterations=3, now_iso="2026-05-12T10:00:00Z")
    state.consecutive_parse_failures = 2
    decision = apply_evaluation(
        state,
        parse_outcome_evaluation("not json"),
        now_iso="2026-05-12T10:01:00Z",
        max_parse_failures=3,
    )
    assert state.status == "paused"
    assert "parse" in (state.paused_reason or "")
    assert decision.should_continue is False


def test_apply_evaluation_stops_at_iteration_budget() -> None:
    state = start_outcome("Fix tests", rubric="", max_iterations=1, now_iso="2026-05-12T10:00:00Z")
    decision = apply_evaluation(
        state,
        parse_outcome_evaluation('{"result":"needs_revision","explanation":"Missing","feedback":"Keep going"}'),
        now_iso="2026-05-12T10:01:00Z",
        max_parse_failures=3,
    )
    assert state.status == "max_iterations_reached"
    assert decision.result == "max_iterations_reached"
    assert decision.should_continue is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_outcomes.py -q
```

Expected: import failure for `apply_evaluation` and `build_evaluator_messages`.

- [ ] **Step 3: Implement evaluator helpers**

Add to `surogates/harness/outcomes.py`:

```python
EVALUATOR_SYSTEM_PROMPT = (
    "You are a strict outcome evaluator for an agent harness. Evaluate the "
    "assistant's latest response against the user's outcome and rubric. Use "
    "a separate, critical perspective. Return only JSON with keys: result, "
    "explanation, feedback. result must be one of satisfied, needs_revision, "
    "or failed. Use failed only when the outcome and rubric contradict each "
    "other or cannot be evaluated. Treat a clearly blocked/unachievable "
    "outcome as satisfied if the response explains the block and next user "
    "action clearly."
)


@dataclass(frozen=True)
class OutcomeDecision:
    result: str
    should_continue: bool
    message: str
    continuation_prompt: str | None = None


def build_evaluator_messages(
    state: OutcomeState,
    latest_response: str,
) -> list[dict[str, str]]:
    payload = (
        f"Outcome:\n{state.description}\n\n"
        f"Rubric:\n{state.rubric}\n\n"
        f"Assistant latest response:\n{(latest_response or '')[:4000]}\n\n"
        "Return JSON exactly like:\n"
        '{"result":"satisfied|needs_revision|failed","explanation":"one sentence","feedback":"revision guidance"}'
    )
    return [
        {"role": "system", "content": EVALUATOR_SYSTEM_PROMPT},
        {"role": "user", "content": payload},
    ]


def apply_evaluation(
    state: OutcomeState,
    evaluation: OutcomeEvaluation,
    *,
    now_iso: str,
    max_parse_failures: int,
) -> OutcomeDecision:
    state.iteration += 1
    state.updated_at = now_iso
    state.last_result = evaluation.result
    state.last_explanation = evaluation.explanation
    state.last_feedback = evaluation.feedback
    if evaluation.parse_failed:
        state.consecutive_parse_failures += 1
    else:
        state.consecutive_parse_failures = 0

    if evaluation.result == "satisfied":
        state.status = "satisfied"
        return OutcomeDecision(
            result="satisfied",
            should_continue=False,
            message=f"Outcome satisfied: {evaluation.explanation}",
        )

    if evaluation.result == "failed":
        state.status = "failed"
        return OutcomeDecision(
            result="failed",
            should_continue=False,
            message=f"Outcome evaluation failed: {evaluation.explanation}",
        )

    if state.consecutive_parse_failures >= max(1, int(max_parse_failures or 1)):
        state.status = "paused"
        state.paused_reason = "evaluator returned unparseable output repeatedly"
        return OutcomeDecision(
            result="paused",
            should_continue=False,
            message=(
                "Outcome paused: evaluator returned unparseable output repeatedly. "
                "Use /goal resume after adjusting the evaluator model or rubric."
            ),
        )

    if state.iteration >= state.max_iterations:
        state.status = "max_iterations_reached"
        return OutcomeDecision(
            result="max_iterations_reached",
            should_continue=False,
            message=(
                f"Outcome paused: {state.iteration}/{state.max_iterations} "
                "iterations used. Use /goal resume to continue, or /goal clear to stop."
            ),
        )

    state.status = "active"
    prompt = build_continuation_prompt(state)
    return OutcomeDecision(
        result="needs_revision",
        should_continue=True,
        message=(
            f"Continuing outcome ({state.iteration}/{state.max_iterations}): "
            f"{evaluation.explanation}"
        ),
        continuation_prompt=prompt,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_outcomes.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/outcomes.py tests/test_outcomes.py
git commit -m "feat(outcomes): add evaluator decisions"
```

## Task 3: Add Outcome Event Types

**Files:**
- Modify: `surogates/session/events.py`
- Test: `tests/test_outcomes.py`

- [ ] **Step 1: Write failing event enum test**

Append to `tests/test_outcomes.py`:

```python
from surogates.session.events import EventType


def test_outcome_event_type_values_are_stable() -> None:
    assert EventType.USER_DEFINE_OUTCOME.value == "user.define_outcome"
    assert EventType.OUTCOME_DEFINED.value == "outcome.defined"
    assert EventType.OUTCOME_EVALUATION_START.value == "span.outcome_evaluation_start"
    assert EventType.OUTCOME_EVALUATION_ONGOING.value == "span.outcome_evaluation_ongoing"
    assert EventType.OUTCOME_EVALUATION_END.value == "span.outcome_evaluation_end"
    assert EventType.OUTCOME_CONTINUATION.value == "outcome.continuation"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_outcomes.py::test_outcome_event_type_values_are_stable -q
```

Expected: `AttributeError: USER_DEFINE_OUTCOME`.

- [ ] **Step 3: Add event types**

Modify `surogates/session/events.py` after `SESSION_RESET`:

```python
    # Outcome-oriented goal loop
    USER_DEFINE_OUTCOME = "user.define_outcome"
    OUTCOME_DEFINED = "outcome.defined"
    OUTCOME_PAUSED = "outcome.paused"
    OUTCOME_CLEARED = "outcome.cleared"
    OUTCOME_EVALUATION_START = "span.outcome_evaluation_start"
    OUTCOME_EVALUATION_ONGOING = "span.outcome_evaluation_ongoing"
    OUTCOME_EVALUATION_END = "span.outcome_evaluation_end"
    OUTCOME_CONTINUATION = "outcome.continuation"
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_outcomes.py::test_outcome_event_type_values_are_stable -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add surogates/session/events.py tests/test_outcomes.py
git commit -m "feat(outcomes): add outcome event types"
```

## Task 4: Add SessionStore Outcome Persistence

**Files:**
- Modify: `surogates/session/store.py`
- Test: `tests/integration/test_session_store.py`

- [ ] **Step 1: Write failing integration tests**

Append to `tests/integration/test_session_store.py`:

```python
from surogates.session.events import EventType


async def test_update_session_config_key_persists_nested_value(session_store, sample_session):
    outcome = {
        "id": "outc_test",
        "description": "Fix tests",
        "status": "active",
    }

    await session_store.update_session_config_key(sample_session.id, "outcome", outcome)

    updated = await session_store.get_session(sample_session.id)
    assert updated.config["outcome"] == outcome


async def test_clear_session_config_key_removes_value(session_store, sample_session):
    await session_store.update_session_config_key(
        sample_session.id,
        "outcome",
        {"id": "outc_test", "description": "Fix tests"},
    )

    await session_store.clear_session_config_key(sample_session.id, "outcome")

    updated = await session_store.get_session(sample_session.id)
    assert "outcome" not in updated.config


async def test_emit_synthetic_outcome_user_message_marks_event(session_store, sample_session):
    event_id = await session_store.emit_synthetic_user_message(
        sample_session.id,
        content="[Continuing toward your defined outcome]\nOutcome: Fix tests",
        synthetic="outcome_continuation",
        metadata={"outcome_id": "outc_test"},
    )

    events = await session_store.get_events(sample_session.id)
    event = next(e for e in events if e.id == event_id)
    assert event.type == EventType.USER_MESSAGE.value
    assert event.data["content"].startswith("[Continuing")
    assert event.data["synthetic"] == "outcome_continuation"
    assert event.data["outcome_id"] == "outc_test"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/integration/test_session_store.py::test_update_session_config_key_persists_nested_value tests/integration/test_session_store.py::test_clear_session_config_key_removes_value tests/integration/test_session_store.py::test_emit_synthetic_outcome_user_message_marks_event -q
```

Expected: `AttributeError` for missing store methods.

- [ ] **Step 3: Implement store helpers**

Add to `surogates/session/store.py` in the Session CRUD section:

```python
    async def update_session_config_key(
        self,
        session_id: UUID,
        key: str,
        value: Any,
    ) -> None:
        """Set one top-level key in sessions.config."""
        async with self._sf() as db:
            result = await db.execute(
                update(SessionRow)
                .where(SessionRow.id == session_id)
                .values(
                    config=func.jsonb_set(
                        func.coalesce(SessionRow.config, text("'{}'::jsonb")),
                        [key],
                        func.to_jsonb(value),
                        True,
                    ),
                    updated_at=func.now(),
                )
            )
            if result.rowcount == 0:
                raise SessionNotFoundError(f"session {session_id} not found")
            await db.commit()

    async def clear_session_config_key(self, session_id: UUID, key: str) -> None:
        """Remove one top-level key from sessions.config."""
        async with self._sf() as db:
            result = await db.execute(
                update(SessionRow)
                .where(SessionRow.id == session_id)
                .values(
                    config=func.coalesce(SessionRow.config, text("'{}'::jsonb")).op("-")(key),
                    updated_at=func.now(),
                )
            )
            if result.rowcount == 0:
                raise SessionNotFoundError(f"session {session_id} not found")
            await db.commit()

    async def emit_synthetic_user_message(
        self,
        session_id: UUID,
        *,
        content: str,
        synthetic: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        data = {"content": content, "synthetic": synthetic}
        if metadata:
            data.update(metadata)
        return await self.emit_event(session_id, EventType.USER_MESSAGE, data)
```

If PostgreSQL rejects `func.jsonb_set(..., [key], ...)`, replace `[key]` with `text("ARRAY[:config_key]::text[]")` and pass a raw SQL update. Keep the tests unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/integration/test_session_store.py::test_update_session_config_key_persists_nested_value tests/integration/test_session_store.py::test_clear_session_config_key_removes_value tests/integration/test_session_store.py::test_emit_synthetic_outcome_user_message_marks_event -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add surogates/session/store.py tests/integration/test_session_store.py
git commit -m "feat(outcomes): persist session outcome state"
```

## Task 5: Reserve `/goal` From Dynamic Skills

**Files:**
- Modify: `surogates/harness/slash_skill.py`
- Modify: `tests/test_slash_skill.py`
- Modify: `tests/test_loop_command.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_slash_skill.py` in `TestParseSlashCommand`:

```python
    def test_returns_none_for_goal(self) -> None:
        assert parse_slash_command("/goal fix tests") is None
```

Add to `tests/test_loop_command.py`:

```python
def test_goal_is_not_treated_as_skill() -> None:
    assert parse_slash_command("/goal fix tests") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_slash_skill.py::TestParseSlashCommand::test_returns_none_for_goal tests/test_loop_command.py::test_goal_is_not_treated_as_skill -q
```

Expected: both assertions fail because `/goal` parses as a skill command.

- [ ] **Step 3: Reserve command**

Modify `_BUILTIN_SLASH_COMMANDS` in `surogates/harness/slash_skill.py`:

```python
_BUILTIN_SLASH_COMMANDS: Final[frozenset[str]] = frozenset({
    "clear",
    "compress",
    "goal",
    "loop",
})
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_slash_skill.py::TestParseSlashCommand::test_returns_none_for_goal tests/test_loop_command.py::test_goal_is_not_treated_as_skill -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/slash_skill.py tests/test_slash_skill.py tests/test_loop_command.py
git commit -m "feat(outcomes): reserve goal slash command"
```

## Task 6: Add Outcome Settings

**Files:**
- Modify: `surogates/config.py`
- Modify: `config.yaml.example`
- Test: `tests/test_outcomes.py`

- [ ] **Step 1: Write failing settings test**

Append to `tests/test_outcomes.py`:

```python
from surogates.config import Settings


def test_outcome_settings_defaults() -> None:
    settings = Settings()
    assert settings.outcomes.max_iterations == 3
    assert settings.outcomes.max_parse_failures == 3
    assert settings.outcomes.evaluator_model == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_outcomes.py::test_outcome_settings_defaults -q
```

Expected: `AttributeError: 'Settings' object has no attribute 'outcomes'`.

- [ ] **Step 3: Add settings model**

Add near other settings classes in `surogates/config.py`:

```python
class OutcomeSettings(BaseSettings):
    """Outcome-oriented /goal loop configuration."""

    model_config = {"env_prefix": "SUROGATES_OUTCOMES_"}

    max_iterations: int = 3
    max_parse_failures: int = 3
    evaluator_model: str = ""
    evaluator_base_url: str = ""
    evaluator_api_key: str = ""
```

Add to `Settings`:

```python
    outcomes: OutcomeSettings = Field(default_factory=OutcomeSettings)
```

Update `config.yaml.example` after `llm:` or near worker settings:

```yaml
# Outcome-oriented /goal loop
outcomes:
  max_iterations: 3        # Default revision loops for outcomes; hard-clamped to 20
  max_parse_failures: 3    # Pause if the evaluator repeatedly returns non-JSON
  # evaluator_model: ""    # Optional cheap/strict evaluator model; defaults to active session model
  # evaluator_base_url: "" # Optional evaluator endpoint
  # evaluator_api_key: ""  # Optional evaluator API key
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_outcomes.py::test_outcome_settings_defaults -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add surogates/config.py config.yaml.example tests/test_outcomes.py
git commit -m "feat(outcomes): add outcome settings"
```

## Task 7: Add `user.define_outcome` API Event

**Files:**
- Modify: `surogates/api/routes/events.py`
- Modify: `surogates/harness/outcomes.py`
- Test: `tests/integration/test_api.py`

- [ ] **Step 1: Write failing API tests**

Append to `tests/integration/test_api.py`:

```python
async def test_define_outcome_event_requires_rubric(client, sample_session):
    response = await client.post(
        f"/v1/sessions/{sample_session.id}/events",
        json={
            "events": [
                {
                    "type": "user.define_outcome",
                    "description": "Fix all tests",
                    "max_iterations": 3,
                }
            ]
        },
    )

    assert response.status_code == 422
    assert "rubric" in response.text


async def test_define_outcome_event_persists_state_and_enqueues(client, app, sample_session):
    response = await client.post(
        f"/v1/sessions/{sample_session.id}/events",
        json={
            "events": [
                {
                    "type": "user.define_outcome",
                    "description": "Fix all tests",
                    "rubric": {"type": "text", "content": "- pytest passes"},
                    "max_iterations": 5,
                }
            ]
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["events"][0]["type"] == "user.define_outcome"
    assert body["events"][0]["outcome_id"].startswith("outc_")

    session = await app.state.session_store.get_session(sample_session.id)
    assert session.config["outcome"]["description"] == "Fix all tests"
    assert session.config["outcome"]["rubric"] == "- pytest passes"
    assert session.config["outcome"]["max_iterations"] == 5
```

If this project's integration fixtures do not expose `sample_session`, adapt the test to the existing session-creation fixture pattern in `tests/integration/test_api.py`, but keep the assertions unchanged.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/integration/test_api.py::test_define_outcome_event_requires_rubric tests/integration/test_api.py::test_define_outcome_event_persists_state_and_enqueues -q
```

Expected: `404 Not Found` or `405 Method Not Allowed` for `POST /events`.

- [ ] **Step 3: Add API schemas and route**

Add to `surogates/api/routes/events.py` near response schemas:

```python
class DefineOutcomeRubric(BaseModel):
    type: str
    content: str | None = None
    file_id: str | None = None


class SessionEventIn(BaseModel):
    type: str
    description: str | None = None
    rubric: DefineOutcomeRubric | None = None
    max_iterations: int | None = None


class SendEventsRequest(BaseModel):
    events: list[SessionEventIn]


class SentSessionEvent(BaseModel):
    type: str
    event_id: int
    outcome_id: str | None = None
    processed_at: str


class SendEventsResponse(BaseModel):
    events: list[SentSessionEvent]
```

Add a route after the polling endpoint or before SSE:

```python
@router.post("/api/sessions/{session_id}/events", response_model=SendEventsResponse, status_code=status.HTTP_202_ACCEPTED)
@router.post("/sessions/{session_id}/events", response_model=SendEventsResponse, status_code=status.HTTP_202_ACCEPTED)
async def send_session_events(
    session_id: UUID,
    body: SendEventsRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SendEventsResponse:
    _require_service_account_api_route(request, tenant)
    store = _get_session_store(request)
    await _verify_session_access(store, session_id, tenant)

    from surogates.config import enqueue_session, load_settings
    from surogates.harness.outcomes import build_defined_outcome_from_event

    sent: list[SentSessionEvent] = []
    for event in body.events:
        if event.type != EventType.USER_DEFINE_OUTCOME.value:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported event type: {event.type}",
            )
        if event.rubric is None or event.rubric.type != "text" or not (event.rubric.content or "").strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="user.define_outcome requires rubric {type: 'text', content: ...}.",
            )
        session = await store.get_session(session_id)
        state, processed_at = build_defined_outcome_from_event(
            description=event.description or "",
            rubric=event.rubric.content or "",
            max_iterations=event.max_iterations,
            settings=load_settings().outcomes,
        )
        event_id = await store.emit_event(
            session_id,
            EventType.USER_DEFINE_OUTCOME,
            {
                "description": state.description,
                "rubric": {"type": "text", "content": state.rubric},
                "max_iterations": state.max_iterations,
                "outcome_id": state.id,
                "processed_at": processed_at,
            },
        )
        await store.update_session_config_key(session_id, "outcome", state.to_config())
        await store.emit_synthetic_user_message(
            session_id,
            content=state.description,
            synthetic="outcome_kickoff",
            metadata={"outcome_id": state.id},
        )
        redis = getattr(request.app.state, "redis", None)
        if redis is not None:
            await enqueue_session(redis, session.agent_id, session_id)
        sent.append(SentSessionEvent(
            type=EventType.USER_DEFINE_OUTCOME.value,
            event_id=event_id,
            outcome_id=state.id,
            processed_at=processed_at,
        ))
    return SendEventsResponse(events=sent)
```

Add `build_defined_outcome_from_event(...)` to `surogates/harness/outcomes.py`:

```python
def build_defined_outcome_from_event(
    *,
    description: str,
    rubric: str,
    max_iterations: int | None,
    settings: Any,
) -> tuple[OutcomeState, str]:
    from datetime import datetime, timezone

    if not (rubric or "").strip():
        raise ValueError("rubric is required")
    now_iso = datetime.now(timezone.utc).isoformat()
    state = start_outcome(
        description,
        rubric=rubric,
        max_iterations=max_iterations or getattr(settings, "max_iterations", DEFAULT_MAX_ITERATIONS),
        now_iso=now_iso,
    )
    return state, now_iso
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/integration/test_api.py::test_define_outcome_event_requires_rubric tests/integration/test_api.py::test_define_outcome_event_persists_state_and_enqueues -q
```

Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/events.py surogates/harness/outcomes.py tests/integration/test_api.py
git commit -m "feat(outcomes): accept define outcome events"
```

## Task 8: Add `/goal` Command Handler

**Files:**
- Modify: `surogates/harness/loop.py`
- Test: `tests/test_outcome_harness.py`

- [ ] **Step 1: Write fake-store tests for command handling**

Create `tests/test_outcome_harness.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.harness.loop import AgentHarness
from surogates.session.events import EventType
from surogates.session.models import Session, SessionLease


def _session(config: dict | None = None) -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-a",
        channel="web",
        status="active",
        config=config or {},
        created_at=now,
        updated_at=now,
    )


def _lease(session_id):
    return SessionLease(
        session_id=session_id,
        owner_id="worker-a",
        lease_token=uuid4(),
        expires_at=datetime.now(timezone.utc),
    )


class FakeStore:
    def __init__(self) -> None:
        self.events: list[tuple[object, EventType, dict]] = []
        self.config_updates: list[tuple[object, str, dict]] = []
        self.config_clears: list[tuple[object, str]] = []
        self.cursor_advances: list[dict] = []
        self.next_event_id = 1

    async def emit_event(self, session_id, event_type, data):
        self.events.append((session_id, event_type, data))
        event_id = self.next_event_id
        self.next_event_id += 1
        return event_id

    async def update_session_config_key(self, session_id, key, value):
        self.config_updates.append((session_id, key, value))

    async def clear_session_config_key(self, session_id, key):
        self.config_clears.append((session_id, key))

    async def advance_harness_cursor(self, session_id, through_event_id, lease_token):
        self.cursor_advances.append({
            "session_id": session_id,
            "through_event_id": through_event_id,
            "lease_token": lease_token,
        })


def _harness(store: FakeStore) -> AgentHarness:
    return AgentHarness(
        store=store,
        llm_client=SimpleNamespace(),
        tool_registry=SimpleNamespace(get_schemas=lambda names=None: []),
        tenant=SimpleNamespace(user_id=uuid4(), org_id=uuid4()),
        worker_id="worker-a",
    )


@pytest.mark.asyncio
async def test_handle_goal_set_persists_state_and_emits_defined_event():
    store = FakeStore()
    harness = _harness(store)
    session = _session()
    lease = _lease(session.id)

    await harness._handle_goal_command(session, "/goal Fix all tests", lease)

    assert store.config_updates[0][1] == "outcome"
    state = store.config_updates[0][2]
    assert state["description"] == "Fix all tests"
    assert state["status"] == "active"
    assert any(e[1] == EventType.OUTCOME_DEFINED for e in store.events)
    response = [e for e in store.events if e[1] == EventType.LLM_RESPONSE][-1]
    assert "Outcome defined" in response[2]["message"]["content"]
    assert store.cursor_advances


@pytest.mark.asyncio
async def test_handle_goal_status_without_goal_reports_no_outcome():
    store = FakeStore()
    harness = _harness(store)
    session = _session()
    lease = _lease(session.id)

    await harness._handle_goal_command(session, "/goal status", lease)

    response = [e for e in store.events if e[1] == EventType.LLM_RESPONSE][-1]
    assert "No active outcome" in response[2]["message"]["content"]


@pytest.mark.asyncio
async def test_handle_goal_pause_updates_existing_state():
    store = FakeStore()
    harness = _harness(store)
    session = _session(config={
        "outcome": {
            "id": "outc_test",
            "description": "Fix tests",
            "rubric": "pytest passes",
            "status": "active",
            "iteration": 1,
            "max_iterations": 5,
        }
    })
    lease = _lease(session.id)

    await harness._handle_goal_command(session, "/goal pause", lease)

    state = store.config_updates[0][2]
    assert state["status"] == "paused"
    assert state["paused_reason"] == "user-paused"
    assert any(e[1] == EventType.OUTCOME_PAUSED for e in store.events)


@pytest.mark.asyncio
async def test_handle_goal_clear_marks_config_cleared():
    store = FakeStore()
    harness = _harness(store)
    session = _session(config={
        "outcome": {
            "id": "outc_test",
            "description": "Fix tests",
            "rubric": "pytest passes",
            "status": "active",
            "iteration": 1,
            "max_iterations": 5,
        }
    })
    lease = _lease(session.id)

    await harness._handle_goal_command(session, "/goal clear", lease)

    assert store.config_clears == [(session.id, "outcome")]
    assert any(e[1] == EventType.OUTCOME_CLEARED for e in store.events)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_outcome_harness.py -q
```

Expected: `_handle_goal_command` missing.

- [ ] **Step 3: Implement `_handle_goal_command` and wire wake dispatch**

Modify `surogates/harness/loop.py` imports:

```python
from datetime import datetime, timezone
from surogates.harness.outcomes import (
    OutcomeState,
    parse_goal_command,
    start_outcome,
)
```

In `wake()`, before `/loop` handling:

```python
            if last_user_content.startswith("/goal"):
                await self._handle_goal_command(session, last_user_content, lease)
                return
```

Add methods near `_handle_loop_command`:

```python
    def _outcome_settings(self) -> Any:
        try:
            from surogates.config import load_settings
            return load_settings().outcomes
        except Exception:
            return SimpleNamespace(max_iterations=3, max_parse_failures=3)

    async def _handle_goal_command(
        self,
        session: Session,
        content: str,
        lease: SessionLease,
    ) -> None:
        args = content[len("/goal") :].strip()
        command = parse_goal_command(args)
        current = OutcomeState.from_config((session.config or {}).get("outcome"))
        message = ""

        if command.action == "status":
            message = self._format_outcome_status(current)
        elif command.action == "set":
            settings = self._outcome_settings()
            now_iso = datetime.now(timezone.utc).isoformat()
            state = start_outcome(
                command.text,
                rubric=command.rubric,
                max_iterations=getattr(settings, "max_iterations", 3),
                now_iso=now_iso,
            )
            await self._store.update_session_config_key(session.id, "outcome", state.to_config())
            await self._store.emit_event(
                session.id,
                EventType.OUTCOME_DEFINED,
                {
                    "outcome_id": state.id,
                    "description": state.description,
                    "rubric": state.rubric,
                    "max_iterations": state.max_iterations,
                },
            )
            message = f"Outcome defined ({state.max_iterations} iterations): {state.description}"
        elif command.action == "pause":
            if current is None or current.status not in {"active", "paused"}:
                message = "No active outcome. Set one with /goal <text>."
            else:
                current.status = "paused"
                current.paused_reason = "user-paused"
                current.updated_at = datetime.now(timezone.utc).isoformat()
                await self._store.update_session_config_key(session.id, "outcome", current.to_config())
                await self._store.emit_event(
                    session.id,
                    EventType.OUTCOME_PAUSED,
                    {"outcome_id": current.id, "reason": current.paused_reason},
                )
                message = f"Outcome paused: {current.description}"
        elif command.action == "resume":
            if current is None or current.status not in {"paused", "max_iterations_reached"}:
                message = "No paused outcome to resume."
            else:
                current.status = "active"
                current.paused_reason = None
                current.updated_at = datetime.now(timezone.utc).isoformat()
                await self._store.update_session_config_key(session.id, "outcome", current.to_config())
                message = f"Outcome resumed: {current.description}"
        elif command.action == "clear":
            await self._store.clear_session_config_key(session.id, "outcome")
            await self._store.emit_event(
                session.id,
                EventType.OUTCOME_CLEARED,
                {"outcome_id": current.id if current else None},
            )
            message = "Outcome cleared." if current else "No active outcome."

        event_id = await self._store.emit_event(
            session.id,
            EventType.LLM_RESPONSE,
            {"message": {"role": "assistant", "content": message}},
        )
        await self._store.advance_harness_cursor(
            session.id,
            through_event_id=event_id,
            lease_token=lease.lease_token,
        )

    def _format_outcome_status(self, state: OutcomeState | None) -> str:
        if state is None or state.status == "cleared":
            return "No active outcome. Set one with /goal <text>."
        bits = [
            f"Outcome ({state.status}, {state.iteration}/{state.max_iterations} iterations): {state.description}"
        ]
        if state.last_explanation:
            bits.append(f"Last evaluation: {state.last_explanation}")
        if state.paused_reason:
            bits.append(f"Paused reason: {state.paused_reason}")
        return "\n".join(bits)
```

If `SimpleNamespace` is not imported in `loop.py`, add:

```python
from types import SimpleNamespace
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_outcome_harness.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/loop.py tests/test_outcome_harness.py
git commit -m "feat(outcomes): handle goal slash command"
```

## Task 9: Add Post-Turn Outcome Evaluation

**Files:**
- Modify: `surogates/harness/loop.py`
- Test: `tests/test_outcome_harness.py`

- [ ] **Step 1: Add failing post-turn tests**

Append to `tests/test_outcome_harness.py`:

```python
from surogates.harness.outcomes import start_outcome


class FakeRedis:
    def __init__(self) -> None:
        self.zadds: list[tuple[str, dict]] = []

    async def zadd(self, key, mapping):
        self.zadds.append((key, mapping))


class FakeEvaluatorStore(FakeStore):
    async def emit_synthetic_user_message(self, session_id, *, content, synthetic, metadata=None):
        data = {"content": content, "synthetic": synthetic}
        if metadata:
            data.update(metadata)
        return await self.emit_event(session_id, EventType.USER_MESSAGE, data)


def _active_outcome_config() -> dict:
    state = start_outcome(
        "Fix tests",
        rubric="pytest passes",
        max_iterations=3,
        now_iso="2026-05-12T10:00:00Z",
    )
    return state.to_config()


@pytest.mark.asyncio
async def test_post_turn_outcome_evaluation_enqueues_continuation(monkeypatch):
    store = FakeEvaluatorStore()
    redis = FakeRedis()
    harness = _harness(store)
    harness._redis = redis
    session = _session(config={"outcome": _active_outcome_config()})
    lease = _lease(session.id)

    async def fake_evaluate(*, state, latest_response, model):
        from surogates.harness.outcomes import parse_outcome_evaluation
        return parse_outcome_evaluation(
            '{"result":"needs_revision","explanation":"Tests still fail","feedback":"Run pytest"}'
        )

    monkeypatch.setattr(harness, "_evaluate_outcome", fake_evaluate)

    handled = await harness._maybe_continue_outcome(
        session,
        lease,
        latest_response="I fixed one test",
        response_event_id=10,
        model="gpt-4o",
    )

    assert handled is True
    assert store.config_updates[-1][2]["status"] == "active"
    assert any(e[1] == EventType.OUTCOME_EVALUATION_START for e in store.events)
    assert any(e[1] == EventType.OUTCOME_EVALUATION_ONGOING for e in store.events)
    assert any(e[1] == EventType.OUTCOME_EVALUATION_END for e in store.events)
    synthetic = [e for e in store.events if e[1] == EventType.USER_MESSAGE][-1]
    assert synthetic[2]["synthetic"] == "outcome_continuation"
    assert "Run pytest" in synthetic[2]["content"]
    assert redis.zadds
    assert store.cursor_advances[-1]["through_event_id"] == store.next_event_id - 1


@pytest.mark.asyncio
async def test_post_turn_outcome_evaluation_completes_when_satisfied(monkeypatch):
    store = FakeEvaluatorStore()
    harness = _harness(store)
    harness._complete_session = AsyncMock()
    session = _session(config={"outcome": _active_outcome_config()})
    lease = _lease(session.id)

    async def fake_evaluate(*, state, latest_response, model):
        from surogates.harness.outcomes import parse_outcome_evaluation
        return parse_outcome_evaluation(
            '{"result":"satisfied","explanation":"pytest passes","feedback":""}'
        )

    monkeypatch.setattr(harness, "_evaluate_outcome", fake_evaluate)

    handled = await harness._maybe_continue_outcome(
        session,
        lease,
        latest_response="All tests pass",
        response_event_id=10,
        model="gpt-4o",
    )

    assert handled is False
    assert store.config_updates[-1][2]["status"] == "satisfied"
    harness._complete_session.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_outcome_harness.py::test_post_turn_outcome_evaluation_enqueues_continuation tests/test_outcome_harness.py::test_post_turn_outcome_evaluation_completes_when_satisfied -q
```

Expected: `_maybe_continue_outcome` missing.

- [ ] **Step 3: Implement evaluator call and continuation hook**

Modify `surogates/harness/loop.py` imports:

```python
from surogates.config import enqueue_session
from surogates.harness.outcomes import (
    apply_evaluation,
    build_evaluator_messages,
    parse_outcome_evaluation,
)
```

Add methods near outcome command methods:

```python
    async def _evaluate_outcome(
        self,
        *,
        state: OutcomeState,
        latest_response: str,
        model: str,
    ):
        settings = self._outcome_settings()
        eval_model = getattr(settings, "evaluator_model", "") or model
        messages = build_evaluator_messages(state, latest_response)
        try:
            response = await self._llm.chat.completions.create(
                model=eval_model,
                messages=messages,
                temperature=0,
                max_tokens=500,
            )
            raw = self._extract_chat_message_content(response)
        except Exception as exc:
            raw = json.dumps({
                "result": "needs_revision",
                "explanation": f"evaluator error: {type(exc).__name__}",
                "feedback": "Continue working toward the outcome.",
            })
        return parse_outcome_evaluation(raw)

    async def _maybe_continue_outcome(
        self,
        session: Session,
        lease: SessionLease,
        *,
        latest_response: str,
        response_event_id: int,
        model: str,
    ) -> bool:
        state = OutcomeState.from_config((session.config or {}).get("outcome"))
        if state is None or state.status != "active":
            return False

        start_id = await self._store.emit_event(
            session.id,
            EventType.OUTCOME_EVALUATION_START,
            {
                "outcome_id": state.id,
                "iteration": state.iteration,
                "response_event_id": response_event_id,
            },
        )
        await self._store.emit_event(
            session.id,
            EventType.OUTCOME_EVALUATION_ONGOING,
            {"outcome_id": state.id, "iteration": state.iteration},
        )
        evaluation = await self._evaluate_outcome(
            state=state,
            latest_response=latest_response,
            model=model,
        )
        settings = self._outcome_settings()
        decision = apply_evaluation(
            state,
            evaluation,
            now_iso=datetime.now(timezone.utc).isoformat(),
            max_parse_failures=getattr(settings, "max_parse_failures", 3),
        )
        await self._store.update_session_config_key(session.id, "outcome", state.to_config())
        await self._store.emit_event(
            session.id,
            EventType.OUTCOME_EVALUATION_END,
            {
                "outcome_id": state.id,
                "outcome_evaluation_start_id": start_id,
                "iteration": state.iteration,
                "result": decision.result,
                "explanation": evaluation.explanation,
                "feedback": evaluation.feedback,
            },
        )
        if decision.message:
            await self._store.emit_event(
                session.id,
                EventType.LLM_RESPONSE,
                {"message": {"role": "assistant", "content": decision.message}},
            )
        if not decision.should_continue or not decision.continuation_prompt:
            return False

        continuation_event_id = await self._store.emit_synthetic_user_message(
            session.id,
            content=decision.continuation_prompt,
            synthetic="outcome_continuation",
            metadata={"outcome_id": state.id},
        )
        await self._store.emit_event(
            session.id,
            EventType.OUTCOME_CONTINUATION,
            {
                "outcome_id": state.id,
                "user_message_event_id": continuation_event_id,
                "iteration": state.iteration,
            },
        )
        await self._store.advance_harness_cursor(
            session.id,
            through_event_id=continuation_event_id,
            lease_token=lease.lease_token,
        )
        if self._redis is not None:
            await enqueue_session(self._redis, session.agent_id, session.id)
        return True
```

In `_run_loop`, replace the direct `_complete_session` call after final no-tool response:

```python
                if await self._maybe_continue_outcome(
                    session,
                    lease,
                    latest_response=assistant_message.get("content") or "",
                    response_event_id=event_id,
                    model=model_id,
                ):
                    return

                await self._complete_session(
                    session, messages, lease, reason="completed",
                    through_event_id=event_id,
                    cost_tracker=cost_tracker,
                )
                return
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_outcome_harness.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/loop.py tests/test_outcome_harness.py
git commit -m "feat(outcomes): evaluate outcomes after turns"
```

## Task 10: Prevent Completed Status During Active Continuation

**Files:**
- Modify: `surogates/harness/loop.py`
- Test: `tests/test_outcome_harness.py`

- [ ] **Step 1: Add a regression test for no session completion on continuation**

Append to `tests/test_outcome_harness.py`:

```python
@pytest.mark.asyncio
async def test_outcome_continuation_does_not_complete_session(monkeypatch):
    store = FakeEvaluatorStore()
    redis = FakeRedis()
    harness = _harness(store)
    harness._redis = redis
    harness._complete_session = AsyncMock()
    session = _session(config={"outcome": _active_outcome_config()})
    lease = _lease(session.id)

    async def fake_evaluate(*, state, latest_response, model):
        from surogates.harness.outcomes import parse_outcome_evaluation
        return parse_outcome_evaluation(
            '{"result":"needs_revision","explanation":"Missing","feedback":"Continue"}'
        )

    monkeypatch.setattr(harness, "_evaluate_outcome", fake_evaluate)

    continued = await harness._maybe_continue_outcome(
        session,
        lease,
        latest_response="partial",
        response_event_id=10,
        model="gpt-4o",
    )

    assert continued is True
    harness._complete_session.assert_not_called()
```

- [ ] **Step 2: Run test**

Run:

```bash
pytest tests/test_outcome_harness.py::test_outcome_continuation_does_not_complete_session -q
```

Expected: pass if Task 9 was wired correctly. If it fails, inspect `_run_loop` and ensure it returns immediately when `_maybe_continue_outcome(...)` returns `True`.

- [ ] **Step 3: Run focused harness tests**

Run:

```bash
pytest tests/test_outcome_harness.py tests/test_harness_pending.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit only if a fix was needed**

```bash
git add surogates/harness/loop.py tests/test_outcome_harness.py
git commit -m "fix(outcomes): keep active outcome sessions running"
```

If no code changed, do not commit.

## Task 11: Document Outcome API and `/goal` Behavior

**Files:**
- Modify: `docs/commands/index.md`
- Modify: `docs/appendices/api-reference.md`

- [ ] **Step 1: Update command resolution list**

In `docs/commands/index.md`, change:

```markdown
1. Builtin command handlers: `/clear`, `/compress`, `/loop`.
```

to:

```markdown
1. Builtin command handlers: `/clear`, `/compress`, `/goal`, `/loop`.
```

- [ ] **Step 2: Add outcome sections to commands docs**

Insert before `/loop`:

```markdown
### `user.define_outcome`

Defines an outcome through the API event stream. This is the canonical
programmatic shape; `/goal` is a chat convenience wrapper.

```http
POST /v1/sessions/{session_id}/events
```

```json
{
  "events": [
    {
      "type": "user.define_outcome",
      "description": "Fix every failing test in tests/",
      "rubric": {
        "type": "text",
        "content": "- The final response includes the passing pytest command\n- No failing tests remain"
      },
      "max_iterations": 5
    }
  ]
}
```

Do not also send a user message to kick it off. Surogates records the
definition, persists the active outcome, appends a synthetic kickoff message,
and wakes the session.

### `/goal <description>`

Defines an outcome for the current session. Surogates starts work immediately
from the normal conversation flow, then evaluates each final assistant response
against the outcome and its rubric. If the evaluator says more revision is
needed, Surogates appends a synthetic continuation message and wakes the same
session again.

Examples:

```text
/goal Fix every failing test in tests/ and report the command that passes

/goal Build a DCF model for Costco

Rubric:
- Produces an .xlsx file
- Uses five years of historical revenue
- Includes WACC and terminal value assumptions
- Includes a sensitivity analysis
```

Controls:

| Command | Behavior |
|---|---|
| `/goal` or `/goal status` | Show current outcome state and last evaluator result |
| `/goal pause` | Pause automatic continuation without clearing state |
| `/goal resume` | Resume a paused outcome |
| `/goal clear` | Clear the current outcome |

Outcome behavior:

- One outcome is active per session.
- Default iteration budget is `outcomes.max_iterations` (`3`, max `20`).
- Evaluator lifecycle events are emitted as `span.outcome_evaluation_start`,
  `span.outcome_evaluation_ongoing`, and `span.outcome_evaluation_end`.
- Continuations are normal `user.message` events marked with
  `synthetic: outcome_continuation`.
- User messages can steer the work while the outcome is active; evaluation
  resumes after the user-directed turn.
- Evaluator failures fail open as `needs_revision`; repeated unparseable
  evaluator output pauses the outcome.
```

- [ ] **Step 3: Add API reference entry**

In `docs/appendices/api-reference.md`, add near the existing session events endpoints:

```markdown
### `POST /v1/sessions/{id}/events`

Send control-plane session events. The first supported event is
`user.define_outcome`, which starts an outcome-oriented work loop without a
separate user message.

Request:

```json
{
  "events": [
    {
      "type": "user.define_outcome",
      "description": "Fix every failing test in tests/",
      "rubric": {
        "type": "text",
        "content": "- The final response includes the passing pytest command\n- No failing tests remain"
      },
      "max_iterations": 5
    }
  ]
}
```

Response:

```json
{
  "events": [
    {
      "type": "user.define_outcome",
      "event_id": 123,
      "outcome_id": "outc_...",
      "processed_at": "2026-05-12T10:00:00Z"
    }
  ]
}
```
```

- [ ] **Step 4: Run docs grep sanity check**

Run:

```bash
rg -n "/goal|user.define_outcome|span.outcome_evaluation|outcomes.max_iterations" docs/commands/index.md docs/appendices/api-reference.md config.yaml.example
```

Expected: matches in both docs and config example.

- [ ] **Step 5: Commit**

```bash
git add docs/commands/index.md docs/appendices/api-reference.md
git commit -m "docs(outcomes): document goal command"
```

## Task 12: Run Focused Verification

**Files:**
- No code changes expected.

- [ ] **Step 1: Run unit tests**

```bash
pytest tests/test_outcomes.py tests/test_slash_skill.py tests/test_loop_command.py tests/test_outcome_harness.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run session store integration tests**

```bash
pytest tests/integration/test_session_store.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run related harness tests**

```bash
pytest tests/test_harness_pending.py tests/test_harness_resilience.py tests/test_title_generator.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Run broader Python test suite if time allows**

```bash
pytest tests -q
```

Expected: all tests pass. If unrelated existing failures appear, record exact failing tests and confirm the focused outcome tests are green.

## Self-Review Notes

- Claude concepts included: explicit outcome, rubric, separate evaluator context, structured evaluation events, one active outcome at a time, bounded iterations, user steering, interrupt/pause semantics.
- Hermes concepts included: `/goal` command UX, pause/resume/clear/status controls, continuation prompt, fail-open evaluator, parse-failure guard, budget backstop.
- Surogates fit: state in `sessions.config`, events in the append-only log, continuation via synthetic `user.message`, Redis wake through existing `enqueue_session`.
- The plan adds `POST /sessions/{id}/events` only for `user.define_outcome`; it does not create a broad arbitrary event ingestion API.
- The first version evaluates the assistant's final response, not workspace files directly. Rubrics can still require the assistant to verify files/tests. A future task can add evaluator access to artifact metadata or workspace summaries.
