# Evidence-gated objective refinement

## Problem

A mission's `description` and `rubric` are written once and never change.
`MissionStore` (`surogates/missions/store.py`) exposes `set_status`,
`record_evaluation`, `increment_iteration`, `record_parse_failure`,
`set_budget` — no mutator for either field. `_CONTROL_VERBS`
(`surogates/missions/commands.py:71`) is `status | pause | resume | cancel |
budget`. There is no path, for any principal, to amend a live mission.

So a mission whose rubric was written wrong has exactly three exits, all of
them losses:

1. The judge returns `blocked` — "cannot be progressed without external
   input" — and `apply_verdict` sets the terminal status and clears
   `active_mission_id`. The work is stranded.
2. The judge returns `failed` — its own prompt names "contradictory rubric"
   as a trigger — and the mission dies for a defect in its acceptance
   criteria rather than in its work.
3. Neither fires, and the mission grinds through `max_iterations`
   `needs_revision` rounds against a target it cannot hit, at full judge and
   coordinator cost, before terminating anyway.

`stagnant_evaluations` already counts non-`satisfied` verdicts in a row and
`is_stagnant()` (store.py:266) already reads it against
`STAGNANT_EVALUATION_LIMIT`. **`is_stagnant()` has no caller.** The signal
that a mission is circling is computed and discarded.

The thing being avoided is real: a mission that can rewrite its own
acceptance criteria will rewrite them to something it has already achieved.
That is why the field is immutable today. The design below is about
separating a *pivot* — the criteria were wrong, here is the evidence — from
*drift*, without giving the coordinator authorship of its own goalposts.

## Design

Three parties, each holding exactly one thing:

| Party | Holds | Cannot |
|---|---|---|
| Judge | authorship of the proposed rubric | apply it |
| Coordinator | relay of the proposal to the user | author or apply it |
| User | authority to apply | author it |

`description` is never mutable. It is the standing intent: what the user
asked for. Only `rubric` — the verification criteria — is amendable, and only
to text the judge wrote.

### Proposal

`_MissionVerdict` (`surogates/harness/loop_mission_evaluator.py:238`) gains
two optional fields:

```python
proposed_rubric: str = ""       # only on blocked / failed
refinement_evidence: str = ""   # what in the workstream shows the rubric is wrong
```

Both default empty, so an existing judge response still validates and every
current call site is unaffected. `outlines`-backed structured generation and
the tolerant fallback parser both pick the fields up from the one schema.

`_SYSTEM_PROMPT` (`surogates/missions/evaluator.py:163`) gains a paragraph:
fill these **only** when returning `blocked` or `failed`, and only when the
completed-task evidence shows the rubric is unreachable *as written* rather
than merely unmet. A rubric the work has not yet satisfied is
`needs_revision`; a rubric the work cannot satisfy is a proposal.

`_render_history` (evaluator.py:293) gains one sentence, rendered when
`is_stagnant()` is true: *if the same blocker has persisted for N rounds,
consider whether the rubric itself is the defect, not the work.* This is
`is_stagnant()`'s first caller. It is a prompt hint, not a gate — stagnation
alone never proposes anything.

### Instead of terminating

`apply_verdict` (evaluator.py:352) currently does:

```python
if result in ("satisfied", "blocked", "failed"):
    await mission_store.set_status(mission_id, result)
    await session_store.clear_session_config_key(..., "active_mission_id")
    return
```

That branch splits. The refinement path is taken when **all** hold:

- `result` is `blocked` or `failed` (never `satisfied` — a satisfied mission
  has nothing to refine, and allowing it there is precisely the drift case)
- `proposed_rubric` is non-empty after stripping
- fewer than `MAX_MISSION_AMENDMENTS` (2) `mission.amended` events exist for
  this mission

Then, in place of termination:

- emit `mission.refinement_proposed` with `{mission_id, old_rubric,
  proposed_rubric, evidence, held_verdict, explanation}`
- `set_status("paused", paused_reason="awaiting_refinement")`
- **do not** clear `active_mission_id`
- emit a synthetic `USER_MESSAGE` (the mechanism `mission.continuation`
  already uses) instructing the coordinator to relay the proposal verbatim
  and end its turn

`held_verdict` is what the mission terminates as if the user rejects. It is
recorded at proposal time so a rejection cannot be resolved by a second judge
call whose verdict may have drifted.

Nothing new is stored on the `missions` row. `events` is already the system
of record, `events.type` is plain text with no CHECK constraint, and
`idx_events_session_type` serves the read. No migration, and no new mission
status — reusing `paused` + `paused_reason` follows the precedent
`pause_if_budget_exhausted` set explicitly (store.py:319) to keep a new
status out of the SDK's `types.ts` and its rebuilt dist.

