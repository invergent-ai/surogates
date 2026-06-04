# Deep Research

The **deep-research** workflow is a two-agent pipeline (planner + writer) that
produces a long-form, citation-grounded report from open-ended research
questions. It is an **opt-in capability** turned on per agent in Surogate
Studio; the base agent stays vanilla until a Studio admin flips the
*Deep research workflow* switch on its **Settings → Identity →
Capabilities** card.

When the switch is on, the agent's published Hub bundle gains two
`AGENT.md` sub-agents — `deep-research` (planner) and `research-writer`
(writer) — and the chat composer's slash menu gains
[`/deep-research <topic>`](../commands/index.md). Sending that slash
runs the full pipeline end-to-end with no further user intervention.

This document covers the workflow architecture, the tools each sub-agent
uses, the gating mechanism, and the guardrails that prevent the pipeline
from running away or producing dangling citations.

---

## 1. Architecture

```
User
 |
 |  /deep-research <topic>           (composer slash command)
 v
Base agent  ──────────────────────────────────────────────────────────┐
   |                                                                  |
   |  delegate_task(agent_type="deep-research", goal=<topic>)        |
   |  (single call, no batching, no retry on failure)                 |
   v                                                                  |
deep-research planner  (child session, channel="delegation")          |
   |                                                                  |
   |  loop:                                                           |
   |    web_search                                                    |
   |    web_extract                                                   |
   |    research_memory(action="add")    ─────► .research/memory.jsonl|
   |    research_outline(action="set")   ─────► .research/outline.md  |
   |                                                                  |
   |  reconcile outline vs. bank (action="list")                      |
   |                                                                  |
   |  delegate_task(agent_type="research-writer", goal=<outline>)    |
   v                                                                  |
research-writer  (grandchild session, channel="delegation")           |
   |                                                                  |
   |  section-by-section:                                             |
   |    research_memory(action="retrieve", query=<section>)           |
   |    write prose with [S#] citations                               |
   |                                                                  |
   |  create_artifact(kind="markdown", spec={content: <full report>}) |
   |     ── Guard 3 validates every [S#] against the bank ──         |
   |                                                                  |
   v                                                                  |
artifact lands in user session  <──────────────────────────────────────┘
```

The shared `.research/` directory under the session workspace is the
seam between planner and writer. Both children inherit the
`sandbox_root_session_id` of the base session, so `workspace_path` is
the same for all three actors and `research_memory` / `research_outline`
write to and read from one place.

---

## 2. The two sub-agents

Both `AGENT.md` files are packaged with the ops wheel under
`surogate_ops/features/deep_research/agents/` and uploaded into the
agent's bundle under `agents/<name>/AGENT.md` by the publisher when
`deep_research_enabled` is true.

### 2.1 `deep-research` (planner)

```yaml
name: deep-research
tools:
  - web_search
  - web_extract
  - research_memory
  - research_outline
  - delegate_task
  - ask_user_question
max_iterations: 60
```

The planner explores and structures the topic. Its loop is:

1. **Decompose** into sub-questions and capture them as an initial outline.
2. **Search and read.** `web_search` for candidates, `web_extract` for the
   promising ones.
3. **Curate evidence.** For each useful source: `research_memory(add)`
   stores it and returns a stable `source_id` (`S1`, `S2`, …).
4. **Refine the outline.** Rewrite `research_outline(set)` with new
   structure and gaps; treat the outline as a living document.
5. **Decide when to stop.** Stable outline + multi-source coverage of
   every sub-question.

**Source-ID discipline** (a hard contract):

- Never invent a `source_id`. Every `[S#]` in the outline must
  correspond to a source already stored via `research_memory(add)`.
- Before delegating to the writer, call `research_memory(action="list")`
  and reconcile — drop or rewrite any `[S#]` in the outline that isn't
  in the returned list.
- Clarifying questions (`ask_user_question`) belong to the planner.
  Asking up front when scope is ambiguous is cheap; asking mid-write is
  disruptive (see writer rules below).

