# Context Compressor — Behavioural Characterization

Source under inspection (file grew from 830 → 977 LOC across the fixes
described in §7.1 and §7.5):
- [surogates/harness/context.py](../../surogates/harness/context.py) (977 LOC) — the compressor.
- [surogates/harness/loop.py](../../surogates/harness/loop.py) — call sites at L1455–1481, L1747–1771, L2174–2193, L2422–2452.
- Companion test harness: [scripts/inspect_compression.py](../../scripts/inspect_compression.py).
- Live-LLM integration tests: [tests/test_compression_with_real_llm.py](../../tests/test_compression_with_real_llm.py)
  (gated on `SUROGATE_LLM_API_KEY`).

The compressor is a single class, `ContextCompressor`, instantiated per worker
and shared across sessions on that worker. It owns no event log access — it
takes a flat OpenAI-shaped `messages: list[dict]` plus an `llm_client` and
returns a new (compressed) message list.

---

## 1. Trigger condition

**Two ways the compressor is invoked, both gated by a token-count threshold:**

- **Pre-flight, every iteration of `_run_loop`** ([loop.py:1455](../../surogates/harness/loop.py#L1455)):
  `self._compressor.should_compress(messages, system_prompt)`. Called *after*
  the assistant turn has been processed and tool results appended.
- **Reactive, on 413 / context-length errors during an LLM call**
  ([loop.py:1747-1771](../../surogates/harness/loop.py#L1747-L1771)). Forces a
  compress even if under threshold.
- **`_engineer_context` re-check on wake replay** ([loop.py:2174](../../surogates/harness/loop.py#L2174)).
- **Explicit `/compress` command path** ([loop.py:2422](../../surogates/harness/loop.py#L2422)).

`should_compress` ([context.py:178-196](../../surogates/harness/context.py#L178-L196))
accepts either an int (true API-reported token count) or `(messages, system_prompt)`
strings, in which case it falls back to a **rough char/4 estimate**
(`_estimate_messages_tokens_rough`, [context.py:67-84](../../surogates/harness/context.py#L67-L84)).

The threshold is **fixed, not adaptive**:

```python
# context.py:139
self.threshold_tokens = int(self.context_length * threshold_percent)
```

Defaults: `threshold_percent = 0.50`, so **fires at 50% of model context
window**. For a 262K-context Qwen3.6 model that's ≈131K tokens. The model's
context window is resolved via `resolve_model_info` — if the model is unknown
to the catalog *and* not in config overrides, the compressor falls back to
**128K tokens** ([context.py:64](../../surogates/harness/context.py#L64), L131)
and emits a warning, but otherwise functions. In practice this means an
unknown model gets a 64K compression threshold regardless of its real window.

The estimator is char/4 — for non-ASCII/CJK content it under-counts; for
Anthropic streams it slightly over-counts. There is no message-count
fallback: 50 small messages won't trigger; one giant message will.

---

## 2. What gets preserved verbatim vs summarized vs dropped

`compress()` ([context.py:641-807](../../surogates/harness/context.py#L641-L807))
operates on the message list as-is, without re-deriving from the event log.

**Preserved verbatim (head)** — `protect_first_n = 3` messages
([context.py:691](../../surogates/harness/context.py#L691)):
- `messages[0]` is typically the system prompt; on the *first* compaction only,
  a one-paragraph note is appended announcing the handoff
  ([context.py:737-741](../../surogates/harness/context.py#L737-L741)).
  On subsequent compactions the system prompt is *not* re-annotated.
- `messages[1..2]` — usually the first user message and first assistant turn.
- `protect_first_n` is a constructor arg; default 3.

**Preserved verbatim (tail)** — token-budget-driven, never fewer than
`protect_last_n = 20` messages ([context.py:586-635](../../surogates/harness/context.py#L586-L635)):
- Walks backward from `len(messages) - 1`, accumulating estimated tokens
  until `tail_token_budget` is reached. `tail_token_budget = threshold_tokens
  * summary_target_ratio` ([context.py:144](../../surogates/harness/context.py#L144))
  — by default ~0.20 × 0.50 × context_window = **10% of the context window**
  in tail tokens. For a 262K model that's ≈26K tokens of tail.
- Floor of `protect_last_n` messages is enforced; if the budget would protect
  less, the floor wins.
- Boundary is then aligned backward to avoid splitting an
  `assistant(tool_calls=…)` from its `tool` results
  ([context.py:558-580](../../surogates/harness/context.py#L558-L580)) — if
  the cut would land mid-group, the **whole group is pushed into the
  summarised region**.

**Pre-pass: tool-output pruning** ([context.py:217-247](../../surogates/harness/context.py#L217-L247)):
Before the LLM summarisation, all `role: "tool"` messages older than
`protect_last_n * 3 = 60` from the end whose content is >200 chars get their
content replaced with the literal string `"[Old tool output cleared to save
context space]"`. The tool message itself stays in the list — only the body
is replaced. Tool results in the last 60 messages survive intact.

**Summarised (middle)** — everything between `protect_first_n` (forward-aligned
past any orphan `tool` results, [context.py:548-556](../../surogates/harness/context.py#L548-L556))
and the tail cut-off. After the tool-output pre-pass, the middle is sent to
the summariser as labelled text (`[ASSISTANT]:`, `[TOOL RESULT id]:`,
`[USER]:`, etc.) with per-message content cap of 3000 chars (truncated to
2000 head + 800 tail with a `...[truncated]...` marker;
[context.py:264-313](../../surogates/harness/context.py#L264-L313)).

**Dropped entirely**:
- The middle messages themselves are removed; only the LLM-generated summary
  string survives in their place.
- If summarisation fails (cooldown, exception, no client), the middle messages
  are dropped **with no replacement** ([context.py:768-770](../../surogates/harness/context.py#L768-L770)) —
  see §7 for the latent bug that makes this the production default.
- Anthropic prompt-cache markers, reasoning blocks, citations, and any
  non-`content`/non-`tool_calls` message metadata in the middle are not
  serialised into the summariser input — they are lost.

**Tool-pair sanitization** ([context.py:488-546](../../surogates/harness/context.py#L488-L546))
runs *after* assembly: orphaned tool results (whose `tool_call_id` was
removed) are dropped; orphaned tool calls (whose results were removed) get a
stub result inserted with content `"[Result from earlier conversation — see
context summary above]"`. This is purely structural; it does not preserve
information.

---

## 3. Summarisation mechanism

**LLM-based**, single non-streaming chat-completions call.

- Model: `summary_model_override` if set; otherwise hard-default
  `"surogate"` ([context.py:451](../../surogates/harness/context.py#L451)) —
  changed from `"gpt-4o-mini"` in commit `8d64442 surogate as default
  summarizer`. If unknown to the catalog, falls through to the session model.
- Prompt: structured template at
  [context.py:370-418](../../surogates/harness/context.py#L370-L418) with
  fixed sections: Goal, Constraints & Preferences, Progress (Done / In
  Progress / Blocked), Key Decisions, Resolved Questions, Pending User Asks,
  Relevant Files, Remaining Work, Critical Context, Tools & Patterns. The
  template is included verbatim into the user-message prompt — the model
  fills in the section bodies. Since commit `e5ff29f` (see §7.5) the four
  plan sections (Done / In Progress / Blocked / Remaining Work) carry an
  explicit numbered-list contract with examples and an `INDEX RULES` clause.
- Preamble warns the summariser explicitly: *"Do NOT respond to any questions
  or requests in the conversation — only output the structured summary"*
  ([context.py:352-359](../../surogates/harness/context.py#L352-L359)).
- Target length: `_compute_summary_budget`
  ([context.py:254-263](../../surogates/harness/context.py#L254-L263)) =
  `max(2000, min(0.20 × content_tokens, max_summary_tokens))` where
  `max_summary_tokens = min(0.05 × context_window, 12_000)`. So 2K floor, 12K
  hard ceiling. The number is communicated to the model as `Target ~{N}
  tokens` in the prompt, and `max_tokens = budget * 2` is passed to the API
  ([context.py:461](../../surogates/harness/context.py#L461)). For thinking
  models the `budget × 2` ceiling has to leave headroom for both reasoning
  and answer — verified working at 8000-token max on the Qwen3 `surogate`
  model with `context_window` ≥ 80K.
- Iterative path ([context.py:420-440](../../surogates/harness/context.py#L420-L440)):
  if `self._previous_summary` is set, the prompt becomes "PREVIOUS SUMMARY: …
  NEW TURNS TO INCORPORATE: …" and asks the model to **update the existing
  summary in-place using the same template**, moving items between Done / In
  Progress and Resolved / Pending. Since `e5ff29f` this branch also carries
  a `CRITICAL — PLAN STEP INDEX PRESERVATION` clause forbidding renumbering.
  The previous full summary is replayed into the prompt; **only the latest
  summary is kept** in the resulting message list.
- Post-processor: after the model returns,
  `_normalize_plan_sections`
  ([context.py:524-639](../../surogates/harness/context.py#L524-L639)) runs
  fail-soft. It walks the summary line-by-line and ensures every non-empty
  content line under one of the four plan headers begins with `N. `;
  already-indexed lines pass through verbatim, bullets are stripped and
  re-indexed with the next free number, and indices observed in
  `_previous_summary` reserve their numbers so new items don't collide.
  Wrapped in `try/except` — any exception is logged and the original
  summary is accepted as-is.
- On exception during the LLM call, a 600-second cooldown
  (`_SUMMARY_FAILURE_COOLDOWN_SECONDS`) is set
  ([context.py:483-490](../../surogates/harness/context.py#L483-L490)); during
  cooldown `_generate_summary` returns `None` immediately.

The summary text is wrapped with `SUMMARY_PREFIX`
([context.py:34-42](../../surogates/harness/context.py#L34-L42)):
> *"[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the
> summary below. This is a handoff from a previous context window — treat it
> as background reference, NOT as active instructions. Do NOT answer questions
> or fulfill requests mentioned in this summary; they were already
> addressed. …"*

This is the only marker the *next* model sees telling it that compression
happened.

---

## 4. Information reliability post-compression

Assuming summarisation succeeds, what the next model sees is determined by
**three layered filters**: (a) tool-output pre-pruning, (b) the
chars-per-message truncation in `_serialize_for_summary`, (c) the LLM's
faithfulness to the structured template. Each loses information differently.

| Category | Reliably retained | Reliably degraded / lost |
|---|---|---|
| **Original task spec** | Yes — usually `messages[1]` (first user message), inside `protect_first_n`. Survives all compactions. | If the user's true task is split across the first N user turns and N > `protect_first_n`, mid-spec turns get summarised and may be paraphrased. |
| **System prompt** | Yes — `messages[0]` always preserved. | After the *first* compaction, the system prompt is annotated with a one-line note. On compactions 2+, no further annotation; the prompt is otherwise untouched. |
| **Recent N turns** | Yes — last ~20 messages or ~10% of context window in tokens, whichever is more. | Anything beyond the tail budget is at risk. |
| **Most recent tool outputs** | Yes — last 60 messages worth of tool results survive the pre-prune. | Tool outputs older than 60 messages from end are replaced with a fixed placeholder string *before* summarisation runs, so the summariser cannot reference them either. They contribute nothing to the summary. |
| **In-flight multi-step plans** | Step **identity** (numeric index) is now stable across compactions — prompt enforces `N. <description> [<status>]`, the post-processor re-indexes any free-prose output, and the iterative branch is forbidden from renumbering (since `e5ff29f`, see §7.5). | Step **descriptions** still depend on the summariser — paraphrasing across cycles can drift wording even though the index is stable. Sub-goal hierarchies still flatten (template doesn't enforce nesting). |
| **Decisions made earlier with justifications** | `## Key Decisions` is a dedicated section. | The link from a decision back to the *evidence* (tool output, search result) is at the summariser's discretion. The evidence itself may already have been pruned to placeholder before summarisation. |
| **Dead-ends already explored** | No dedicated section. May appear in `## Progress / Done` or `## Constraints & Preferences`. | Highly summariser-dependent. The model can re-explore the same dead-end after compression because there is no explicit "do not retry these" surface. |
| **Intermediate conclusions** | `## Critical Context` is the catch-all. | Numerical values, error messages, hashes, paths are explicitly called out in the prompt to preserve, but truncation at 3000 chars per message and 2000+800 split can cut a long stack trace mid-line. |

**Reliability falls hardest on: tool-result evidence older than ~60 messages,
explored-and-rejected branches, and the *content* of multi-step plan steps
(though step identity is now stable — see §7.5).** These are
exactly the regimes the user cites — agent repeats completed work, loses
in-flight plans, drifts on confidence in earlier conclusions. Stable step
indices reduce the "repeats completed work" failure but don't eliminate it
when the model rewords a description into something it doesn't recognise as
done.

---

## 5. Re-compression behaviour

`compression_count` ([context.py:140](../../surogates/harness/context.py#L140))
is a per-instance counter. It is bumped at the end of `compress()`
([context.py:785](../../surogates/harness/context.py#L785)) but its only
real consumer is the head-annotation gate at L737 (annotate system prompt
only on count == 0).

**Iterative summary state** is `self._previous_summary`
([context.py:165](../../surogates/harness/context.py#L165)). On every
successful summarisation it is overwritten with the *new* summary
([context.py:454](../../surogates/harness/context.py#L454)). On the next
compression event, the iterative-update prompt branch is taken
([context.py:408](../../surogates/harness/context.py#L408)).

Behaviour over multiple compactions:
1. **Compaction 1**: Middle is summarised from scratch using the structured
   template. `_previous_summary` is set to the resulting text.
2. **Compaction 2**: The message list now contains
   `head + [summary_msg_1] + new_turns_since_compaction_1 + tail`. The cut
   boundaries are recomputed. `summary_msg_1` is itself a regular message in
   `messages` and **falls inside the middle region** (it's between
   `protect_first_n=3` and the tail cut). It gets serialised into the new
   summariser input as a regular `[ASSISTANT]:` or `[USER]:` block, *and*
   the prompt also embeds `_previous_summary` separately. The summariser
   sees the same content twice.
3. **Compaction N**: same pattern; `_previous_summary` always reflects the
   most recent output, but it is **never anchored** — the summariser is free
   to drop, paraphrase, or reorganise sections it received in the previous
   summary. There is no diff/anchor mechanism preventing decay across cycles.

There is **no notion of permanent vs ephemeral content** beyond the static
`protect_first_n` head. **Decay protection is now partial**: as of `e5ff29f`,
plan-step *indices* in the four plan sections (Done / In Progress / Blocked
/ Remaining Work) are anchored across cycles by both the prompt
(`CRITICAL — PLAN STEP INDEX PRESERVATION`) and the
`_normalize_plan_sections` post-processor, which reserves indices observed
in `_previous_summary` so new items get fresh numbers. Live-LLM verification:
indices from compaction 1 still appear in compaction 3 (see
[tests/test_compression_with_real_llm.py::test_third_compaction_preserves_first_compaction_indices](../../tests/test_compression_with_real_llm.py)).

The other sections (`## Key Decisions`, `## Constraints & Preferences`,
`## Resolved Questions`) are still **un-anchored** — a fact in compaction
1's Key Decisions will persist only if the summariser chooses to copy it
forward each time. Across 4–8 compactions in an 8-hour session, expect
non-trivial drift on those sections specifically.

The previous summary gets cycled through the LLM N times — each cycle is
lossy. This is the most important place to weight your training data.

---

## 6. The boundary turn

What does the model see immediately after a compression event?

The compressed list is assembled at
[context.py:733-783](../../surogates/harness/context.py#L733-L783):

```
messages[0..protect_first_n-1]      # head, system prompt may be annotated on 1st compaction
[summary_message]                    # role chosen dynamically, see below
messages[compress_end..end]          # tail
```

**The summary is injected as a regular message**, not a system message. Role
selection ([context.py:746-767](../../surogates/harness/context.py#L746-L767)):

- If the last head message is `assistant` or `tool`, summary role = `user`.
- Otherwise summary role = `assistant`.
- If that choice would create consecutive same-role with the *first tail*
  message, role is flipped to the opposite.
- If both roles would collide on either side, the summary is **merged
  inline into the first tail message's content** with the marker
  `--- END OF CONTEXT SUMMARY — respond to the message below, not the
  summary above ---` between summary and original content
  ([context.py:776-781](../../surogates/harness/context.py#L776-L781)).

Whichever path is taken, the summary body always starts with `SUMMARY_PREFIX`
(the "[CONTEXT COMPACTION — REFERENCE ONLY] …" preamble). So the explicit
signal to the next model is **the prefix string at the start of the summary
text**, not a structural marker. The model must read English to know
compression happened.

**Practical consequence for fine-tuning**: in your training trajectories,
the post-compression turn looks like one of:
- `{role: "user", content: "[CONTEXT COMPACTION — REFERENCE ONLY] …"} ` followed
  by an actual `{role: "user", content: <real ask>}` further down. **The
  model sees two consecutive user messages with very different intents.**
- `{role: "assistant", content: "[CONTEXT COMPACTION — REFERENCE ONLY] …"}`
  inserted into the assistant turn flow.
- Inline-merged summary inside an existing user message preceded by the END
  OF CONTEXT SUMMARY marker.

The first variant is the most common. Your fine-tuned model needs to be
robust to all three.

---

## 7. Failure modes identified during inspection

### 7.1 — `get_model_info` not imported, summariser path NameErrored on every invocation — **FIXED in `0ae62d7 fix context bug`**

**Original problem.** `_generate_summary` called
`get_model_info(summariser_model)` but the module only imported
`resolve_model_info, ModelInfo, estimate_tokens`. The call was *outside*
the `try:` block, so it propagated uncaught out of `_generate_summary`, out
of `compress()`, and was caught by the top-level `except Exception` in
`wake()` at [loop.py:578](../../surogates/harness/loop.py#L578) — emitting
`HARNESS_CRASH`, retrying, hitting the same NameError, and after 3 retries
failing the session.

The 600-second cooldown / "drop middle without summary" fallback was
unreachable because the NameError fired before the cooldown's try/except.
Every compression in production crashed; no ground-truth post-compression
turns ever made it into the event log. There were no tests covering this
path — the only test touching `ContextCompressor` used a `MagicMock`, and
`test_model_discovery.py` only exercised the constructor.

**Fix.** `get_model_info` was added to the import block in commit
`0ae62d7 fix context bug`. The summariser path now runs cleanly.
[tests/test_compression_with_real_llm.py](../../tests/test_compression_with_real_llm.py)
adds eight live-LLM integration tests covering the path end-to-end against
the `surogate` Qwen3 model, including a regression test
(`test_compressor_swallows_summariser_exception`) that points the
summariser at a non-existent model to verify graceful failure + cooldown
engagement without crashing.

### 7.2 — Tool outputs > 60 turns old are reduced to a fixed placeholder string before the summariser sees them

[context.py:217-247](../../surogates/harness/context.py#L217-L247),
called with `protect_tail_count = self.protect_last_n * 3 = 60`. The
summariser cannot include any specific value, error message, or
search-result content from older tool calls in the summary, because by the
time it runs the tool results have already been replaced. The summariser
*can* still see assistant messages that referenced those results in prose,
which is a partial mitigation, but anything the assistant did not explicitly
quote into its own message is gone.

For a long debugging session where the agent made 200+ tool calls, this
means **only the assistant's own narration of what it found** survives
compression — not the raw tool evidence. Decisions made on the basis of
older tool output therefore lose their evidence trail.

### 7.3 — Per-message 3000-char cap with mid-truncation can split structured content

[context.py:279-310](../../surogates/harness/context.py#L279-L310) —
content over 3000 chars gets cut to `[:2000] + "...[truncated]..." + [-800:]`.
Long stack traces, JSON tool results, and code diffs are routinely > 3000
chars; the head/tail split mid-line can misrepresent the result (e.g. the
"head 2000" stops in the middle of a multi-line error and the "tail 800"
starts mid-stack-frame). The summariser will not know it is reading a
truncated artifact and may confidently summarise the visible portion as
complete.

### 7.4 — Iterative re-summarisation has no anchor / pinning

§5 above. Across multiple compactions a fact in `## Key Decisions` is only
preserved if the summariser model chooses to copy it forward each time.
There is no whitelist of "must-retain" sentences, no diff against the
previous summary, no penalty for dropping content the previous summary had.
For an 8-hour session with 4–8 compactions, expect drift on Key Decisions,
Constraints, and Resolved Questions specifically.

### 7.5 — Plans summarised as free-form prose under section headers — **FIXED in `e5ff29f`**

**Original problem.** The template had `### Done / ### In Progress /
### Blocked / ## Remaining Work` headers but did not enforce a list-of-steps
format inside them. The summariser routinely produced free prose like
*"Currently fixing the auth bug; finished the schema migration."* Step
indices, dependencies, and the link between an item and its status were
lost. This was the failure mode that produces "agent repeats completed
work" — the compressed plan was too vague to distinguish *"step 3: done"*
from *"step 3: in progress"*.

**Fix.** Three layers of enforcement land together in commit `e5ff29f`:

1. **Prompt template** ([context.py:370-418](../../surogates/harness/context.py#L370-L418)) —
   each of the four plan sections now specifies the
   `N. <description> [<status>]` format with an inline example, plus a
   closing `INDEX RULES` clause stating that the four sections share one
   integer namespace and indices are stable across compactions.
2. **Iterative-update prompt** ([context.py:420-440](../../surogates/harness/context.py#L420-L440)) —
   adds a `CRITICAL — PLAN STEP INDEX PRESERVATION` paragraph instructing
   the summariser to preserve every index from `PREVIOUS SUMMARY`, move
   items between sections without renumbering, and append new items with
   the next free index.
3. **Post-processor**
   `_normalize_plan_sections` ([context.py:524-639](../../surogates/harness/context.py#L524-L639)) —
   fail-soft pass that walks the summary line-by-line and ensures every
   non-empty content line under one of the four plan headers begins with
   `N. `. Already-indexed lines pass through verbatim, bullets are stripped
   and re-indexed, indices observed in `_previous_summary` reserve their
   numbers so new items don't collide. Wrapped in a try/except — any
   exception is logged and the original summary accepted as-is.

**Verification.**
[tests/test_compression_with_real_llm.py](../../tests/test_compression_with_real_llm.py)
exercises the full contract end-to-end against the live `surogate`
endpoint:

- `test_first_compaction_produces_indexed_plan_sections` — at least two
  numbered items appear after a single compaction.
- `test_first_compaction_indices_start_at_one` — first index is 1.
- `test_second_compaction_preserves_indices` — every index from
  compaction 1 is still in compaction 2.
- `test_new_work_in_second_compaction_gets_index_above_prior_max` — new
  indices are strictly greater than the previous max.
- `test_in_progress_step_migrates_to_done_keeping_index` — when a step
  changes status, it migrates between sections keeping its original index.
- `test_third_compaction_preserves_first_compaction_indices` — indices
  from compaction 1 still present after three cycles.

What is **still drift-prone**: the *description text* under each index is
re-emitted by the summariser each cycle and can be paraphrased. If the
agent post-compaction needs to recognise "step 3" as the same work it
already did, the index gives identity but the description may have shifted
wording. That residual risk is now small enough to be a fine-tuning target
rather than a structural problem.

### 7.6 — Boundary alignment can push a large tool group into the summarised region wholesale

[context.py:558-580](../../surogates/harness/context.py#L558-L580) — if the
tail cut would split a `assistant(tool_calls=…) + tool + tool + …` group,
the boundary moves *back*, putting the whole group into the middle. For
agents that do many parallel tool calls in a single turn near the end of
context, the *most recent* tool call's results may be the ones to get
summarised, not the oldest. Combined with §7.2, the very tool output the
agent is currently reasoning about can be replaced with placeholder + brief
prose summary on the first compaction.

### 7.7 — Two consecutive same-role messages on certain head/tail role combinations

The role-selection logic ([context.py:746-767](../../surogates/harness/context.py#L746-L767))
prefers `assistant` for the summary, flips on tail collision, and only
falls back to inline-merge if both flips would collide. For OpenAI/Anthropic
APIs that don't strictly require alternation this is fine; for some
local-served Qwen builds with strict role-alternation chat templates, the
inserted summary can produce either two consecutive `user` or two
consecutive `assistant` messages depending on context shape. Verify your
inference server handles this; if not, the workaround is to pin
`summary_role` in your fine-tune to whatever your serving stack accepts.

### 7.8 — `compression_count` only suppresses the system-prompt annotation, not the iterative-update prompt path

§5: `_previous_summary` is the only state that drives iterative updates. It
is reset only by re-instantiation of the compressor — i.e. on worker
restart. If a session is paused and resumed on a *different* worker pod
(crash recovery), the new worker has `_previous_summary = None` and runs
**from-scratch** summarisation again on the next compaction, even though
the message list already contains a previous summary as a regular message.
The summariser sees the previous summary as part of the middle and may
paraphrase it. This produces an asymmetry between same-pod and
cross-pod compactions.

---

## Implications for fine-tuning

1. **The training distribution you want to weight heavily is "post-compression
   turn" — what comes after the SUMMARY_PREFIX message, with the model expected
   to behave coherently against a structured-but-lossy summary.**
   Both §7.1 (NameError) and §7.5 (free-prose plans) are now fixed, so the
   compressor produces real summaries with stable plan-step indices.
   Generate trajectories by running frontier models inside the harness, read
   `compacted_messages` off `CONTEXT_COMPACT` events
   ([loop.py:1471-1478](../../surogates/harness/loop.py#L1471-L1478)),
   and pair each with the messages that immediately preceded the event.

2. **The N-th compression turn is harder than the 1st.** Drift accumulates.
   Consider sampling synthetic trajectories with N ∈ {1, 2, 4, 8} compactions
   and weighting higher-N more (real 8-hour sessions will hit this). The
   live integration tests cover up to three cycles; preservation holds at
   that depth, but the summariser's *description-text* drift is still
   present and worth training the model to tolerate.

3. **Tool-evidence loss is structural.** Your model will frequently need to
   make decisions where the supporting tool output was reduced to a
   placeholder (§7.2). Training data should reflect this — *"I previously
   verified X (see summary §Critical Context); proceeding on that basis"*
   is the behaviour you want, not *"let me re-verify X."*

4. **Plan-step indices are now stable; descriptions still drift.** Train
   the model to treat the index as the durable identity of a step and the
   description as evidence (which may have been paraphrased). If a step
   shows index 3 in `### Done`, do not redo it even if the description
   doesn't match what you remember writing — the index is the contract.

5. **Sections that are still un-anchored** (`## Key Decisions`,
   `## Constraints & Preferences`, `## Resolved Questions`) drift across
   cycles. If you need a decision to survive 8-hour horizons reliably, the
   only sure path today is to surface it into a plan step (so it gets an
   anchored index) or write it into memory via the `memory` tool (separate
   subsystem, not subject to compaction).

---

## How to run the test harness

**Local script** — [scripts/inspect_compression.py](../../scripts/inspect_compression.py).
Walks a synthetic or real conversation through the compressor, printing
before/after at each compression event with role-by-role diff, anchor
survival, plan-section indices, and inter-event index diffs. Default mode
uses a mock summariser (no API calls); pass `--real-summariser --base-url …
--api-key …` to hit a real OpenAI-compatible endpoint. The
`--no-patch-name-error` flag is retained as a regression guard but the
underlying NameError is fixed (§7.1) — running with the flag now succeeds.

**Live-LLM integration suite** — [tests/test_compression_with_real_llm.py](../../tests/test_compression_with_real_llm.py).
Eight pytest cases gated on the `SUROGATE_LLM_API_KEY` env var. To run:

```
SUROGATE_LLM_API_KEY=<key> python -m pytest tests/test_compression_with_real_llm.py -v
```

Override the endpoint or model via `SUROGATE_LLM_BASE_URL` /
`SUROGATE_LLM_MODEL`. Tests use a `model_overrides` flag to set the
compressor's catalog `context_window` to 80K so the threshold is
manageable; the LLM call itself uses the model's true context.