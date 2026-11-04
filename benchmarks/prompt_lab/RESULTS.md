# Results: the `surogate` prompt profile (2026-11-03)

> Since 2026-11-04 this prompt is the default for every model: the profile's
> fragments, tool descriptions, skill index lines and switches replaced the
> base ones, and profile selection (`SUROGATES_PROMPT_PROFILES`, model ids)
> was removed. The measurements below were taken while it was a profile.

Model: `surogate/Surogate3.7-35B-A3B` (NVFP4, 4 engines x 2 GPUs, the engine's
default sampling: temperature 1.0, top_p 0.95, top_k 20, thinking on, thinking
retained across turns). Harness: master `9a6c3fa9a`.

- **base**: master as is, session model `surogate` (the tier sentinel, i.e. the
  prompt every non-Claude, non-DeepSeek model gets today: working principles,
  execution discipline, brainstorming gate, mandatory-skills index, full tool
  descriptions).
- **opt2**: this branch, session model `surogate/Surogate3.7-35B-A3B`, which
  selects the `surogate` profile.

43 scenarios x 3 samples per arm, run in the same lab environment (the tool
groups section below adds 4 more). Scores are
deterministic checks (0-1). The paired difference is per scenario (mean of its
samples), bootstrapped over scenarios.

| | base | opt2 | change |
|---|---|---|---|
| Mean score | 0.960 | **0.989** | **+0.030** (95% CI +0.010 to +0.056) |
| System prompt (studio agent) | 40,162 chars | 8,608 chars | -79% |
| Tool schemas (45 tools) | 54,976 chars | 41,186 chars | -25% |
| Prompt tokens, first request | 23,195 | 13,154 | -43% |
| Prompt tokens per task | 182,107 | 100,640 | -45% |
| Completion tokens per task | 2,390 | 1,787 | -25% |
| LLM calls per task | 5.6 | 5.1 | -9% |
| Runs that loaded a skill | 16% | 13% | |
| Invalid tool calls | 0 | 0 | |

Wall time is not compared: the arms partly ran in different batches under
different engine load.

## Where the score moved

| Scenario | base | opt2 | Why |
|---|---|---|---|
| pptx_deck | 0.58 | 1.00 | base loaded `brainstorming` (the gate says "MUST ... before any creative work") and stopped at a design proposal awaiting approval |
| docx_memo | 0.83 | 1.00 | same stall; also loaded `writing-plans` |
| h_expense | 0.75 | 1.00 | base approved a 640 EUR claim the policy caps at 250 in 1 of 3 runs |
| h_feature2 | 0.92 | 1.00 | base loaded `brainstorming` for a fully specified function |
| kb_covered | 0.83 | 1.00 | answered from the search snippet without reading the page |
| kb_uncovered | 0.89 | 1.00 | |
| schedule | 0.92 | 1.00 | base started an unrequested `cron_run_now` |

Everything else (36 scenarios) was at or near 1.00 in both arms: the
everyday tier is easy for this model, which is why the hard tier exists.

Two findings from the iterations, both now in the profile:

- **Memory claims.** A first compact version said "I've saved your
  preference" without calling `memory` (2 of 3 runs). The base fragment's
  hard rule ("never say saved unless a memory call succeeded") and an explicit
  trigger ("from now on", "always", "I prefer") fixed it (3 of 3).