### 2.2 `research-writer` (writer)

```yaml
name: research-writer
tools:
  - research_memory
  - create_artifact
max_iterations: 40
```

No web access — the writer writes *only* from the curated evidence bank.

Section-by-section:

1. Identify sections from the outline.
2. `research_memory(action="retrieve", query=<section>, k=8)` pulls the
   most relevant sources for that section.
3. Write prose with `[S#]` citations.
4. Final **References** section maps each `[S#]` to title + URL via
   `research_memory(action="list")`.
5. `create_artifact(kind="markdown", spec={content: <full report>})`.

**Writer must never call `ask_user_question`.** If the outline
references `[S#]` IDs not in the bank, trust the bank as ground truth:
silently drop or rewrite the affected claims. The `create_artifact`
Guard 3 enforces this — see §4.

---

## 3. Tooling

### 3.1 `research_memory`

Curated evidence bank under `{workspace}/.research/memory.jsonl`.

| action | required args | returns |
|--------|---------------|---------|
| `add` | `url`, `title`, `summary`, `evidence[]` | `source_id` (stable, sequential) |
| `retrieve` | `query`, optional `k` (default 5) | top-`k` sources by keyword overlap |
| `list` | — | every source in order (used for References) |

URL-keyed deduplication: an `add` for an already-stored URL returns the
existing `source_id` without creating a duplicate row.

### 3.2 `research_outline`

The living outline under `{workspace}/.research/outline.md`.

| action | required args | returns |
|--------|---------------|---------|
| `set` | `outline` (full markdown) | `sections[]` (extracted from `##`+ headings) |
| `get` | — | the current outline + `sections[]` |

Treated as a single document, not append-only; every `set` replaces the
file. Normalized on write (collapses blank-line runs, strips leading/
trailing whitespace).

---

## 4. The `create_artifact` citation validator (Guard 3)

Applied when `kind == "markdown"` and a workspace is wired. Scans the
body for `[S#]` chips (single `[S1]` or grouped `[S1, S2, S3]`) and
rejects the call when any `S#` is missing from
`{workspace}/.research/memory.jsonl`.

The error payload names every missing ID so the writer's retry message
is precise:

```json
{
  "success": false,
  "error": "Markdown body cites source IDs that are not in the research memory bank: ['S99', 'S100']. Either drop those claims, rewrite without the citation, or store the missing sources via research_memory(action=\"add\") before retrying.",
  "hint": "Run research_memory(action=\"list\") to see which source IDs are valid."
}
```

The validator no-ops when:

- `kind != "markdown"` (chart/html/svg labels containing `[S1]` text
  do not trigger).
- No `workspace_path` is wired (anonymous / harness-test sessions).
- The markdown has no `[S#]` chips at all.

---

## 5. Gating: per-agent opt-in

Deep research is a feature flag on each agent record:

```
agents.deep_research_enabled  BOOLEAN  NOT NULL DEFAULT FALSE
```

Toggling the flag in Studio republishes the agent's Hub bundle. The
publisher:

- When the flag is **true**, walks
  `surogate_ops/features/deep_research/agents/<name>/AGENT.md` from the
  ops wheel and uploads each file to `agents/<name>/<relpath>` in the
  bundle.
- When the flag is **false**, the next publish prunes the
  `agents/deep-research/` and `agents/research-writer/` subtrees so the
  bundle no longer carries them.