A paused mission fails `should_evaluate`'s `mission.status != "active"` check
(evaluator.py:126), so a mission awaiting authorization burns no judge calls
while it waits. `get_active_for_session` includes `paused`, so the mission is
still the session's active one and `/mission status` still finds it.

### Authority

Two verbs join `_CONTROL_VERBS`: `accept` and `reject`.

`/mission accept` reads the **latest `mission.refinement_proposed` event for
the mission** and writes *that* `proposed_rubric` into the row. It takes no
rubric argument.

Events are keyed by session, not by mission, so the read is
`session_id = <coordinator session> AND type = 'mission.refinement_proposed'`
ordered by `id DESC`, taking the first row whose `data->>'mission_id'`
matches — served by `idx_events_session_type`. Both events carry
`mission_id` for that reason, and the `mission.amended` count that enforces
the cap uses the same shape. A session holds at most one active-or-paused
mission (`ActiveMissionConflictError`), so the match is a guard against
proposals left behind by an earlier terminated mission on the same session,
not a fan-out. This is the idiom `merge_experiment` already establishes for
research missions — the tool that commits a change accepts no value for the
thing being committed, so the value cannot be supplied by the party being
gated. Whatever prose the coordinator put on screen is display only; if it
paraphrased, the event holds the verbatim text, the applied rubric is the
judge's, and the divergence is auditable.

On accept: `rubric := proposed_rubric`, `stagnant_evaluations := 0`,
`status := "active"`, `paused_reason := NULL`, emit `mission.amended` with
`{mission_id, old_rubric, new_rubric, evidence}`, then inject a continuation naming both
the old and the new rubric so the coordinator's next turn knows what changed
and why it is not starting over.

`iteration` is deliberately **not** reset. The amendment changes the target,
not the allowance; a mission that has burned 18 of 20 iterations getting the
target wrong does not get 20 more for free. `/mission budget` and a manual
resume already exist for the case where the user wants to fund the pivot.

`/mission reject` sets the `held_verdict` from the proposal event, clears
`active_mission_id`, and terminates exactly as the un-split branch would
have.

A proposal left unanswered leaves the mission paused. That is an existing,
visible, resumable state, not a leak.

### Bound

`MAX_MISSION_AMENDMENTS = 2`, counted from `mission.amended` events. Past the
cap, `blocked` is `blocked` and `failed` is `failed`.

Without a cap the mechanism inverts: each rejection-free amendment moves the
criteria toward what the work already produced, and a long-running mission
negotiates its rubric down to something trivially satisfiable. The cap is on
*applied* amendments, not proposals, so a user who rejects twice has not
spent the budget.

## Out of scope

- An `objective` column (Argus's mutable `o_t`). `description` stays frozen
  and stays the only objective field. Adding one means a migration plus an
  SDK `types.ts` change and a dist rebuild — a known release-breaker — to buy
  a case an amendable rubric already covers.
- Extending `max_iterations` or `budget_tokens` as part of the authorized
  act. `/mission budget` already exists.
- Auto-expiry of a stale proposal. Add when a paused-awaiting mission is
  observed sitting long enough to matter.
- Refinement on `max_iterations_reached`. There is no judge call in hand on
  that path, so proposing would mean spending one specifically to negotiate
  the ceiling. Add when a real mission burns out mid-pivot.
- Any change to `/goal` (`surogates/harness/outcomes.py`), which has its own
  state machine and no task-evidence layer to gate on.
- Research missions. `adjust_research_verdict` already gates their terminal
  verdicts on machine-written scores; a refinable rubric there would need to
  reason about `eval_cmd_test`, which is a separate design.

## Tests

`tests/missions/test_refinement.py`, assert-based:

- A `blocked` verdict carrying `proposed_rubric` leaves the mission `paused`
  with `paused_reason="awaiting_refinement"`, emits
  `mission.refinement_proposed`, and does **not** clear `active_mission_id`.
- A `blocked` verdict with no `proposed_rubric` terminates as it does today —
  the un-split branch is unchanged.
- A `satisfied` verdict carrying `proposed_rubric` terminates as satisfied.
  The field is ignored outside `blocked`/`failed`.
- `accept` applies the rubric from the event, not any string reachable from
  the command or the coordinator's last response.
- `accept` sets `active`, zeroes `stagnant_evaluations`, and leaves
  `iteration` where it was.
- `reject` terminates with the `held_verdict` recorded at proposal time, not
  a freshly computed one.
- The third proposal on a mission with two `mission.amended` events
  terminates instead of pausing.
- `should_evaluate` returns `should=False` for a mission in
  `awaiting_refinement`.
