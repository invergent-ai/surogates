# GAIA Benchmark Results

Scored against the frozen dev split (110 tasks) unless the size column says
otherwise. "Model" is the model that actually served the calls: sessions run
under the `surogate` / `surogate-pro` tier sentinels, and the sentinel is not
a model — it resolves per request, so a run is only comparable to another run
that resolved the same way.

Strict is the official GAIA scorer. Lenient additionally accepts a correct
answer in the wrong format; the gap between them is a prompt problem, not a
capability problem.

| Date | Run | Where | Model served | Size | Strict | Score |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-07-26 | `dev-001` | local harness | claude-sonnet-5 | 110 | 86 | **78.2%** |
| 2026-08-14 | `dev-005` | local harness | claude-sonnet-5 (+ qwen3.7-max) | 110 | 74 | **67.3%** |
| 2026-08-27 | `dev-006` | prod, agent `gaia-xi8anu` | Surogate Pro | 110 | 62 | **56.4%** |
| 2026-08-28 | `dev-007` | prod, agent `gaia-xi8anu` | Surogate Pro | 110 | 53 | **48.2%** |
| 2026-08-29 | `dev-018` | local harness, agent `gaia-h5iiol` | claude-sonnet-5 | 110 | 62 | **56.4%** |
| 2026-08-29 | `dev-021` | local harness, agent `gaia-h5iiol` | claude-sonnet-5 | 110 | 66 | **60.0%** |
| 2026-08-29 | `dev-022` | local harness, agent `gaia2-tbmpuz` | claude-sonnet-5 | 110 | 68 | **61.8%** |

> **The 2026-08-27 run is `dev-001` in its own `report.md`.** Run ids auto-
> increment from the contents of `runs/`, and it was produced against a fresh
> `runs/`, so it restarted the count and collided with the local run of the
> same name. Renumbered here to `dev-006`. Note the counter increments from
> the *entry count* of `runs/`, not the highest existing id, so it does not
> hand out `dev-006` itself — a local run in a populated `runs/` jumps
> straight past it (the next one here came out `dev-017`). Its traces were
> only ever on the machine that ran it, so there is no `runs/dev-006/` to
> reconcile — if those traces resurface, land them under that id.

The 2026-08-27 run scored L1 21/33, L2 36/60, L3 5/17, with zero collection
errors, no `infra_error` flags, and flags: 14 `no_final_answer`,
11 `no_tool_use`, 4 `empty_llm_response`, 2 `tool_error`. It is not directly comparable to the local rows — different
platform, different agent config, different model tier — but the failure
shape is the same one the local runs show: turns ending without acting.

`dev-007` (same agent, same served model as `dev-006`) scored L1 17/33,
L2 31/60, L3 5/17, with zero collection errors, no `infra_error` flags, and
flags:
22 `no_final_answer`, 16 `tool_error`, 9 `no_tool_use`, 1 `empty_llm_response`.
The trace evidence points platform-side for the 9-point drop: 15 of the 22
unanswered sessions end on an `llm.request` that never receives an
`llm.response` — the provider returned nothing and the harness marked the
session `completed` instead of `failed` — and tool errors hit every tool
(terminal, browser, web_search, web_extract, vision) rather than any single
one. Sessions long enough to trigger context compaction answered 92% of the
time; the failures concentrate in sessions that died on their first or second
turn awaiting the model. Two platform items fall out: empty-response turn
exhaustion should fail a session rather than complete it, and the serving
route's reliability varied sharply between the two days.

### Smaller runs

Pilots, regression probes and single-task checks. Too small to read as a
score — they exist to isolate one behaviour, and a 1-task run at 100% means
nothing except that one task passed.

| Date | Run | Model served | Size | Strict | Score |
| --- | --- | --- | --- | --- | --- |
| 2026-07-26 | `pilot-1` | claude-sonnet-5 | 10 | 8 | 80.0% |
| 2026-07-26 | `pilot-1-attach` | claude-sonnet-5 | 2 | 2 | 100% |
| 2026-07-26 | `smoke-2` | claude-sonnet-5 | 1 | 1 | 100% |
| 2026-07-27 | `verify-pro` | (not recorded) | 3 | 0 | 0% |
| 2026-07-27 | `verify-pro-2` | claude-sonnet-5 | 3 | 1 | 33.3% |
| 2026-08-14 | `regress-master` | claude-sonnet-5 + qwen3.7-max | 10 | 4 | 40.0% |
| 2026-08-14 | `regress-a` | claude-sonnet-5 + qwen3.7-max | 16 | 6 | 37.5% |
| 2026-08-15 | `cache-test` | claude-sonnet-5 | 1 | 1 | 100% |
| 2026-08-15 | `cache-price` | claude-sonnet-5 | 1 | 1 | 100% |
| 2026-08-15 | `cache-turns` | claude-sonnet-5 | 1 | 0 | 0% |
| 2026-08-15 | `dump-master` | claude-sonnet-5 | 1 | 1 | 100% |
| 2026-08-15 | `dump-branch` | claude-sonnet-5 | 1 | 1 | 100% |
| 2026-08-15 | `gated` | claude-sonnet-5 | 1 | 1 | 100% |

`dev-002` is an aborted run — task traces, no `outcomes.json`, so it has no score.

## dev-018 → dev-021: the no-answer fixes

