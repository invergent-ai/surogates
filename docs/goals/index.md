# Goals Quick Start

Goals let you define an outcome once and have Surogates keep working until a
separate evaluator decides the outcome is satisfied, the iteration budget is
used, or you pause/clear the goal.

Use goals when the success condition matters more than a single reply:

- Fix all failing tests and report the passing command.
- Build a file or artifact that meets specific criteria.
- Research a question until the final answer covers required points.
- Continue a long task automatically after intermediate attempts fall short.

## Start From Chat

In any session, send `/goal` followed by the outcome:

```text
/goal Fix every failing test in tests/ and report the command that passes
```

Surogates will:

1. Save the goal in the session.
2. Add a synthetic kickoff message with the goal text.
3. Run the normal agent loop.
4. Ask the base LLM, in a separate evaluator context, whether the latest final
   response satisfies the goal.
5. Continue automatically when the evaluator returns `needs_revision`.

## Add A Rubric

Use a rubric when the goal has concrete acceptance criteria:

```text
/goal Build a DCF model for Costco

Rubric:
- Produces an .xlsx file
- Uses five years of historical revenue
- Includes WACC and terminal value assumptions
- Includes a sensitivity analysis
- Final response names the generated file
```

Write rubrics as observable outcomes. Prefer "the final response includes the
passing pytest command" over "try hard to test everything".

## Check Or Control A Goal

| Command | Behavior |
|---|---|
| `/goal` | Show the current goal, status, iteration count, and last evaluator result |
| `/goal status` | Same as `/goal` |
| `/goal pause` | Stop automatic continuation but keep the saved goal |
| `/goal resume` | Resume a paused goal |
| `/goal clear` | Remove the current goal from the session |

Only one goal is active per session. Setting a new goal replaces the previous
goal state.

## Start From The API

Programmatic clients should use `user.define_outcome` instead of sending a
chat slash command:

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

Do not also send a separate user message. The event handler persists the goal,
adds the synthetic kickoff message, and wakes the session.

## What To Watch In Events

Goal runs are normal session activity plus a few goal-specific events:

| Event | Meaning |
|---|---|
| `outcome.defined` | A goal was saved for the session |
| `span.outcome_evaluation_start` | The evaluator started grading a final response |
| `span.outcome_evaluation_ongoing` | The evaluator is active |
| `span.outcome_evaluation_end` | The evaluator returned `satisfied`, `needs_revision`, or `failed` |
| `outcome.continuation` | Surogates queued another attempt |
| `user.message` with `synthetic: outcome_continuation` | The next model-visible continuation prompt |
| `outcome.paused` | The goal was paused by the user or by evaluator parse failures |
| `outcome.cleared` | The goal state was removed |

The harness cursor stops before synthetic continuation messages so they remain
pending for the next wake.

## Iteration Budget

The default budget is `outcomes.max_iterations`, which defaults to `3` and is
capped at `20`. When the budget is reached, the goal status becomes
`max_iterations_reached`. Use `/goal resume` to continue from that state, or
`/goal clear` to stop tracking it.

## Tips

- Put completion evidence in the rubric: test commands, file names, expected
  sections, metrics, or explicit blockers.
- Ask for verification in the goal when the work is code or data dependent.
- Keep the goal stable while it is running; send normal messages only when you
  want to steer the current attempt.
- Use `/goal pause` before taking over manually, then `/goal resume` when the
  agent should continue.

See [Commands](../commands/index.md) for the slash-command reference and
[REST API Reference](../appendices/api-reference.md#post-v1sessionsidevents)
for the event schema.
