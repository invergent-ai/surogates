# 8. Commands

Commands are user messages that start with `/`. The harness handles builtin
commands before calling the model. If the command is not builtin, the harness
tries to resolve it as a skill name and expands that skill into the model
context.

Commands are not tools. Tools are model-invoked capabilities; commands are
user-invoked shortcuts that shape the next harness turn.

## Command Resolution

When the latest user message starts with `/`, Surogates resolves it in this
order:

1. Builtin command handlers: `/clear`, `/compress`, `/goal`, `/loop`.
2. Dynamic skill command: `/<skill-name> [args...]`.
3. Plain user message if no builtin or skill matches.

Builtin command names are reserved and are never treated as skill names.

## Builtin Commands

### `/clear`

Clears the conversation context for the current session.

What it does:

- Destroys the session sandbox if one exists.
- Emits a `context.compact` event with an empty compacted message list.
- Emits an assistant confirmation: `Conversation cleared.`
- Leaves the durable event log intact; replay uses the compacted context from
  that point forward.

Use this when the current session context is no longer useful and the user wants
to continue with a clean slate in the same session.

### `/compress`

Forces context compression for the current session.

What it does:

- Removes the `/compress` user message from the model-visible conversation.
- Runs the configured context compressor even if the automatic threshold has not
  been reached.
- Emits a `context.compact` event and an assistant message describing the
  compression result.
- Does not call the main chat model for a normal response.

Use this when a long session should be compacted before continuing.

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

For a task-oriented walkthrough, see the
[Goals Quick Start](../goals/index.md).

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

- One outcome is active per session. While status is `active`,
  `/goal <new text>` is rejected — pause or clear the existing outcome
  first.
- Default iteration budget is `outcomes.max_iterations` (`20`, capped at
  `20`).
- Evaluator lifecycle events are emitted as `span.outcome_evaluation_start`,
  `span.outcome_evaluation_ongoing`, and `span.outcome_evaluation_end`.
- The evaluator returns one of `satisfied`, `needs_revision`, `blocked`,
  or `failed`. `satisfied` and `failed` end the loop; `blocked` ends it
  with an explanation of what user action is required to unblock;
  `needs_revision` queues a continuation.
- Continuations are normal `user.message` events marked with
  `synthetic: outcome_continuation`.
- User messages can steer the work while the outcome is active; evaluation
  resumes after the user-directed turn.
- Evaluator failures fail open as `needs_revision`; repeated unparseable
  evaluator output pauses the outcome.

### `/loop [interval] <prompt>`

Schedules a user-owned loop. The scheduled prompt becomes a fresh
`channel="scheduled"` session each time it fires.

Examples:

```text
/loop 5m /babysit-prs
/loop every 1 minute get bitcoin price
/loop check deploys every 20m
/loop check queue health
```

Parsing rules:

- Leading interval: `/loop 5m check deploys` uses `5m`.
- Leading `every` clause: `/loop every 1 minute get bitcoin price` uses `1m`.
- Trailing `every` clause: `/loop check deploys every 20m` uses `20m`.
- No interval: `/loop check queue health` creates a dynamic loop. The agent
  picks the next delay after each run with `loop_wait`.

Supported interval units:

| Unit | Meaning | Notes |
|---|---|---|
| `s` | seconds | Rounded up to whole minutes |
| `m` | minutes | Clean minute steps map to `*/N * * * *`; uneven values round to a clean cadence |
| `h` | hours | `N <= 23` maps to `0 */N * * *`; larger values round up to days |
| `d` | days | Maps to `0 0 */N * *` |

Cron step syntax only represents minute intervals that divide evenly into an
hour. Requests such as `7m` are rounded to the nearest clean cadence, and the
confirmation tells the user what Surogates picked. Requests such as `90m` round
up to hours.

Loop behavior:

- Requires an authenticated user. Anonymous and service-account sessions cannot
  create user-owned loops.
- Stores schedules in PostgreSQL scoped by `org_id`, `user_id`, and `agent_id`.
- Runs from the owning agent worker, which claims due rows with
  `FOR UPDATE SKIP LOCKED`.
- Fixed-interval loops auto-expire after 3 days. A run can end the schedule
  early by calling `loop_complete` when the prompt's stop condition is met.
- Dynamic loops auto-expire after 7 days. Each run must call `loop_wait` with a
  next delay between 1 minute and 1 hour; if it does not, Surogates applies a
  10-minute fallback delay. Setting `completed: true` on `loop_wait` ends the
  schedule early.
- Accepts slash commands as prompts, so `/loop 5m /some-skill args` works.
- Scans prompts before persistence for prompt-injection markers, invisible
  Unicode, secret-exfiltration patterns, and destructive command patterns.

### `/loop list`

Lists active loops for the current user and agent.

### `/loop cancel <id>`

Cancels a loop by scheduled session ID.

## Dynamic Skill Commands

Any non-builtin slash command can invoke a skill:

```text
/research vector databases
/babysit-prs
```

Resolution rules:

- The command name must match an available skill name.
- The harness calls `skill_view` server-side.
- The returned skill content is inlined into the model-visible user message.
- The original slash command remains in the event log.
- If the skill has supporting assets, scripts, templates, or references, normal
  skill staging still applies.
- If no skill matches, the original message reaches the model unchanged.

Dynamic skill commands are loaded through the normal skill layering rules:
platform, organization, then user.

## Channel Notes

These commands are Surogates chat commands. Channel-native commands, such as a
Slack app slash command like `/surogates <message>`, are transport entry points:
the channel adapter turns them into regular Surogates messages before the
harness sees them.