`dev-021` re-measures `dev-018` after three harness fixes aimed at sessions
that ended without an answer. Same agent, same served model, same split.

| | dev-018 | dev-021 |
| --- | --- | --- |
| Strict | 62/110 (56.4%) | **66/110 (60.0%)** |
| L1 / L2 / L3 | 21 / 34 / 7 | 21 / 39 / 6 |
| `no_final_answer` | 24 | **6** |
| `no_tool_use` | 15 | 11 |
| `tool_error` | 4 | 4 |
| `empty_llm_response` | 3 | 3 |
| `infra_error` | 1 | 0 |

Net +4 is 13 newly passing against 9 newly failing. The score moved less than
the flag did, and the flag is the better evidence: `no_final_answer` fell by
18, which is what the three fixes targeted.

What the 24 no-answer sessions in `dev-018` actually were:

- **13 — the model stated an intention and called no tool.** "Let me check the
  page directly via the browser...." then `finish_reason: stop`. Eleven were
  one turn after a browser call. Root cause: `browser_navigate` returned only
  `{url, title}`, so the model landed on a page with nothing to read. It now
  returns the page outline.
- **6 — a punctuation-only final response**, a 2-token `"..."` after turns of
  successful tool use. Non-empty, so it slipped past the empty-response ladder
  and was taken as the final answer.
- **5 — other**: one session with no `llm.response` at all, the rest answered
  wrongly rather than not at all.

On the 9 regressions: five carry no failure flag, meaning the model answered
and was simply wrong — run-to-run variance, which this benchmark warns about.
Three touched the browser, and one of those was a real regression this run
caught: navigate snapshots were not being pruned as superseded state, so a
34-call session reached 324,700 input tokens against a 262,144 window. Fixed;
not yet re-measured.

## dev-021 → dev-022: snapshot pruning

Re-measured after pruning superseded `browser_navigate` snapshots and marking
un-inlinable attachments. Ran on `gaia2-tbmpuz`, which differs from
`gaia-h5iiol` only in `research_enabled` / `deep_research_enabled` — flags that
gate the `/auto-research` and `/deep-research` slash commands, which the
benchmark never sends. Same model, same tools.

| | dev-021 | dev-022 |
| --- | --- | --- |
| Strict | 66/110 (60.0%) | **68/110 (61.8%)** |
| L1 / L2 / L3 | 21 / 39 / 6 | 23 / 37 / 8 |
| `no_tool_use` | 11 | 8 |
| `no_final_answer` | 6 | 5 |
| `empty_llm_response` | 3 | 1 |
| `tool_error` | 4 | 4 |
| Cost | $86.47 | $72.94 |

Net +2 is 8 newly passing against 6 newly failing, four of which carry no
flag — answered and wrong, i.e. variance. Treat +2 as "not worse", not as a
measured gain.

Pruning worked but did not rescue its target task. `d5141ca5` went 324,700 →
274,386 peak input tokens and 34 → 21 browser calls, and still failed. It
spent 58 turns and 9,995,161 input tokens, 92% of them cache reads, for
$3.56.

**The context window the harness plans against is wrong.** Sessions run under
the `surogate` sentinel, which the catalog gives a 262,144-token window, but
they are served by claude-sonnet-5 at 1,000,000. That 274,386-token request
succeeded — the real model had room to spare. So compaction and browser-state
pruning are sized against a limit ~3.8x smaller than the one that applies,
discarding context that never needed discarding. Nothing overflows; the
harness is just planning against the wrong number.

## The earlier regression

`dev-005` lost **10.9 points** against `dev-001` on the same 110 tasks: 16
tasks that passed in July fail in August, listed in `runs/dev-005/report.md`.

Where it went, by level:

| Level | dev-001 | dev-005 | Δ |
| --- | --- | --- | --- |
| 1 | 26/33 | 26/33 | 0 |
| 2 | 49/60 | 41/60 | **-8** |
| 3 | 11/17 | 7/17 | **-4** |

Level 1 is untouched, so this is not a broad capability loss — it is
concentrated in the multi-step tasks. The failure flags moved the same way:

| Flag | dev-001 | dev-005 |
| --- | --- | --- |
| `no_tool_use` | 4 | 9 |
| `no_final_answer` | 4 | 8 |
| `infra_error` | 2 | 5 |
| `tool_error` | 1 | 4 |
| `empty_llm_response` | 0 | 2 |

Two things are worth noting before treating that as a harness regression.

`dev-005` was not served by the same model. 114 of its calls went to
`qwen3.7-max` rather than `claude-sonnet-5`, and the `regress-a` /
`regress-master` probes on the same day were roughly half qwen. A tier that
resolves differently between runs is a confound, not a result — the model
mix has to match before a score delta can be attributed to harness changes.

The lenient-strict gap also closed (1 → 0), so the remaining failures are not
formatting. Combined with `no_tool_use` and `no_final_answer` doubling, the
shape is turns ending without acting, which is what the execution-discipline
and tool-surface work targets.

## Method notes

- Deltas from a single run are provisional. Tasks flip on their own; re-run
  an affected subset 3× before believing a fix.
- `--only-failing` is a fast filter, never the basis for a claim: it cannot
  show what a change broke. Only a full dev re-measure counts.
- Holdout (55 tasks) is untouched here on purpose. It is the overfitting
  signal and is only run when reporting.