The bundle's `agents/` subtree is read by
[`ResourceLoader._load_agents_from_bundle`](../sub-agents/index.md#agent-loading)
on every wake — the planner + writer surface in the active agent's
sub-agent catalog the same way platform-bundled sub-agents do, but they
are visible only when the flag is on.

The chat composer reads `agent.deepResearchEnabled` from its store and
conditionally adds the `/deep-research` slash entry to the builtin menu.
Disabling the flag both removes the bundle entries (so a delegate by
name fails) and hides the slash entry (so the user can't invoke it
from the menu either).

---

## 6. Operational guardrails

The deep-research workflow runs expensive children that themselves
spawn sub-agents; without guardrails one user prompt can fan out into a
runaway fleet. The harness enforces four:

1. **Delegation timeout is 900s** (15 min). Set in
   `surogates/tools/builtin/delegate.py::_DELEGATION_TIMEOUT_SECONDS`.
   Long enough for a planner → writer hand-off; a child that doesn't
   complete in 15 minutes is considered wedged.

2. **Slash directive forbids retry-on-failure and batch fan-out.** The
   `/deep-research` slash command rewrites the user message to a
   directive that explicitly instructs the base LLM: *"Call
   delegate_task EXACTLY ONCE with a single goal. If it returns an
   error or times out, report the failure to the user verbatim and
   stop. Do NOT retry."*

3. **Planner self-recursion is rejected at the tool layer.** A session
   running as `agent_type="deep-research"` cannot delegate to another
   `deep-research`. Defined by `_NON_RECURSIVE_AGENT_TYPES` in
   `delegate.py`; the writer (`research-writer`) is intentionally not
   on this list so the normal handoff is allowed.

4. **Batch `goals=[...]` rejected for non-recursive agent types.** A
   single `delegate_task` call may not fan multiple deep-research
   goals out in parallel. Single-goal calls remain legal.

In the UI: any session with `channel="delegation"` (or any session
with a `parentId`) is rendered **read-only**. The composer is replaced
by a `Sub-task started by parent session — read-only` banner. The
parent's LLM is the only authoritative voice in the child; user input
injected mid-stream either bends the goal or races the parent's
completion read.

---

## 7. Storage layout

```
{workspace}/
└── .research/
    ├── memory.jsonl     (one JSON line per source; sequential S# ids)
    └── outline.md       (the living outline; normalized on every set)
```

The directory is created on demand by `research_memory` /
`research_outline`. It lives inside the session workspace, so it is
deleted when the workspace is reaped at session end.

---

## 8. UI surface

The `agent-chat-react` SDK exposes the deep-research artifacts directly
in the chat thread:

- **Outline cards.** Each `research_outline(set)` renders as a compact
  card showing the outline body and an extracted section count.
- **Evidence one-liners.** Each `research_memory(add)` shows
  `Recorded source S3 · arxiv.org`; `retrieve` shows
  `Retrieved N sources for "query"`; `list` shows `Listed N sources`.
- **Sources panel.** An expandable card above the composer; populated
  in real time as the planner curates the bank. Citation chips in the
  final report deep-link via element id `source-<sourceId>` (clicking
  a chip auto-expands the panel and scrolls to the row).
- **Citation chips.** The writer's `[S#]` references are linkified in
  the rendered markdown; clicking opens the source URL or scrolls to
  the panel row.

The SDK does not auto-toggle the Sources panel based on agent
capabilities; the panel appears the first time a source is added to the
bank and stays available for the rest of the session.

---

## 9. End-to-end testing

A complete smoke test:

1. Enable the toggle on a test agent in Studio.
2. Wait for the bundle republish (check
   `agent_runtime_config.config->>'bundle_version'` bumps).
3. Open a new chat session.
4. Type `/<deep-research>` from the slash menu, append a topic, send.
5. Watch the timeline for:
   - `delegate_task → deep-research` (planner spawned).
   - Multiple `research_memory(add)` calls populating the Sources
     panel.
   - One or more `research_outline(set)` calls updating the outline
     card.
   - `delegate_task → research-writer` (handoff).
   - Writer's `research_memory(retrieve)` calls and final
     `create_artifact`.
   - Markdown artifact rendered inline with linkified `[S#]` chips.

If `create_artifact` is rejected with the Guard 3 error, the planner
left dangling citations in the outline — see §4. The writer will
retry once with the validator's hint; persistent failure means the
planner needs to be re-prompted to reconcile before delegating.
