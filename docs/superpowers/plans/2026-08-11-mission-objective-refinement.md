# Mission Objective Refinement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a mission whose rubric was written wrong be re-aimed — the judge authors a replacement rubric, the user authorizes it with a slash command, and the harness applies the recorded text — instead of dying `blocked`/`failed` on a defect in its own acceptance criteria.

**Architecture:** Three parties hold one power each. The judge authors a `proposed_rubric` but cannot apply it. The coordinator relays it to the user but authors nothing. The user authorizes with `/mission accept`, which reads the rubric from the `mission.refinement_proposed` **event** — the command takes no rubric argument, the same bypass-proof idiom as `merge_experiment` taking no score argument. `description` is never mutable; only `rubric` is. State lives in the append-only event log, so there is no migration and no new mission status (`paused` + `paused_reason="awaiting_refinement"`).

**Tech Stack:** Python 3.12, async SQLAlchemy 2.x (asyncpg), pydantic v2, pytest + pytest-asyncio, testcontainers (PostgreSQL 16 / Redis 7) for integration tests.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-11-mission-objective-refinement-design.md`. Read it before Task 1.
- Repo: `/work/surogates`. Branch: `feat/mission-objective-refinement` (already created and holding the spec commit).
- **No migration.** No column is added to `missions`. No new value is added to `MissionStatus`. Both are deliberate: a new status has to land in the SDK's `types.ts` and its rebuilt dist, a known release-breaker, and `store.py:319` (`pause_if_budget_exhausted`) is the existing precedent for reusing `paused` + `paused_reason` instead.
- **`description` is immutable.** Nothing in this plan writes to it.
- **`iteration` is never reset by an amendment.** The amendment changes the target, not the allowance.
- `MAX_MISSION_AMENDMENTS = 2`, counted from applied `mission.amended` events, not from proposals.
- Do **not** run `uv run` in this repo. Run `pytest` directly.
- Commit messages follow Conventional Commits (`type(scope): subject`). No `Co-Authored-By` trailer.
- Integration tests need Docker (testcontainers). Unit tests under `tests/missions/` do not.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `surogates/harness/loop_mission_evaluator.py` | `_MissionVerdict` schema — the two new proposal fields | 1 |
| `surogates/missions/evaluator.py` | Judge prompt, stagnation hint, `apply_verdict` refinement branch, `_propose_refinement`, relay template, `MAX_MISSION_AMENDMENTS` | 1, 3 |
| `surogates/session/events.py` | Two new `EventType` members | 2 |
| `surogates/session/store.py` | `latest_mission_proposal`, `count_mission_amendments` | 2 |
| `surogates/missions/store.py` | `amend_rubric` | 2 |
| `surogates/missions/commands.py` | `accept`/`reject` parsing, `handle_mission_accept`, `handle_mission_reject`, `MissionHandlerResult.kickoff_synthetic` | 4, 5 |
| `surogates/harness/loop_outcome_commands.py` | Two dispatch branches + usage string | 6 |
| `docs/tasks/index.md` | Operator-facing docs for the two verbs | 7 |
| `tests/missions/test_refinement_unit.py` | Pure-unit: verdict schema, command parsing | 1, 4 |
| `tests/integration/missions/test_refinement.py` | DB-backed: prompt, pause branch, accept, reject, cap | 1, 3, 5 |

---

### Task 1: Judge authors the proposal

The judge gains the ability to *write* a replacement rubric. It gains no ability to apply one — that is Tasks 3 and 5.

**Files:**
- Modify: `surogates/harness/loop_mission_evaluator.py:238-250` (`_MissionVerdict`)
- Modify: `surogates/missions/evaluator.py:163-191` (`_SYSTEM_PROMPT`), `:194-290` (`build_evaluator_prompt`), `:293-312` (`_render_history`)
- Create: `tests/missions/test_refinement_unit.py`
- Create: `tests/integration/missions/test_refinement.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_MissionVerdict` with fields `proposed_rubric: str = ""` and `refinement_evidence: str = ""`. Task 3 reads both off the verdict dict returned by `_build_mission_judge`'s `judge()` (which returns `model_dump()`, so both keys are always present).

- [ ] **Step 1: Write the failing unit test for the verdict schema**

Create `tests/missions/test_refinement_unit.py`:

```python
"""Unit tests for evidence-gated rubric refinement.

The judge may author a replacement rubric; it may never apply one. These
cover the authoring half -- the schema and the command parsing. The
applying half is DB-backed and lives in
tests/integration/missions/test_refinement.py.
"""
from __future__ import annotations


def test_verdict_defaults_the_proposal_fields_empty():
    """An existing judge response, unaware of refinement, still validates."""
    from surogates.harness.loop_mission_evaluator import _MissionVerdict

    v = _MissionVerdict.model_validate(
        {"result": "blocked", "explanation": "stuck", "feedback": ""},
    )
    assert v.proposed_rubric == ""
    assert v.refinement_evidence == ""


def test_verdict_carries_a_proposal():
    from surogates.harness.loop_mission_evaluator import _MissionVerdict

    v = _MissionVerdict.model_validate({
        "result": "blocked",
        "explanation": "the rubric names a file the build never emits",
        "feedback": "",
        "proposed_rubric": "Satisfied when dist/bundle.js exists and tests pass.",
        "refinement_evidence": "T9f2a build task: no dist/app.js in output tree",
    })
    assert v.proposed_rubric.startswith("Satisfied when dist/bundle.js")
    assert "T9f2a" in v.refinement_evidence


def test_verdict_dump_always_carries_both_keys():
    """Task 3 reads these off a plain dict; they must never be absent."""
    from surogates.harness.loop_mission_evaluator import _MissionVerdict

    dumped = _MissionVerdict.model_validate({"result": "satisfied"}).model_dump()
    assert "proposed_rubric" in dumped
    assert "refinement_evidence" in dumped
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /work/surogates && pytest tests/missions/test_refinement_unit.py -v`
Expected: FAIL — `pydantic_core.ValidationError` on the extra keys, or `AttributeError: 'proposed_rubric'`.

- [ ] **Step 3: Add the two fields to `_MissionVerdict`**

In `surogates/harness/loop_mission_evaluator.py`, replace the body of `_MissionVerdict` (keep the existing docstring, append to it):

```python
class _MissionVerdict(BaseModel):
    """Structured shape the judge must return.

    Used both for ``outlines``-backed constrained generation (preferred,
    via :func:`generate_structured`) and for tolerant fallback parsing
    when outlines isn't installed or fails to coerce the model's output.
    Keeping the schema in one place means the prompt's documented JSON
    shape and the parser's expected shape stay in lockstep.

    ``proposed_rubric`` / ``refinement_evidence`` are the judge's
    *authorship* of a replacement rubric. They carry no authority: only
    ``/mission accept`` applies one, and it reads the text from the
    recorded event, not from here. Both default empty so a judge response
    written before this existed still validates. See
    docs/superpowers/specs/2026-08-11-mission-objective-refinement-design.md.
    """

    result: Literal["satisfied", "needs_revision", "blocked", "failed"]
    explanation: str = ""
    feedback: str = ""
    proposed_rubric: str = ""
    refinement_evidence: str = ""
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `cd /work/surogates && pytest tests/missions/test_refinement_unit.py -v`
Expected: 3 passed.

