# Prompt lab

A/B harness for the **model-facing surface** of Surogates: the system prompt,
the tool schemas and the skills index. It renders exactly what a checkout's
`PromptBuilder` and tool registry would send for a session, runs workplace
scenarios through a Surogates-shaped agent loop against a live model, and
scores the results with deterministic checks.

It was built to tune the prompt for Surogate 3.7 (35B-A3B); the result,
first shipped as the `surogate` prompt profile, is now the default prompt
for every model. It is not a public benchmark: its job is to compare two
checkouts on the same tasks.

## What is real and what is simulated

| Real | Simulated |
| --- | --- |
| The checkout's system prompt (`PromptBuilder.build()`), tool schemas (`ToolRegistry.get_schemas()`; on older checkouts plus the profile's `apply_tool_profile`), skills index and `SKILL.md` bodies (served by `skill_view`, files staged into the workspace) | Web search/extract (scripted pages per scenario), KB search/read, memory, todo, `ask_user_question` (scripted answers), `create_artifact`, cron, scenario-specific MCP-style tools |
| File tools and `terminal` act on a real per-run workspace (python, node, the office libraries if the host has them) | The sandbox: commands run on the host in the run's folder with a deny-list, not in a pod. LibreOffice, pandoc and poppler are only there if the host has them |
| The model, its sampling defaults and its tool-call parser (whatever the endpoint serves) | The harness loop's extras (unfinished-turn nudges, todo reminders, context compression, streaming) |

The run ends when the model answers without a tool call. If that answer
hands a question to the user, a scripted reply ("go ahead with your best
judgment", or the scenario's own) is sent once, so a stall costs a turn the
way it does in a one-shot benchmark.

## Scenarios

`scenarios.py` (30, everyday workplace work), `scenarios_hard.py` (13,
multi-step with traps) and `scenarios_groups.py` (4 that need a held-back tool
group: browser pages and a form on a scripted site, GitHub, image generation): chat and drafting in English and Romanian, CSV/JSON
analysis, xlsx/docx/pptx creation and editing, PDF reading, code fixes and a
feature with hidden tests, YAML edits that must keep comments, web research
with stale and conflicting sources, multi-hop lookups, KB grounding (covered,
uncovered, outdated page), policy-bound customer-service flows (identity
checks, refund and approval limits, refusals), clarification judgment (ask
when blocked, act when specified), prompt injection in a file, memory,
artifacts, scheduling, multi-turn edits, long-document needles, strict
formatting. Each check returns a 0-1 score with notes.

## Running

```bash
cd benchmarks/prompt_lab
export LAB_BASE_URLS=https://llm.surogate.ai:8443/v1   # comma-separate several replicas
export LAB_API_KEY=...                                 # never commit it
export LAB_PYTHON=/path/to/venv/bin/python             # has the product's dependencies + pytest
# Optional: LAB_TOOL_PATH (PATH prefix for the stand-in sandbox: python with
# openpyxl/python-docx/python-pptx/pypdf, node), LAB_NODE_PATH (pptxgenjs, docx).

$LAB_PYTHON run.py \
  --variant base=/path/to/checkout@surogate \
  --variant opt=/path/to/checkout@surogate/Surogate3.7-35B-A3B \
  --samples 3 --out runs/ab1 --concurrency 24
python3 report.py runs/ab1 --vs base
```

A variant is `name=<checkout>[@<session model id>][#nogroups]` (`#nogroups`
renders the checkout without its on-demand tool groups; with them the runner
holds schemas back and releases them on `load_tools` or a direct call, as the
harness loop does). On checkouts from before the prompt became the default,
the model id selects the prompt profile as it did in production
(`surogate/Surogate3.7-35B-A3B` selects `surogate`). One engine is used per conversation, as
the load balancer's source persistence does, so prefix caching behaves as in
production. `rescore.py` re-applies changed checks to finished runs without
calling the model.

`report.py` prints, per variant: mean score, prompt tokens of the first
request (`tok1`), prompt and completion tokens per task, iterations, wall
time, the share of runs that loaded a skill or asked the user, invalid tool
calls and runs that hit the iteration cap; then per-scenario means and, with
`--vs`, the paired difference with a bootstrap 95% CI over scenarios.

## Results behind the default prompt (2026-11-03)

See `RESULTS.md`.
