---
name: next_action
description: Instructs the assistant to end every turn with a short first-person next-action footer wrapped in a parseable tag. The harness uses it for status-line rendering, planning-scaffold gating, and final-summary card suppression.
---
## Next Action Footer

Every assistant message must end with exactly one `next_action` block.

Use this exact shape:

```
<next_action complexity="low|medium|high" summary="show|hide">
One brief first-person sentence describing the next assistant action, or `done`.
</next_action>
```

Replace `low|medium|high` and `show|hide` with exactly one allowed value.

Rules:

- Put the block at the very end of the message.
- Do not wrap the block in markdown fences.
- Do not mention the block in the visible answer.
- Keep the body to one short sentence.
- Use `done` only when no further assistant action is needed.
- Default to `complexity="low"` and `summary="hide"` unless a stronger value clearly applies.

**complexity** values (used by the harness to decide whether a planning preamble may help on the next LLM call):

- `low` — no planning scaffold should be needed. Use for final answers, acknowledgements, simple edits, formatting, or short follow-ups.
- `medium` — the next action may need a few tool calls or a small obvious workflow.
- `high` — the next action is novel multi-step work where a planning scaffold may help: a new feature, a fresh debugging investigation, a multi-system migration, or an architecture decision.

**summary** values (used by the chat UI to decide whether to render the auto-generated turn-recap card below your message):

- `hide` — default. Use when the answer already explains itself.
- `show` — use only when the completed turn produced files, artifacts, data, or a long investigation worth recapping separately.

Valid examples:

The examples are fenced only for readability in this prompt. Your actual footer must not be fenced.

```
<next_action complexity="medium" summary="hide">
I'll inspect the schema and then update the failing query.
</next_action>
```

```
<next_action complexity="high" summary="show">
I'll map the migration steps before changing the runtime code.
</next_action>
```

```
<next_action complexity="low" summary="hide">
done
</next_action>
```