- [ ] **Step 5: Write the failing integration test for the prompt**

Create `tests/integration/missions/test_refinement.py`:

```python
"""Evidence-gated rubric refinement, end to end over a real database.

A mission whose rubric is the defect used to have three exits, all losses:
terminate `blocked`, terminate `failed`, or grind out max_iterations against
a target it cannot hit. Here the judge authors a replacement, the user
authorizes it, and the harness applies the *recorded* text.

See docs/superpowers/specs/2026-08-11-mission-objective-refinement-design.md.
"""
from __future__ import annotations

import pytest

from surogates.missions.store import MissionStore

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _mission(session_factory, org_id, user_id, chat_session, **kw):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator", description="ship the bundle",
        rubric="Satisfied when dist/app.js exists.", **kw,
    )
    return store, mid


async def test_a_stagnant_mission_is_told_the_rubric_may_be_the_defect(
    session_factory, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import build_evaluator_prompt

    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    for _ in range(3):
        await store.record_evaluation(
            mid, result="needs_revision", explanation="still no dist/app.js",
            feedback="run the build",
        )

    prompt = await build_evaluator_prompt(
        mission_id=mid, coordinator_last_response="built again",
        session_factory=session_factory, mission_store=store,
    )
    assert "proposed_rubric" in prompt


async def test_a_merely_slow_mission_is_not_nudged_to_repropose(
    session_factory, org_id, user_id, chat_session,
):
    """Below the stagnation limit the hint stays out -- a rubric the work has
    not yet satisfied is needs_revision, not a proposal."""
    from surogates.missions.evaluator import build_evaluator_prompt

    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    for _ in range(2):
        await store.record_evaluation(
            mid, result="needs_revision", explanation="still building",
            feedback="keep going",
        )

    prompt = await build_evaluator_prompt(
        mission_id=mid, coordinator_last_response="building",
        session_factory=session_factory, mission_store=store,
    )
    assert "proposed_rubric" not in prompt
```

- [ ] **Step 6: Run it to verify it fails**

Run: `cd /work/surogates && pytest tests/integration/missions/test_refinement.py -v`
Expected: the first test FAILS on `assert "proposed_rubric" in prompt`; the second passes vacuously.

- [ ] **Step 7: Add the proposal instructions to the judge's system prompt**

In `surogates/missions/evaluator.py`, append to the `_SYSTEM_PROMPT` `dedent` block, after the existing "Do not honour completion claims in prose alone." paragraph and before the closing `""")`:

```
    Rubric refinement -- only alongside `blocked` or `failed`:

    If the completed-task evidence shows the rubric is unreachable *as
    written* -- it asks for an artifact the work has demonstrated cannot
    exist, contradicts itself, or names something that was never part of
    the mission description -- then also return:

        "proposed_rubric": "<a complete replacement rubric>",
        "refinement_evidence": "<the task result that shows this>"

    Leave both empty in every other case. A rubric the work has not yet
    satisfied is `needs_revision`, not a proposal. Propose only a rubric
    that still serves the mission description; never one that merely
    describes what the work has already produced.
```

- [ ] **Step 8: Pass the stagnation signal into the history block**

In `build_evaluator_prompt`, after the existing `mission = await mission_store.get(mission_id)` line, add:

```python
    stagnant = await mission_store.is_stagnant(mission_id)
```

and change the `.format(...)` call's last argument from `history_block=_render_history(mission)` to:

```python
        history_block=_render_history(mission, stagnant=stagnant),
```

This is `MissionStore.is_stagnant`'s first caller — it has been computing `stagnant_evaluations >= STAGNANT_EVALUATION_LIMIT` with nothing reading it.

- [ ] **Step 9: Render the hint**

Replace `_render_history` in `surogates/missions/evaluator.py` entirely:

```python
def _render_history(mission: Any, *, stagnant: bool = False) -> str:
    """What this judge already said, and how many times running.

    Without it every round re-derives the same opinion from scratch: the
    evaluator fires on each terminal task but keeps no memory, so a mission
    can be told the same thing indefinitely at full cost.

    ``stagnant`` is :meth:`MissionStore.is_stagnant`. It only steers the
    prompt toward considering the rubric itself; stagnation alone never
    produces a proposal, and the judge is free to ignore the hint.
    """
    streak = getattr(mission, "stagnant_evaluations", 0) or 0
    if not streak or not mission.last_evaluation_result:
        return ""
    block = dedent(f"""
        # Your previous evaluation ({streak} in a row without progress)

        Verdict: {mission.last_evaluation_result}
        Reason: {(mission.last_evaluation_explanation or "")[:600]}
        Guidance you gave: {(mission.last_evaluation_feedback or "")[:600]}

        If the same blocker is still present after {streak} rounds, say so
        plainly and return `blocked` rather than repeating the guidance.
        """)
    if stagnant:
        block += dedent("""
        This blocker has survived every round of the streak. Consider whether
        the rubric itself is the defect rather than the work. If it is,
        return `blocked` with a `proposed_rubric` that still serves the
        mission description.
        """)
    return block
```

- [ ] **Step 10: Run both test files to verify they pass**

Run: `cd /work/surogates && pytest tests/missions/test_refinement_unit.py tests/integration/missions/test_refinement.py tests/integration/missions/test_stagnation.py -v`
Expected: all pass. `test_stagnation.py` is included because it exercises `build_evaluator_prompt` and `_render_history` — it must not regress.

- [ ] **Step 11: Commit**

```bash
cd /work/surogates
git add surogates/harness/loop_mission_evaluator.py surogates/missions/evaluator.py tests/missions/test_refinement_unit.py tests/integration/missions/test_refinement.py
git commit -m "feat(missions): let the judge author a replacement rubric"
```

---

### Task 2: Durable state — events, event reads, and the rubric mutator

No behaviour change yet. This task lays the three primitives Tasks 3 and 5 call.

