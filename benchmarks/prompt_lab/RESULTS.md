# Results: the `surogate` prompt profile (2026-11-03)

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