- **Policies govern actions.** With a knowledge base whose description did
  not obviously cover the request, both arms refunded 240 EUR against a
  100 EUR limit (base 0.27, opt v1 0.20). `guidance/kb_actions` ("before you
  act for someone in an area these knowledge bases cover, look up the
  governing policy") made opt2 escalate instead (0.80). With a KB whose
  description matches (`h_cs_refund2`, `h_addr_change2`) both arms score 0.80.
  Remaining miss in every arm: the order is looked up before identity is
  verified, against the policy's order.

## What the profile changes

- `working_principles` rewritten (act on a reasonable reading, ground facts in
  tools, verify then report, deliver, language, untrusted content, judgment);
  `execution_discipline` merged into it (empty overlay).
- `brainstorming_gate` limited to large, open-ended builds with unclear
  requirements.
- Skills: the "1% chance / red flags / STOP" block replaced by one paragraph
  next to the index; short "use when" index lines (`skills.yaml`); `SKILL.md`
  files unchanged.
- `ask_user_question`, `artifact`, `browser`, `web_search`, `memory`,
  `session_search`, `parallel_tools`, `workspace_rules`, default personality:
  compact versions; tool mechanics live in the tool descriptions.
- `tools.yaml`: compact descriptions for 30 tools (descriptions only; names,
  types, enums, required lists unchanged). office_* tools and the office
  skill are untouched.
- `compact_context`: no org UUID, model id, context-window or platform
  architecture lines.

## On-demand tool groups (`tool_groups` in `profile.yaml`)

Rarely needed tools are held back: their schemas are sent only after
`load_tools` (whose description lists the groups, one line each) or a direct
call to one of them. Held-back tools stay callable, so a skill that names
`cron_create` still works. Measured with browser, scheduling, media, github,
research and skill_authoring held back (`opt3`), against the same profile
without groups (`opt2`), 47 scenarios x 3 (the 43 above plus 4 that need a
group: a JavaScript-only helpdesk page, a booking form, GitHub issues, an
image):

| | opt2 | opt3 | change |
|---|---|---|---|
| Mean score | 0.990 | 0.979 | -0.011 (95% CI -0.029 to +0.001) |
| Tool schemas shown at start | 45 tools, 41.2k chars | 20 tools, 22.1k chars | -46% |
| Prompt tokens, first request | 13.1k | 7.9k | -40% |
| Prompt tokens per task* | 92.0k | 66.8k | -27% |
| Uncached prompt tokens per task* | 16.5k | 11.2k | -32% (master: 24.0k) |
| Completion tokens per task* | 1,559 | 1,696 | +9% |
| LLM calls per task* | 4.81 | 5.15 | +7% |

\* without `h_xlsx_edit`, where two opt3 runs hit the 40-step cap chasing
cached formula values with LibreOffice missing from the lab host (the files
were correct in every run).

- In the 126 runs that needed no group, none loaded one.
- The browser, GitHub and scheduling tasks loaded the right group in every run
  and scored like opt2. The helpdesk task spent about two extra calls
  (`web_extract`, a search) before loading the browser.
- With media held back, "generate a logo" was answered with a hand-drawn SVG
  in 1 of 3 runs instead of `generate_image` (3 of 3 when shown). The shipped
  profile therefore keeps `generate_image`/`generate_video` shown and holds back
  browser, scheduling, github, research and skill_authoring (22 tools, 24.4k
  chars shown at start). That final configuration was smoke-tested, not
  re-measured.
- Adding a group changes the tool list, so the engine re-reads the
  conversation once after each load; the uncached figures above include it.

## Real harness: DABstep and EnterpriseOps-Gym (2026-11-03/04)

The lab above calls the model directly. To check that the profile holds up in
the product, two full runtimes (api, worker, mcp-proxy, docker sandboxes, ops
server and LLM proxy) ran side by side against the same 4 engines: **master**
(`9a6c3fa9a`) and **branch** (this PR). Both used the engine's default
sampling (temperature 1.0, top_p 0.95, top_k 20, thinking on). Both
benchmarks score deterministically: DABstep against its answer key,
EnterpriseOps-Gym (oracle tool mode, 7 domains) with its SQL checks on the
final database state. No LLM judge.

Master ran first and was frozen: 310 DABstep tasks and 426 EnterpriseOps-Gym
tasks that completed (EnterpriseOps-Gym: 2 samples, scored as their mean).
The branch ran each task once. Pairs are per task; 95% CIs bootstrapped over
tasks.

| | n | master | branch | change (95% CI) | input tokens/task | output tokens/task |
|---|---|---|---|---|---|---|
| DABstep | 191 | 0.466 | **0.696** | **+0.230** (+0.152 to +0.314) | 747k -> 554k (-26%) | 17.3k -> 15.8k (-8%) |
| EnterpriseOps-Gym | 191 | 0.327 | 0.351 | +0.024 (-0.026 to +0.073) | 340k -> 225k (-34%) | 9.8k -> 9.5k (-2%) |
| Pooled | 382 | | | **+0.127** (+0.079 to +0.175) | | |

EnterpriseOps-Gym by domain (tasks, master -> branch): calendar 31,
0.13 -> 0.16; csm 25, 0.34 -> 0.36; drive 32, 0.50 -> 0.50; email 9,
0.67 -> 0.67; hr 43, 0.34 -> 0.37; itsm 29, 0.36 -> 0.45; teams 22,
0.14 -> 0.09.

- **Coverage.** The branch run was stopped when the engine box was needed
  elsewhere: DABstep 211 of 310 tasks, EnterpriseOps-Gym 197 of 426.
  Sessions that timed out (DABstep 9, EnterpriseOps-Gym 5) are left out, not
  re-run; master's tasks that timed out twice are left out too.
- **DABstep** mixes two profile wordings: 102 tasks ran with the first one
  (+0.284, +0.167 to +0.392), 89 with the final one (+0.169, +0.056 to
  +0.281). The final wording only adds the EnterpriseOps-Gym fix below.
- **What the first wording got wrong.** On EnterpriseOps-Gym it scored -0.039
  (-0.111 to +0.033, n=90) against master. The profile told the model to run
  `date`, so it called `terminal`, which these gym-tools-only agents lack,
  and it guessed MCP resources, prompts and URIs (in 48% of tasks, master
  26%). The date line went (Context has the date), and
  `guidance/working_principles` now asks for tools used as documented,
  without guessing undocumented interfaces.
- **What did not help.** A further sentence ("use exact values verbatim; read
  back what you changed before reporting done") cost EnterpriseOps-Gym about
  0.05 against the final wording (0.292 -> 0.247 on 89 paired tasks): fewer
  writes per task (4.6 -> 3.9), more runs with no write at all (5% -> 12%),
  and no fewer paraphrased values. It is not in this PR.
- **Why EnterpriseOps-Gym barely moves.** In a sample of failed runs, the
  model had all it needed in the prompt (the task's policy text included).
  It missed a policy trap (reopened a closed case on a phone call), used an
  unstated convention wrongly (notification type, who is the caller of a
  call), skipped a step of a long task, picked the wrong record, or read a
  value literally where the task meant otherwise, and the other way round.
  These are judgment errors no prompt rule fixes; leaderboard figures for
  the strongest models are around 50% on this benchmark.
- **The tool-name fix** (`message_utils.clean_tool_call_name`,
  `neutralize_unsendable_tool_names`) comes from these runs: in a few
  EnterpriseOps-Gym sessions the model wrote its template's call markup into
  a tool name, and the replayed name made every later request fail until
  the session timed out.
- **The worker fix** (build the prompt with the wake's model id) also comes
  from these runs: on a session's first wake the row has no model yet, so
  the profile was never selected in the real runtime.

## Limits of this evidence

- 3 samples per scenario; 43 scenarios; deterministic checks written for these
  tasks. A lab, not a public benchmark.
- Web pages, KB, memory, cron and MCP-style tools are scripted. Terminal and
  file tools are real, but the host lacked LibreOffice, pandoc and poppler, so
  the office skills' render/recalc steps failed in both arms.
- Only the Studio (web) channel and a plain agent were exercised: no
  messaging channels, coordinators, cases, whiteboard or browser sessions;
  those keep the base fragments under the profile.
- Not measured on other models: the profile is selected only for Surogate 3.x.
