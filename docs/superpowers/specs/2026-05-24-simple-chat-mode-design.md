# Simple Chat Mode

Replace the per-tool / per-reasoning timeline in `agent-chat-react` with
a Simple mode that groups each LLM iteration under a one-line, model-
generated summary and presents a per-turn recap with artifact links.
Today's detailed view is preserved as Expert mode behind a toggle.

The harness generates the summaries using its existing
`summary_model` auxiliary LLM and emits two new persisted events.
The SDK groups the existing event stream by iteration and renders the
new summaries.

## Goals

- A regular user sees an assistant turn as a short list of one-line
  steps ("Rework hero paragraph to introduce brain/hands metaphor")
  rather than every read/write/patch call.
- A reasoning block in Simple mode shows a one-line summary of what
  the model thought about, not "Thought for 32 seconds".
- After each assistant turn, a summary card lists the artifacts that
  came out of the turn (edited files, created artifacts, fetched
  URLs) with click-through to existing viewers.
- Power users keep today's view via a toggle in the composer's tools
  row. Default is Simple.
- Session replay produces the same Simple-mode layout as the live
  session — summaries are persisted, not regenerated on render.

## Non-goals

- Replacing the existing per-tool renderers
  (`components/chat/tools/*.tsx`). They are reused inside an expanded
  iteration row and unchanged in Expert mode.
- Reworking the browser-activity or web-search sub-grouping. Both
  continue to apply *inside* an expanded iteration in Simple mode.
- A second summarization for failed or cancelled turns. No
  `turn.summary` is emitted in those cases; the iteration that
  errored stays expanded permanently.
- Cross-turn or cross-session recap. The card is per assistant turn.

## Architecture overview

```
┌─────────────────────────────────────────────────────────────────┐
│ HARNESS (/work/surogates)                                       │
│                                                                 │
│  Iteration ends      ┌───────────────────────┐                  │
│  (reasoning+tools) ─▶│ turn_summarizer.py    │                  │
│                      │   uses summary_model  │                  │
│  Turn ends         ─▶│   (auxiliary LLM)     │                  │
│                      └───────────┬───────────┘                  │
│                                  │                              │
│                       emits new events                          │
│                                  │                              │
│                                  ▼                              │
│        ┌──────────────────────────────────────────┐             │
│        │ event stream (existing, persisted in DB) │             │
│        │   ... llm.response / tool.call ...       │             │
│        │   + iteration.summary  (NEW)             │             │
│        │   + turn.summary       (NEW)             │             │
│        └──────────────────────┬───────────────────┘             │
└───────────────────────────────┼─────────────────────────────────┘
                                │ WebSocket / replay
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│ SDK (@invergent/agent-chat-react)                               │
│                                                                 │
│   runtime/reducer.ts  ─── attaches new summaries to messages    │
│                                                                 │
│   chat-thread.tsx                                               │
│     ├─ viewMode toggle (simple | expert) in composer row        │
│     │                                                           │
│     ├─ simple: one timeline row per LLM iteration               │
│     │         (summary line + expand to see contents)           │
│     │         + TurnSummaryCard at end of each turn             │
│     │                                                           │
│     └─ expert: today's per-tool / per-reasoning timeline        │
└─────────────────────────────────────────────────────────────────┘
```

Two coordinated changes:

1. **Harness** — new `turn_summarizer.py` module plus two new event
   types persisted in the existing events table. Summarization runs
   concurrently with the next iteration; failures are silent.
2. **SDK** — new view mode state, reducer handlers for the two new
   events, an `IterationGroup` component, a `TurnSummaryCard`
   component, and a toggle in the composer's tools row.

## Harness changes

### New module: `surogates/harness/turn_summarizer.py`

```python
class TurnSummarizer:
    """
    Generates one-line iteration summaries and end-of-turn recaps
    using the existing summary_model auxiliary LLM.

    Results are persisted in the events table so session replay
    reuses them without re-summarising.
    """

    def __init__(self, summary_client, summary_model: str): ...

    async def summarize_iteration(
        self,
        iteration_id: str,
        reasoning: str,
        tool_calls: list[ToolCall],
        prior_iteration_summaries: list[str],
    ) -> str:
        """
        Imperative one-liner ("Rework hero paragraph to introduce
        brain/hands metaphor"). Prior summaries supplied so successive
        iterations don't repeat themselves.
        """

    async def summarize_turn(
        self,
        turn_id: str,
        user_message: str,
        iteration_summaries: list[str],
        artifacts: list[TurnArtifact],
    ) -> TurnSummary:
        """
        1–3 sentence recap plus a curated artifact list. The summariser
        LLM decides which tool calls are "notable" — read_file and
        list_files do not become artifacts; write_file, patch,
        create_artifact, web_extract, and notable terminal commands
        do.
        """
```