**Files:**
- Modify: `surogates/session/events.py:167-173` (mission `EventType` block)
- Modify: `surogates/session/store.py` (add after `latest_todo_snapshot`, which ends at :1624)
- Modify: `surogates/missions/store.py` (add after `set_budget`, which ends at :264)
- Modify: `tests/integration/missions/test_refinement.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `EventType.MISSION_REFINEMENT_PROPOSED = "mission.refinement_proposed"`
  - `EventType.MISSION_AMENDED = "mission.amended"`
  - `SessionStore.latest_mission_proposal(session_id, mission_id) -> dict | None`
  - `SessionStore.count_mission_amendments(session_id, mission_id) -> int`
  - `MissionStore.amend_rubric(mission_id, *, new_rubric: str) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/missions/test_refinement.py`:

```python
async def test_amend_rubric_replaces_the_text_and_reactivates(
    session_factory, org_id, user_id, chat_session,
):
    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    await store.record_evaluation(
        mid, result="needs_revision", explanation="no", feedback="f",
    )
    await store.set_status(mid, "paused", paused_reason="awaiting_refinement")

    await store.amend_rubric(mid, new_rubric="Satisfied when dist/bundle.js exists.")

    m = await store.get(mid)
    assert m.rubric == "Satisfied when dist/bundle.js exists."
    assert m.status == "active"
    assert m.paused_reason is None
    assert m.stagnant_evaluations == 0
    assert m.description == "ship the bundle"


async def test_amend_rubric_leaves_the_iteration_allowance_alone(
    session_factory, org_id, user_id, chat_session,
):
    """The amendment changes the target, not the budget."""
    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    for _ in range(4):
        await store.increment_iteration(mid)

    await store.amend_rubric(mid, new_rubric="Satisfied when the build is green.")

    assert (await store.get(mid)).iteration == 4


async def test_amend_rubric_rejects_empty_text(
    session_factory, org_id, user_id, chat_session,
):
    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    with pytest.raises(ValueError):
        await store.amend_rubric(mid, new_rubric="   ")


