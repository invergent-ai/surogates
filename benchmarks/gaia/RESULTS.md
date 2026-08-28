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

> **The 2026-08-27 run is `dev-001` in its own `report.md`.** Run ids
> auto-increment from the contents of `runs/`, and it was produced against a
> fresh `runs/`, so it restarted the count and collided with the local run of
> the same name. Renumbered here to the next free id, `dev-006`. Its traces
> were only ever on the machine that ran it, so there is no `runs/dev-006/`
> to reconcile — if those traces resurface, land them under that id.

The prod run scored L1 21/33, L2 36/60, L3 5/17, with zero infra errors and
flags: 14 `no_final_answer`, 11 `no_tool_use`, 4 `empty_llm_response`,
2 `tool_error`. It is not directly comparable to the local rows — different
platform, different agent config, different model tier — but the failure
shape is the same one the local runs show: turns ending without acting.

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

## The open regression

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