`TurnArtifact` shape mirrors the SDK type below.

### New event types

| Event              | When emitted                                                                  | Fields                                                                                                            |
|--------------------|-------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------|
| `iteration.summary`| After each LLM iteration (reasoning + that iteration's tool batch) completes  | `turn_id`, `iteration_index`, `summary`, `tool_call_ids[]`, `started_at`, `ended_at`                              |
| `turn.summary`     | After the final iteration of an assistant turn that completed without error   | `turn_id`, `recap`, `artifacts: [{kind, label, ref, meta?}]`                                                      |

Both events are persisted in the events table alongside everything
else. SSE/WebSocket delivery uses the existing notifier. No new
database tables.

### Correlation: matching summary events to assistant messages

The SDK reconstructs assistant messages on the fly from
`llm.delta` / `llm.response` events; it currently has no `turn_id` or
`iteration_index` on `AgentChatMessage`. To make the new summary
events resolvable client-side without reverse-engineering turn
boundaries, the harness threads two identifiers through the existing
event stream:

1. Every `llm.delta` and `llm.response` event in an assistant turn
   gains two new fields in `event.data`:
   - `turn_id: str` — stable across all iterations in the turn.
   - `iteration_index: int` — 0-based, monotonically increasing within
     the turn.
2. The SDK reducer (in `applyLlmDelta` / `applyLlmResponse`) reads
   both fields and stamps them on the resulting `AgentChatMessage`
   (new fields `turnId` and `iterationIndex` on
   `AgentChatMessage`).
3. `iteration.summary` matches the assistant message by
   `(turn_id, iteration_index)`.
4. `turn.summary` matches by `turn_id` and attaches to the **last**
   assistant message with that `turnId` (the tail iteration of the
   turn).

Multiple `llm.response` events can land on the same
`AgentChatMessage` (the existing `matchesExistingToolTurn`
rehydration path); the second event's `iteration_index` overwrites
the first. That is fine: by the time `iteration.summary` arrives, the
message's `iterationIndex` is the final value for that iteration's
`llm.response`.

### Hook points in the orchestrator

In the worker loop (`surogates/orchestrator/worker.py`):

- After each LLM iteration completes (after all tool results for that
  iteration land, just before the next iteration starts — or before
  turn end for the final iteration): kick off
  `TurnSummarizer.summarize_iteration` as a background task. When it
  resolves, emit `iteration.summary`.
- After the final iteration of an assistant turn completes
  successfully: kick off `TurnSummarizer.summarize_turn` as a
  background task. When it resolves, emit `turn.summary`.

Summarisation **never blocks the main loop**. The next iteration
starts immediately. The summary event may arrive after the next
iteration has already streamed its first tokens — the SDK reducer is
keyed by `iteration_index`, so out-of-order arrival is fine.

### Failure handling

- Summariser throws or times out (10s soft cap per call) → no event
  emitted. Log a warning. SDK falls back to its live-state rendering
  permanently for that iteration / no `TurnSummaryCard` for that
  turn.
- No retries. Summaries are nice-to-have, not load-bearing.

### Backwards compat

- New behaviour gated behind a setting:
  `surogates.config.harness.emit_turn_summaries: bool = True`.
- Older SDK versions ignore unknown event types — no breakage.
- Newer SDK gracefully degrades if `emit_turn_summaries` is off or if
  events fail to arrive.

### Cost

Each iteration adds one extra `summary_model` call (typically 1–4 per
turn) plus one `turn.summary` call per turn. The summary model is the
cheap auxiliary already used for context compression and title
generation. No gating by client mode — even Expert-mode users benefit
from the cached summaries when they later switch to Simple, and per-
client gating would force the server to re-summarise on toggle.

### Artifact filtering

The summariser LLM decides which tool calls become artifacts. Allowlist
nudge in the prompt:

- `write_file`, `patch`, `create_artifact` → always notable.
- `web_extract`, `web_crawl` → notable when content was returned.
- `terminal` → notable when the command produced output the user is
  likely to care about (the LLM judges).
- `read_file`, `list_files`, `search_files`, `session_search` → never
  notable.

## SDK changes

### View mode state

`viewMode: "simple" | "expert"` lives on the chat runtime store
(`runtime/use-agent-chat-runtime.ts`). Default is `"simple"`.

Persisted per user via two new optional adapter methods:

```ts
// adapter-context.tsx
interface AgentChatAdapter {
  // ...existing...
  getChatViewMode?(): Promise<"simple" | "expert" | null>;
  setChatViewMode?(mode: "simple" | "expert"): Promise<void>;
}
```

If the adapter doesn't implement them, the SDK falls back to
`localStorage` under the key
`@invergent/agent-chat-react:viewMode`. Toggle is the segmented
control already used elsewhere in the composer's tools row.

### Toggle placement

Inside the composer's tools row (`components/chat/chat-composer.tsx`)
alongside the existing browser/workspace toggle buttons. A two-segment
control labeled `Simple` / `Expert`. Always visible; not gated on
session state.

### Types (`src/types.ts`)

```ts
export interface IterationSummary {
  iterationIndex: number;
  summary: string;
  toolCallIds: string[];
  startedAt: string;
  endedAt: string;
}

export type TurnArtifactKind = "file" | "artifact" | "url" | "command";

export interface TurnArtifactRef {
  kind: TurnArtifactKind;
  label: string;
  ref: string;              // path | artifact_id | url | command_tool_call_id
  meta?: Record<string, unknown>;
}

export interface TurnSummary {
  turnId: string;
  recap: string;            // 1–3 sentences
  artifacts: TurnArtifactRef[];
}

export interface AgentChatMessage {
  // ...existing fields...
  turnId?: string;                    // set from llm.delta / llm.response
  iterationIndex?: number;            // 0-based within the turn
  iterationSummary?: IterationSummary; // 1:1 with the message's iteration
  turnSummary?: TurnSummary;          // attached to the tail assistant
                                      // message of a turn
}
```

### Reducer (`src/runtime/reducer.ts`)

Three changes:

- Extend `applyLlmDelta` and `applyLlmResponse` to read `turn_id` and
  `iteration_index` from `event.data` and stamp them on the resulting
  `AgentChatMessage`.
- New handler for `iteration.summary`: find the assistant message
  whose `(turnId, iterationIndex)` matches the event and set
  `iterationSummary`. Out-of-order arrival is fine — the message
  already exists by the time the summary lands.
- New handler for `turn.summary`: find the **last** assistant message
  with `turnId === event.data.turn_id` and set `turnSummary` on it.

### `IterationGroup` (new — `components/chat/iteration-group.tsx`)

Collapsible timeline row. Used only in Simple mode. Rendered once per
assistant message with an `iterationSummary` (or once per message
without one, in which case it stays expanded — see "Live state").

- **Collapsed**: one-line `iterationSummary.summary` text, status dot
  whose color reflects the worst tool status inside (running →
  pulsing primary, any error → red, else emerald).
- **Expanded**: today's per-tool entries (`messageToEntries`) for the
  message's `toolCalls`, plus the message's `reasoning`. Browser-
  activity and web-search sub-grouping continue to apply inside the
  expansion. In Simple mode, `AssistantGroup` renders one
  `IterationGroup` per constituent assistant message in the visual
  group, in order.

### Live state — Simple mode while streaming

- Assistant message with no `iterationSummary` yet: row reads
  `Thinking…` shimmer (no tools started) or `Working… (N tools)`
  (tools running). The live iteration is **always expanded** so
  progress is visible.
- The moment the matching `iteration.summary` event lands and the
  reducer sets `iterationSummary`, the row swaps to the one-line
  summary and collapses.
- If the summary never arrives (timeout, error, replay of pre-feature
  history with no `turnId` stamped on the message), the iteration
  stays expanded permanently.

### `TurnSummaryCard` (new — `components/chat/turn-summary-card.tsx`)

Rendered below the final text entry of an assistant turn whenever the
tail assistant message has `turnSummary` set. Hidden in Expert mode.
Hidden when both `recap` is empty and `artifacts` is empty.

Artifact links dispatch by `kind`:

| `kind`     | Action                                                                              |
|------------|-------------------------------------------------------------------------------------|
| `file`     | Calls existing `onFileSelect(path)` (`ref` is the workspace-relative path).         |
| `artifact` | Resolves `ref` (artifact id) to the session's `artifact.created` system message and renders an inline `ArtifactBlock`. |
| `url`      | External `<a target="_blank" rel="noopener noreferrer">`.                            |
| `command`  | `ref` is a tool-call id. The card scans `messages[*].toolCalls` to find it and opens the existing terminal-tool detail dialog. If the tool call cannot be located (e.g. truncated history), the row renders as plain non-clickable text. |

### Rendering — Simple mode layout

```
┌─ assistant turn ──────────────────────────────────────────┐
│                                                           │
│  ●  Rework hero paragraph to introduce brain/hands…    ▸  │  ← iteration 0
│  ●  Inline the new metaphor across the feature list   ▸   │  ← iteration 1
│  ●  Working…                                              │  ← live iteration
│                                                           │
│  ─ final answer text ────────────────────────────────     │
│  Here's what I changed and why…                           │
│                                                           │
│  ┌─ TurnSummaryCard ──────────────────────────────────┐   │
│  │ Reworked the hero section around the brain/hands   │   │
│  │ metaphor and propagated it through the page.       │   │
│  │                                                    │   │
│  │ • landing.html       (edited)                      │   │
│  │ • hero-copy.md       (artifact v3)                 │   │
│  │ • https://example…   (referenced)                  │   │
│  └────────────────────────────────────────────────────┘   │
│                                                           │
│  [👍 👎]                                                  │
└───────────────────────────────────────────────────────────┘
```

### Rendering — Expert mode

Unchanged from today. The view-mode toggle gates between the two
render paths inside `AssistantGroup`. Existing grouping
(`groupBrowserActivityEntries`, `groupWebSearchEntries`) and per-tool
blocks are reused unchanged.

## Edge cases

| Case                                                                                       | Behavior                                                                                                                                          |
|--------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------|
| Session replay of pre-feature messages (no `iteration.summary` events in history)          | Simple mode falls back per-iteration to the live-state rendering (expanded with reasoning + tools). No empty rows.                                |
| Turn errors mid-iteration                                                                  | Last live iteration stays expanded forever. Error info shows below as today. No `turn.summary` emitted.                                           |
| Turn cancelled by user                                                                     | Same as error: no `turn.summary`. Existing cancel UI unchanged.                                                                                   |
| Single-iteration turn with no tools (just a text answer)                                   | Iteration row reads the iteration summary. `TurnSummaryCard` renders only if `recap` or `artifacts` is non-empty.                                 |
| Tool call fails                                                                            | Iteration dot reads red. Expanding shows the failed tool. Summary text still describes what was attempted ("Attempted to rework hero paragraph"). |
| User toggles to Expert mid-stream                                                          | Re-renders current turn in expert layout immediately. Both layouts read the same underlying messages — no state loss.                             |
| Browser-activity / web-search groups inside a turn                                         | Render inside an expanded iteration in Simple mode, same as today. They do not escape iteration boundaries.                                       |
| `iteration.summary` arrives after the *next* iteration has already started                 | Reducer matches by `iterationIndex`. First iteration's row swaps from expanded "Working…" to its summary line, mid-stream.                        |
| Multiple assistant messages in one turn (tool-result re-prompt creates a follow-up message) | Each carries the same `turnId` and a distinct `iterationIndex`. `AssistantGroup` renders one `IterationGroup` per message, in order; `turn.summary` attaches to the last one.                      |
| Summariser fails / times out                                                               | No event emitted. SDK leaves the live-state rendering in place permanently. Logged server-side for telemetry.                                     |
| Artifact `ref` points to a file no longer in the workspace                                 | Link click is a no-op (`onFileSelect` already handles missing files via the file viewer's empty state).                                           |

## Rollout

Two independently shippable phases:

1. **Harness side first**: ship `turn_summarizer.py` + the two new
   events behind `emit_turn_summaries: bool = True`. Old SDKs ignore
   unknown events. No user-visible change yet.
2. **SDK side**: ship types, reducer handlers, view-mode toggle
   (defaulting to Simple), `IterationGroup`, `TurnSummaryCard`. The
   moment the SDK ships, Simple mode is the default view.
3. **Verify on a real session**, then leave the setting on. The
   `emit_turn_summaries` setting stays as a kill switch.

## Tests

### Harness

- `tests/harness/test_turn_summarizer.py` — unit tests for
  `summarize_iteration` and `summarize_turn` with a stubbed summary
  client. Cover normal output, empty reasoning, no notable tool
  calls, and the artifact filtering rules.
- `tests/orchestrator/test_worker_summaries.py` — integration test
  that runs a fake LLM turn through the worker and asserts the two
  new events land in the events table with the expected shape.
- Existing worker tests stay green; new events are additive.

### SDK

- `tests/reducer.test.ts` — extend to cover `iteration.summary` and
  `turn.summary` event handling, including out-of-order arrival.
- `tests/iteration-group.test.tsx` (new) — collapse/expand, live
  "Working…" state, fallback when summary never arrives.
- `tests/turn-summary-card.test.tsx` (new) — renders artifacts,
  dispatches by `kind`, hidden in Expert mode, hidden when empty.
- `tests/view-mode-toggle.test.tsx` (new) — toggle flips render path,
  persists via adapter, falls back to localStorage when adapter
  methods absent.
- All existing tests stay green. New tests explicitly opt into Expert
  mode where they assert today's per-tool rendering (so the default
  Simple mode doesn't accidentally invalidate them).

## Open questions

None. All design decisions have been made with the user.