async def test_proposal_lookup_ignores_another_missions_proposal(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """Events are keyed by session, not mission. A session outlives its
    missions, so a stale proposal from a terminated one must not be found."""
    from surogates.session.events import EventType

    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    await session_store.emit_event(
        chat_session.id, EventType.MISSION_REFINEMENT_PROPOSED,
        {"mission_id": "00000000-0000-0000-0000-0000000000ff",
         "proposed_rubric": "someone else's rubric"},
    )
    assert await session_store.latest_mission_proposal(chat_session.id, mid) is None

    await session_store.emit_event(
        chat_session.id, EventType.MISSION_REFINEMENT_PROPOSED,
        {"mission_id": str(mid), "proposed_rubric": "ours"},
    )
    found = await session_store.latest_mission_proposal(chat_session.id, mid)
    assert found is not None and found["proposed_rubric"] == "ours"


async def test_proposal_lookup_takes_the_newest(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.session.events import EventType

    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    for text in ("first", "second"):
        await session_store.emit_event(
            chat_session.id, EventType.MISSION_REFINEMENT_PROPOSED,
            {"mission_id": str(mid), "proposed_rubric": text},
        )
    found = await session_store.latest_mission_proposal(chat_session.id, mid)
    assert found["proposed_rubric"] == "second"


async def test_amendment_count_is_per_mission(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.session.events import EventType

    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    assert await session_store.count_mission_amendments(chat_session.id, mid) == 0

    await session_store.emit_event(
        chat_session.id, EventType.MISSION_AMENDED, {"mission_id": str(mid)},
    )
    await session_store.emit_event(
        chat_session.id, EventType.MISSION_AMENDED,
        {"mission_id": "00000000-0000-0000-0000-0000000000ff"},
    )
    assert await session_store.count_mission_amendments(chat_session.id, mid) == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /work/surogates && pytest tests/integration/missions/test_refinement.py -v -k "amend or proposal or amendment"`
Expected: FAIL — `AttributeError: 'EventType' has no attribute 'MISSION_REFINEMENT_PROPOSED'` and `'MissionStore' object has no attribute 'amend_rubric'`.

- [ ] **Step 3: Add the two event types**

In `surogates/session/events.py`, extend the mission block (after `MISSION_CANCELLED = "mission.cancelled"` on :173):

```python
    # Evidence-gated rubric refinement. The proposal event is the *only*
    # source `/mission accept` reads the new rubric from, so it is durable
    # state, not just telemetry. See
    # docs/superpowers/specs/2026-08-11-mission-objective-refinement-design.md.
    MISSION_REFINEMENT_PROPOSED = "mission.refinement_proposed"
    MISSION_AMENDED = "mission.amended"
```

- [ ] **Step 4: Add the two `SessionStore` reads**

In `surogates/session/store.py`, immediately after `latest_todo_snapshot` (ends :1624):

```python
    async def latest_mission_proposal(
        self, session_id: UUID | str, mission_id: UUID | str,
    ) -> dict | None:
        """The newest rubric proposal recorded for *mission_id*, or ``None``.

        This is what ``/mission accept`` applies. The command takes no rubric
        argument on purpose, so the text it commits can only come from here --
        the same shape as ``merge_experiment`` accepting no score.

        Events are keyed by session, not by mission, and a session outlives
        its missions, so the payload's ``mission_id`` is matched in SQL to
        keep a terminated mission's stale proposal out of the result.
        """
        stmt = (
            select(EventRow.data)
            .where(
                EventRow.session_id == session_id,
                EventRow.type == EventType.MISSION_REFINEMENT_PROPOSED.value,
                EventRow.data["mission_id"].astext == str(mission_id),
            )
            .order_by(EventRow.id.desc())
            .limit(1)
        )
        async with self._sf() as db:
            row = (await db.execute(stmt)).scalar_one_or_none()
        return row if isinstance(row, dict) else None

    async def count_mission_amendments(
        self, session_id: UUID | str, mission_id: UUID | str,
    ) -> int:
        """How many rubric amendments have been *applied* to *mission_id*.

        Counts applications, not proposals: a user who declines twice has
        not spent the mission's allowance.
        """
        stmt = (
            select(func.count())
            .select_from(EventRow)
            .where(
                EventRow.session_id == session_id,
                EventRow.type == EventType.MISSION_AMENDED.value,
                EventRow.data["mission_id"].astext == str(mission_id),
            )
        )
        async with self._sf() as db:
            return int((await db.execute(stmt)).scalar_one() or 0)
```

Both reads are served by `idx_events_session_type`. `events.data` is `JSONB` (`surogates/db/models.py:467`), so `.astext` is valid. `select`, `func`, `EventRow` and `EventType` are already imported in this module — confirm before adding, and add only what is missing.

- [ ] **Step 5: Add `MissionStore.amend_rubric`**

In `surogates/missions/store.py`, after `set_budget` (ends :264):

```python
    async def amend_rubric(self, mission_id: UUID, *, new_rubric: str) -> None:
        """Replace the rubric and put the mission back to work.

        The caller passes text it read from a ``mission.refinement_proposed``
        event, never text from the coordinator or from the command line.

        ``iteration`` is deliberately untouched: the amendment changes the
        target, not the allowance. A mission that burned 18 of 20 iterations
        getting the target wrong does not get 20 more for free --
        ``/mission budget`` funds a pivot explicitly.

        ``description`` is never written. It is the standing intent.
        """
        cleaned = (new_rubric or "").strip()
        if not cleaned:
            raise ValueError("amend_rubric requires a non-empty rubric")
        async with self._sf() as db:
            res = await db.execute(
                update(MissionRow)
                .where(MissionRow.id == mission_id)
                .values(
                    rubric=cleaned,
                    status="active",
                    paused_reason=None,
                    stagnant_evaluations=0,
                    updated_at=func.now(),
                )
            )
            if res.rowcount == 0:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            await db.commit()
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd /work/surogates && pytest tests/integration/missions/test_refinement.py -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add surogates/session/events.py surogates/session/store.py surogates/missions/store.py tests/integration/missions/test_refinement.py
git commit -m "feat(missions): record rubric proposals and amendments as events"
```

---

### Task 3: The evaluator pauses instead of terminating

**Files:**
- Modify: `surogates/missions/evaluator.py` — add `MAX_MISSION_AMENDMENTS` near `_TASKS_BLOCK_LIMIT` (:160), add `_REFINEMENT_RELAY_TEMPLATE` after `_CONTINUATION_TEMPLATE` (:349), add `_propose_refinement`, split the terminal branch in `apply_verdict` (:393-398)
- Modify: `tests/integration/missions/test_refinement.py`

**Interfaces:**
- Consumes: `_MissionVerdict`'s `proposed_rubric` / `refinement_evidence` (Task 1); `EventType.MISSION_REFINEMENT_PROPOSED`, `SessionStore.count_mission_amendments` (Task 2).
- Produces: a `mission.refinement_proposed` event whose payload is `{mission_id, old_rubric, proposed_rubric, evidence, held_verdict, explanation}`, and a mission left `paused` with `paused_reason="awaiting_refinement"`. Task 5's handlers read `proposed_rubric`, `evidence` and `held_verdict` from it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/missions/test_refinement.py`:

```python
async def _created(session_factory, session_store, org_id, user_id, chat_session):
    """A mission created through the real command handler, so the session
    config carries active_mission_id the way production does."""
    from surogates.missions.commands import handle_mission_create

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="ship the bundle",
        rubric="Satisfied when dist/app.js exists.",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator", session_store=session_store,
        session_factory=session_factory, mission_store=store,
    )
    return store, created.mission_id


_PROPOSAL = {
    "result": "blocked",
    "explanation": "the rubric names dist/app.js; the build emits dist/bundle.js",
    "feedback": "",
    "proposed_rubric": "Satisfied when dist/bundle.js exists and tests pass.",
    "refinement_evidence": "build task output tree contains only dist/bundle.js",
}


async def test_a_blocked_verdict_with_a_proposal_pauses_instead_of_terminating(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.db.models import Session as ORMSession
    from surogates.missions.evaluator import apply_verdict

    store, mid = await _created(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    await apply_verdict(
        mission_id=mid, verdict=dict(_PROPOSAL),
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )

    m = await store.get(mid)
    assert m.status == "paused"
    assert m.paused_reason == "awaiting_refinement"
    assert m.rubric == "Satisfied when dist/app.js exists."  # not applied yet

    async with session_factory() as db:
        sess = await db.get(ORMSession, chat_session.id)
        assert "active_mission_id" in (sess.config or {})

    proposal = await session_store.latest_mission_proposal(chat_session.id, mid)
    assert proposal["held_verdict"] == "blocked"
    assert proposal["proposed_rubric"] == _PROPOSAL["proposed_rubric"]
    assert proposal["old_rubric"] == "Satisfied when dist/app.js exists."


async def test_a_blocked_verdict_without_a_proposal_still_terminates(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """The un-split branch is unchanged."""
    from surogates.missions.evaluator import apply_verdict

    store, mid = await _created(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    await apply_verdict(
        mission_id=mid,
        verdict={"result": "blocked", "explanation": "no access", "feedback": ""},
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )
    assert (await store.get(mid)).status == "blocked"


async def test_a_satisfied_verdict_ignores_a_proposal(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """Refining the criteria of a mission that just met them is the drift
    case the whole gate exists to prevent."""
    from surogates.missions.evaluator import apply_verdict

    store, mid = await _created(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    await apply_verdict(
        mission_id=mid,
        verdict={**_PROPOSAL, "result": "satisfied"},
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )
    assert (await store.get(mid)).status == "satisfied"


async def test_a_proposal_identical_to_the_standing_rubric_is_not_raised(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """A no-op proposal would cost the user a round-trip to approve nothing."""
    from surogates.missions.evaluator import apply_verdict

    store, mid = await _created(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    await apply_verdict(
        mission_id=mid,
        verdict={**_PROPOSAL,
                 "proposed_rubric": "Satisfied when dist/app.js exists."},
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )
    assert (await store.get(mid)).status == "blocked"


async def test_the_third_proposal_terminates(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import apply_verdict
    from surogates.session.events import EventType

    store, mid = await _created(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    for _ in range(2):
        await session_store.emit_event(
            chat_session.id, EventType.MISSION_AMENDED, {"mission_id": str(mid)},
        )
    await apply_verdict(
        mission_id=mid, verdict=dict(_PROPOSAL),
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )
    assert (await store.get(mid)).status == "blocked"


async def test_a_paused_mission_fires_no_evaluator(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """No judge calls burn while a proposal waits for the user."""
    from surogates.missions.evaluator import apply_verdict, should_evaluate

    store, mid = await _created(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    await apply_verdict(
        mission_id=mid, verdict=dict(_PROPOSAL),
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )
    decision = await should_evaluate(
        mission_id=mid, coordinator_last_response="[[mission-complete]]",
        session_factory=session_factory, mission_store=store,
        rate_limit_seconds=0,
    )
    assert decision.should is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /work/surogates && pytest tests/integration/missions/test_refinement.py -v -k "proposal or terminates or paused_mission or satisfied"`
Expected: the pause/cap/no-evaluator tests FAIL (status is `blocked`, no proposal event); the "still terminates", "satisfied", and "identical" tests pass already.

- [ ] **Step 3: Add the cap constant and the relay template**

In `surogates/missions/evaluator.py`, after `_TASKS_BLOCK_LIMIT: int = 20` (:160):

```python
# Applied rubric amendments allowed per mission. Without a ceiling the
# mechanism inverts: each amendment moves the criteria toward what the work
# already produced, and a long-running mission negotiates its rubric down to
# something trivially satisfiable.
MAX_MISSION_AMENDMENTS: int = 2
```

After `_CONTINUATION_TEMPLATE` (ends :349):

```python
_REFINEMENT_RELAY_TEMPLATE = dedent("""\
    [Mission paused -- rubric refinement proposed]

    The evaluator was about to end this mission as `{held_verdict}`:
    {explanation}

    It judges the rubric itself to be the defect, and proposes replacing it.

    Current rubric:
    {old_rubric}

    Proposed rubric:
    {proposed_rubric}

    Evidence:
    {evidence}

    Relay this to the user verbatim -- both rubrics and the evidence -- and
    end your turn. Do not paraphrase either rubric, and do not argue for or
    against the change: you are the messenger, not a party to it. Only the
    user can authorize it, with `/mission accept` or `/mission reject`.
    `/mission accept` applies the proposed rubric exactly as recorded above;
    nothing you write can change what it commits.
""").strip()
```

- [ ] **Step 4: Add `_propose_refinement`**

In `surogates/missions/evaluator.py`, immediately before `apply_verdict`:

```python
async def _propose_refinement(
    *,
    mission_id: UUID,
    verdict: dict[str, Any],
    coordinator_session_id: UUID,
    session_store: Any,
    mission_store: Any,
) -> bool:
    """Pause the mission on a judge-authored rubric proposal.

    Returns ``True`` when the mission was paused awaiting authorization, so
    the caller skips termination. ``False`` leaves the terminal path exactly
    as it was -- which covers every case where the judge proposed nothing,
    proposed a no-op, or the mission has already spent its amendments.

    Nothing here applies the rubric. The proposal is recorded as an event
    and the mission waits for the user; see
    docs/superpowers/specs/2026-08-11-mission-objective-refinement-design.md.
    """
    proposed = str(verdict.get("proposed_rubric") or "").strip()
    if not proposed:
        return False

    mission = await mission_store.get(mission_id)
    if proposed == (mission.rubric or "").strip():
        return False

    amendments = await session_store.count_mission_amendments(
        coordinator_session_id, mission_id,
    )
    if amendments >= MAX_MISSION_AMENDMENTS:
        logger.info(
            "Mission %s proposed a rubric refinement past the cap (%d); "
            "terminating as %s instead",
            mission_id, MAX_MISSION_AMENDMENTS, verdict.get("result"),
        )
        return False

    held_verdict = str(verdict.get("result") or "blocked")
    explanation = str(verdict.get("explanation") or "")
    evidence = str(verdict.get("refinement_evidence") or "")

    await session_store.emit_event(
        coordinator_session_id, EventType.MISSION_REFINEMENT_PROPOSED,
        {
            "mission_id": str(mission_id),
            "old_rubric": mission.rubric,
            "proposed_rubric": proposed,
            "evidence": evidence,
            "held_verdict": held_verdict,
            "explanation": explanation,
        },
    )
    await mission_store.set_status(
        mission_id, "paused", paused_reason="awaiting_refinement",
    )
    await session_store.emit_event(
        coordinator_session_id, EventType.USER_MESSAGE,
        {
            "content": _REFINEMENT_RELAY_TEMPLATE.format(
                held_verdict=held_verdict,
                explanation=explanation,
                old_rubric=mission.rubric,
                proposed_rubric=proposed,
                evidence=evidence or "(none given)",
            ),
            "synthetic": "mission_refinement_proposed",
        },
    )
    return True
```

`held_verdict` is recorded now, not recomputed on rejection: a second judge call could return a different verdict, and the user is declining *this* proposal against *that* ruling.

- [ ] **Step 5: Split the terminal branch in `apply_verdict`**

Replace the block at `surogates/missions/evaluator.py:393-398`:

```python
    if result in ("satisfied", "blocked", "failed"):
        await mission_store.set_status(mission_id, result)
        await session_store.clear_session_config_key(
            coordinator_session_id, "active_mission_id",
        )
        return
```

with:

```python
    if result in ("satisfied", "blocked", "failed"):
        # A `satisfied` mission has nothing to refine, and refining the
        # criteria of work that just met them is precisely the drift this
        # gate exists to prevent -- so only the two failure verdicts can
        # carry a proposal.
        if result in ("blocked", "failed") and await _propose_refinement(
            mission_id=mission_id,
            verdict=verdict,
            coordinator_session_id=coordinator_session_id,
            session_store=session_store,
            mission_store=mission_store,
        ):
            return
        await mission_store.set_status(mission_id, result)
        await session_store.clear_session_config_key(
            coordinator_session_id, "active_mission_id",
        )
        return
```

`record_evaluation` and the `mission.evaluation.end` emit both run before this branch and are unchanged, so the verdict is on the record whether or not it is held.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd /work/surogates && pytest tests/integration/missions/test_refinement.py tests/integration/missions/test_evaluator.py -v`
Expected: all pass. `test_evaluator.py` is included because Task 3 edits `apply_verdict`, which it covers heavily.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add surogates/missions/evaluator.py tests/integration/missions/test_refinement.py
git commit -m "feat(missions): hold a terminal verdict when the judge proposes a new rubric"
```

---

### Task 4: Parse `/mission accept` and `/mission reject`

**Files:**
- Modify: `surogates/missions/commands.py:31-33` (`MissionAction`), `:71` (`_CONTROL_VERBS`), `:138-167` (verb dispatch)
- Modify: `tests/missions/test_refinement_unit.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `parse_mission_command("accept")` returns `MissionCommand(action="accept")`; same for `"reject"`. Task 6 dispatches on `command.action`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/missions/test_refinement_unit.py`:

```python
def test_accept_and_reject_parse_as_control_verbs():
    from surogates.missions.commands import parse_mission_command

    assert parse_mission_command("accept").action == "accept"
    assert parse_mission_command("reject").action == "reject"


def test_accept_is_case_insensitive_like_the_other_verbs():
    from surogates.missions.commands import parse_mission_command

    assert parse_mission_command("ACCEPT").action == "accept"


def test_reject_captures_a_reason():
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("reject the new rubric drops the test gate")
    assert cmd.action == "reject"
    assert cmd.reason == "the new rubric drops the test gate"


def test_a_description_starting_with_the_word_accept_still_creates():
    """Control verbs are matched on the first token only; a mission whose
    description happens to begin with 'accepted' must not be swallowed."""
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command(
        "accepted-payments service needs a health check\n\nRubric:\n200 on /health",
    )
    assert cmd.action == "create"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /work/surogates && pytest tests/missions/test_refinement_unit.py -v -k "accept or reject"`
Expected: FAIL — `accept` falls through to the create path and raises `MissionCommandParseError: missing Rubric: block`.

- [ ] **Step 3: Extend the action type and the verb tuple**

In `surogates/missions/commands.py`, replace the `MissionAction` alias (:31-33):

```python
MissionAction = Literal[
    "create", "status", "pause", "resume", "cancel", "budget",
    "accept", "reject",
]
```

and `_CONTROL_VERBS` (:71):

```python
_CONTROL_VERBS = (
    "status", "pause", "resume", "cancel", "budget", "accept", "reject",
)
```

- [ ] **Step 4: Dispatch the two verbs**

In `parse_mission_command`, inside the `if verb in _CONTROL_VERBS:` block, after the `resume` branch (:152-153) and before the `budget` branch:

```python
        if verb in ("accept", "reject"):
            return MissionCommand(action=verb, reason=rest or None)
```

The existing `first_token, _, rest = text.partition(" ")` already guarantees first-token-only matching, so a description beginning `accepted-payments` is unaffected.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /work/surogates && pytest tests/missions/test_refinement_unit.py tests/missions/test_commands_parser.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/missions/commands.py tests/missions/test_refinement_unit.py
git commit -m "feat(missions): parse /mission accept and /mission reject"
```

---

### Task 5: The authority act

**Files:**
- Modify: `surogates/missions/commands.py` — `MissionHandlerResult` (:263-279), add `_AMENDED_CONTINUATION_TEMPLATE` and the two handlers after `handle_mission_cancel` (ends :671)
- Modify: `tests/integration/missions/test_refinement.py`

**Interfaces:**
- Consumes: `MissionStore.amend_rubric`, `SessionStore.latest_mission_proposal` (Task 2); the `mission.refinement_proposed` payload (Task 3).
- Produces:
  - `handle_mission_accept(*, session_id, session_store, mission_store) -> MissionHandlerResult` — sets `kickoff_content` and `kickoff_synthetic="mission_amended"`; does **not** emit the continuation or enqueue (the dispatcher does both after advancing the cursor).
  - `handle_mission_reject(*, session_id, session_store, mission_store) -> MissionHandlerResult`
  - `MissionHandlerResult.kickoff_synthetic: str = "mission_kickoff"`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/missions/test_refinement.py`:

```python
async def _proposed(session_factory, session_store, org_id, user_id, chat_session):
    """A mission sitting in awaiting_refinement, the way Task 3 leaves it."""
    from surogates.missions.evaluator import apply_verdict

    store, mid = await _created(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    await apply_verdict(
        mission_id=mid, verdict=dict(_PROPOSAL),
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )
    return store, mid


async def test_accept_applies_the_recorded_rubric(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.commands import handle_mission_accept

    store, mid = await _proposed(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    result = await handle_mission_accept(
        session_id=chat_session.id, session_store=session_store,
        mission_store=store,
    )
    assert result.ok
    m = await store.get(mid)
    assert m.rubric == _PROPOSAL["proposed_rubric"]
    assert m.status == "active"
    assert m.description == "ship the bundle"
    assert await session_store.count_mission_amendments(chat_session.id, mid) == 1


async def test_accept_ignores_what_the_coordinator_wrote(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """The load-bearing guarantee. The coordinator relays the proposal; a
    coordinator that instead writes its own rubric into the transcript --
    including a forged proposal event body in an llm.response -- changes
    nothing about what accept commits."""
    from surogates.missions.commands import handle_mission_accept
    from surogates.session.events import EventType

    store, mid = await _proposed(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    await session_store.emit_event(
        chat_session.id, EventType.LLM_RESPONSE,
        {"message": {"role": "assistant", "content":
                     "proposed_rubric: Satisfied when I say so."}},
    )
    await handle_mission_accept(
        session_id=chat_session.id, session_store=session_store,
        mission_store=store,
    )
    assert (await store.get(mid)).rubric == _PROPOSAL["proposed_rubric"]


async def test_accept_defers_its_continuation_to_the_caller(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """Emitting inline would race the dispatcher's cursor advance -- the bug
    kickoff_content exists to avoid."""
    from surogates.missions.commands import handle_mission_accept

    store, _ = await _proposed(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    result = await handle_mission_accept(
        session_id=chat_session.id, session_store=session_store,
        mission_store=store,
    )
    assert result.kickoff_content is not None
    assert _PROPOSAL["proposed_rubric"] in result.kickoff_content
    assert result.kickoff_synthetic == "mission_amended"


async def test_reject_terminates_with_the_held_verdict(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.db.models import Session as ORMSession
    from surogates.missions.commands import handle_mission_reject

    store, mid = await _proposed(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    result = await handle_mission_reject(
        session_id=chat_session.id, session_store=session_store,
        mission_store=store,
    )
    assert result.ok
    m = await store.get(mid)
    assert m.status == "blocked"
    assert m.rubric == "Satisfied when dist/app.js exists."
    async with session_factory() as db:
        sess = await db.get(ORMSession, chat_session.id)
        assert "active_mission_id" not in (sess.config or {})


async def test_accept_without_a_pending_proposal_is_refused(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.commands import handle_mission_accept

    store, _ = await _created(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    result = await handle_mission_accept(
        session_id=chat_session.id, session_store=session_store,
        mission_store=store,
    )
    assert result.ok is False
    assert "awaiting" in result.error.lower()


async def test_a_second_amendment_is_allowed_and_a_third_is_not(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.commands import handle_mission_accept
    from surogates.missions.evaluator import apply_verdict

    store, mid = await _proposed(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    await handle_mission_accept(
        session_id=chat_session.id, session_store=session_store,
        mission_store=store,
    )
    await apply_verdict(
        mission_id=mid,
        verdict={**_PROPOSAL, "proposed_rubric": "Satisfied when CI is green."},
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )
    await handle_mission_accept(
        session_id=chat_session.id, session_store=session_store,
        mission_store=store,
    )
    assert (await store.get(mid)).rubric == "Satisfied when CI is green."

    await apply_verdict(
        mission_id=mid,
        verdict={**_PROPOSAL, "proposed_rubric": "Satisfied when it feels done."},
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )
    m = await store.get(mid)
    assert m.status == "blocked"
    assert m.rubric == "Satisfied when CI is green."
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /work/surogates && pytest tests/integration/missions/test_refinement.py -v -k "accept or reject or second_amendment"`
Expected: FAIL — `ImportError: cannot import name 'handle_mission_accept'`.

- [ ] **Step 3: Add `kickoff_synthetic` to the result shape**

In `surogates/missions/commands.py`, add one field to `MissionHandlerResult` (after `kickoff_content`, :279):

```python
    kickoff_content: str | None = None
    # Label written into the deferred synthetic user.message's ``synthetic``
    # key. Defaults to the create path's value so existing callers are
    # unchanged; the amendment path overrides it so the two are
    # distinguishable in the event log and in ``count_synthetic_since``.
    kickoff_synthetic: str = "mission_kickoff"
```

- [ ] **Step 4: Add the continuation template**

After `_KICKOFF_TEMPLATE` in `surogates/missions/commands.py`:

```python
_AMENDED_CONTINUATION_TEMPLATE = """\
[Mission rubric amended by the user]

The rubric you were being graded against has been replaced. You are not
starting over -- everything already completed still counts.

Previous rubric:
{old_rubric}

New rubric:
{new_rubric}

Re-read the new rubric against the work already done, then continue. If the
completed work already satisfies it, say so with the evidence and mark
completion with [[mission-complete]] on its own line.
"""
```

- [ ] **Step 5: Add the two handlers**

In `surogates/missions/commands.py`, after `handle_mission_cancel` (ends :671):

```python
async def handle_mission_accept(
    *,
    session_id: UUID,
    session_store: Any,
    mission_store: MissionStore,
) -> MissionHandlerResult:
    """Apply the judge's proposed rubric. This is the authority act.

    The new rubric is read from the ``mission.refinement_proposed`` event,
    never from the command and never from anything the coordinator wrote --
    the same idiom as ``merge_experiment`` accepting no score argument. The
    coordinator relays the proposal to the user; what it types cannot change
    what this commits.

    The continuation is returned as ``kickoff_content`` rather than emitted
    here: the slash dispatcher advances the harness cursor past its own
    reply, so an inline emit would be skipped on the next wake.
    """
    active = await mission_store.get_active_for_session(session_id)
    if active is None or active.paused_reason != "awaiting_refinement":
        return MissionHandlerResult(
            ok=False,
            error="No rubric refinement is awaiting your decision.",
        )
    proposal = await session_store.latest_mission_proposal(session_id, active.id)
    new_rubric = str((proposal or {}).get("proposed_rubric") or "").strip()
    if not new_rubric:
        return MissionHandlerResult(
            ok=False, mission_id=active.id,
            error=(
                "The recorded proposal is missing or empty; use "
                "/mission reject or /mission cancel."
            ),
        )

    old_rubric = active.rubric
    await mission_store.amend_rubric(active.id, new_rubric=new_rubric)
    await session_store.emit_event(
        session_id, EventType.MISSION_AMENDED,
        {
            "mission_id": str(active.id),
            "old_rubric": old_rubric,
            "new_rubric": new_rubric,
            "evidence": str((proposal or {}).get("evidence") or ""),
        },
    )
    return MissionHandlerResult(
        ok=True, mission_id=active.id,
        message="Rubric amended; mission resumed.",
        kickoff_content=_AMENDED_CONTINUATION_TEMPLATE.format(
            old_rubric=old_rubric, new_rubric=new_rubric,
        ),
        kickoff_synthetic="mission_amended",
    )


async def handle_mission_reject(
    *,
    session_id: UUID,
    session_store: Any,
    mission_store: MissionStore,
) -> MissionHandlerResult:
    """Decline the proposal; the mission ends as the judge originally ruled.

    ``held_verdict`` comes off the proposal event rather than from a fresh
    judge call: the user is declining *this* proposal against *that* ruling,
    and a second call could return something else.

    ``paused_reason`` is left as ``awaiting_refinement`` on the terminal row
    -- a breadcrumb that this mission ended after a refinement was declined.
    ``get_active_for_session`` excludes terminal statuses, so it cannot be
    mistaken for a live proposal.
    """
    active = await mission_store.get_active_for_session(session_id)
    if active is None or active.paused_reason != "awaiting_refinement":
        return MissionHandlerResult(
            ok=False,
            error="No rubric refinement is awaiting your decision.",
        )
    proposal = await session_store.latest_mission_proposal(session_id, active.id)
    held = str((proposal or {}).get("held_verdict") or "blocked")
    if held not in ("blocked", "failed"):
        held = "blocked"

    await mission_store.set_status(active.id, held)
    await session_store.clear_session_config_key(session_id, "active_mission_id")
    await session_store.emit_event(
        session_id, EventType.MISSION_EVALUATION_END,
        {
            "mission_id": str(active.id),
            "trigger": "refinement_rejected",
            "result": held,
            "explanation": str((proposal or {}).get("explanation") or ""),
            "feedback": "",
        },
    )
    return MissionHandlerResult(
        ok=True, mission_id=active.id,
        message=f"Refinement declined; mission {held}.",
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd /work/surogates && pytest tests/integration/missions/test_refinement.py -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add surogates/missions/commands.py tests/integration/missions/test_refinement.py
git commit -m "feat(missions): add /mission accept and reject handlers"
```

---

### Task 6: Wire the verbs into the harness dispatcher

**Files:**
- Modify: `surogates/harness/loop_outcome_commands.py:113-123` (imports), `:238-269` (dispatch branches + usage string), `:286-297` (deferred emit label)
- Modify: `tests/integration/missions/test_refinement.py`

**Interfaces:**
- Consumes: `handle_mission_accept`, `handle_mission_reject`, `MissionHandlerResult.kickoff_synthetic` (Task 5).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/missions/test_refinement.py`:

```python
async def test_the_usage_string_lists_the_new_verbs(
    session_factory, org_id, user_id, chat_session,
):
    """A user who mistypes the command has to be able to find accept/reject."""
    import inspect

    from surogates.harness import loop_outcome_commands

    source = inspect.getsource(loop_outcome_commands)
    usage_start = source.index("Usage: /mission")
    usage = source[usage_start:usage_start + 400]
    assert "/mission accept" in usage
    assert "/mission reject" in usage
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /work/surogates && pytest tests/integration/missions/test_refinement.py -v -k usage_string`
Expected: FAIL on `assert "/mission accept" in usage`.

- [ ] **Step 3: Import the two handlers**

In `surogates/harness/loop_outcome_commands.py`, add to the import block at `:113-123` (keep it alphabetical):

```python
        from surogates.missions.commands import (
            MissionCommandParseError,
            MissionHandlerResult,
            handle_mission_accept,
            handle_mission_cancel,
            handle_mission_create,
            handle_mission_pause,
            handle_mission_reject,
            handle_mission_resume,
            handle_mission_budget,
            handle_mission_status,
            parse_mission_command,
        )
```

- [ ] **Step 4: Add the two dispatch branches**

In `_handle_mission_command`, after the `elif command.action == "cancel":` block (ends :263) and before the trailing `else:`:

```python
                elif command.action == "accept":
                    result = await handle_mission_accept(
                        session_id=session.id,
                        session_store=self._store,
                        mission_store=mission_store,
                    )
                    message = result.message or result.error
                elif command.action == "reject":
                    result = await handle_mission_reject(
                        session_id=session.id,
                        session_store=self._store,
                        mission_store=mission_store,
                    )
                    message = result.message or result.error
                    if result.ok:
                        # Mirror the DB clear_session_config_key call in the
                        # in-memory session, the way the cancel branch does.
                        cfg = dict(session.config or {})
                        cfg.pop("active_mission_id", None)
                        session.config = cfg
```

Neither branch needs Redis: `accept` returns `kickoff_content`, and the existing deferred-emit block enqueues the session itself; `reject` is terminal and wakes nobody.

- [ ] **Step 5: Extend the usage string**

Replace the `else:` branch's message (:264-269):

```python
                else:
                    message = (
                        "Usage: /mission <description>\\n\\nRubric:\\n<criterion>"
                        " | /mission status | /mission pause [reason]"
                        " | /mission resume | /mission cancel [--cascade] [reason]"
                        " | /mission accept | /mission reject [reason]"
                    )
```

- [ ] **Step 6: Honour the synthetic label on the deferred emit**

In the deferred-emit block (:291-297), replace the hardcoded label:

```python
            await self._store.emit_event(
                session.id, EventType.USER_MESSAGE,
                {
                    "content": result.kickoff_content,
                    "synthetic": result.kickoff_synthetic,
                },
            )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd /work/surogates && pytest tests/integration/missions/test_refinement.py tests/integration/missions/test_commands.py -v`
Expected: all pass. `test_commands.py` is included because it exercises the dispatcher's other branches.

- [ ] **Step 8: Commit**

```bash
cd /work/surogates
git add surogates/harness/loop_outcome_commands.py tests/integration/missions/test_refinement.py
git commit -m "feat(missions): dispatch /mission accept and reject in the harness"
```

---

### Task 7: Documentation and full-suite verification

**Files:**
- Modify: `docs/tasks/index.md` (missions section)
- Verify: whole mission suite

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Find the mission command reference**

Run: `cd /work/surogates && grep -n "/mission cancel\|/mission resume" docs/tasks/index.md docs/commands/index.md`
Note every place the control verbs are listed — each one needs `accept` and `reject` added.

- [ ] **Step 2: Document the two verbs**

Add to each list found in Step 1, matching the surrounding table or bullet format. Where the docs describe mission status transitions, add this paragraph:

```markdown
### Rubric refinement

When the evaluator is about to end a mission `blocked` or `failed` because
the **rubric itself** is unreachable as written — it names an artifact the
work has shown cannot exist, or contradicts itself — it may propose a
replacement rubric instead. The mission pauses with
`paused_reason=awaiting_refinement`, the coordinator relays the proposal, and
the mission waits: no judge calls run while it waits.

- `/mission accept` — apply the proposed rubric and resume. The rubric
  applied is the one recorded in the `mission.refinement_proposed` event;
  the command takes no rubric text, so nothing the coordinator wrote can
  change what is committed.
- `/mission reject [reason]` — decline; the mission ends with the verdict
  that was held back.

The mission's `description` is never amended — it is the standing intent —
and `iteration` is not reset, so a pivot does not refill the allowance
(`/mission budget` does that). A mission accepts at most two amendments.
```

- [ ] **Step 3: Run the whole mission suite**

Run: `cd /work/surogates && pytest tests/missions/ tests/integration/missions/ -v`
Expected: all pass, no skips other than pre-existing ones.

- [ ] **Step 4: Confirm no migration was introduced**

Run: `cd /work/surogates && git diff master --stat -- surogates/db/`
Expected: empty output. Any change under `surogates/db/` means a column or model was touched, which this design forbids — stop and re-read the Global Constraints.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add docs/tasks/index.md
git commit -m "docs(missions): document /mission accept and reject"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| `_MissionVerdict` gains two optional fields | 1 |
| `_SYSTEM_PROMPT` proposal paragraph, blocked/failed only | 1 |
| `_render_history` stagnation hint; `is_stagnant()`'s first caller | 1 |
| `mission.refinement_proposed` payload incl. `mission_id`, `held_verdict` | 2, 3 |
| Event read by `(session_id, type)` + JSONB `mission_id` match | 2 |
| `paused` + `paused_reason="awaiting_refinement"`, no new status | 3 |
| `active_mission_id` not cleared on the refinement path | 3 |
| Synthetic relay message, verbatim instruction | 3 |
| No proposal on `satisfied` | 3 |
| `MAX_MISSION_AMENDMENTS = 2`, counted from applied amendments | 2, 3, 5 |
| `accept` reads the stored text, takes no rubric argument | 5 |
| `accept`: rubric replaced, stagnation zeroed, `iteration` untouched | 2, 5 |
| `reject` terminates with the recorded `held_verdict` | 5 |
| No migration, no new `MissionStatus` | Constraint + Task 7 Step 4 |
| `description` never mutated | 2, 5 (asserted in tests) |

Two spec details are implemented slightly beyond the letter and are called out in the code comments: the no-op guard (a proposal identical to the standing rubric is not raised, so the user is never asked to approve nothing), and `reject` leaving `paused_reason` on the terminal row as a breadcrumb.

**Placeholder scan:** none — every step carries the literal code or the exact command.

**Type consistency:** `handle_mission_accept` / `handle_mission_reject` take the same three keyword arguments in Tasks 5 and 6. `latest_mission_proposal` and `count_mission_amendments` take `(session_id, mission_id)` positionally in Tasks 2, 3 and 5. `amend_rubric` is keyword-only on `new_rubric` in Tasks 2 and 5. `kickoff_synthetic` is defined in Task 5 and read in Task 6.
