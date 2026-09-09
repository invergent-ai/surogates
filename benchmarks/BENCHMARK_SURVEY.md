# Benchmark suitability survey

Surveyed on 2026-09-09 against the local checkout at `f3afd9ce`, including
the initial harness-evolution controller. Scope: all 11 directories
under `benchmarks`: nine benchmark adapters and two optimization tools.
Evidence: local README files, dataset loaders, runners, graders, split
manifests, installation/artifact inventory, and upstream release metadata.
This is a suitability assessment, not a live benchmark result. No model
calls, service deployments, dataset generation, or benchmark runs were made.

**Implementation follow-up:** the controller now includes EnterpriseOps-Gym,
DABstep and GAIA alongside ClawEval and Workspace-Bench. This change adds a
pinned EnterpriseOps importer, per-task tool/context enforcement, private task
and answer-key snapshots, reserved holdout checks and parameterized tool
profiles. Offline tests cover these boundaries; live fidelity and model
performance have not been measured. Findings below describe the surveyed
baseline and explain the priority order.

**Recommendation:** build the core harness-search suite around
**EnterpriseOps-Gym + ClawEval + Workspace-Bench**, add **DABstep** for
analytical work, and retain **GAIA** as a general-capability regression
check. EnterpriseOps-Gym has the strongest combination of enterprise
relevance, task diversity, state reset, and executable outcome verification,
but its local adapter needs fidelity work before automated optimization.
TheAgentCompany is a strong second-stage addition after fixing its task
lifecycle. Curated Agents' Last Exam tasks can then test expert work.

The ranking below is engineering judgment for our Standard/Pro harness,
not an empirical claim about which benchmark predicts customer success.
Successful tool execution, reliable grading, and representative workflows
matter more than nominal task count. These measurements evaluate the model,
harness, tools, and environment together; a failed task does not by itself
establish that the harness caused the failure.

| Benchmark | Task pool relevant to our adapter | Signal and harness surface | Recommended role | Main limitation before automated search |
| --- | --- | --- | --- | --- |
| [EnterpriseOps-Gym](enterpriseops_gym/README.md) | 1,150 in the full benchmark; **649 public oracle rows** currently; pinned repository sample has 13 tasks | Final database state checked by SQL; real MCP calls, entity lookup, dependent actions, policy-sensitive workflows | **Primary enterprise search suite** after adapter fixes | Import/pin public release, enforce task tool scope, handle task context, validate supported gym/verifier types, provision isolated domain services |
| [ClawEval](claweval/README.md) | Local adapter documents **45 eligible English general tasks**, plus 35 Chinese; upstream advertises 300 across its wider release | Mock service actions through our MCP path; upstream programmatic checks and LLM rubrics; safety/completion | **Core search and regression suite**; expand adapter coverage next | Current adapter excludes sandbox fixtures, simulated-user tasks, and multimodal tasks; judge noise and service setup cost |
| [Workspace-Bench](workspace_bench/README.md) | **100 English Lite tasks: 70 dev / 30 holdout** | Real workspace uploads, terminal/file tools, documents, spreadsheets, presentations, code; per-rubric judging | **Core artifact-quality suite** | Extracted-text judging incompletely measures visual quality and large outputs; freeze judge and dataset; reject grading failures as invalid evaluations |
| [DABstep](dabstep/README.md) | **450 main tasks: 100 dev / 350 holdout**, plus 10 separate public development tasks | Payments-domain data analysis over local CSV/JSON/manual; deterministic answer scoring | **Analytical search supplement** and regression tests | Main-set local references are reconstructed from publicly accepted submissions, not authoritative hidden answers; use a frozen, reviewed key |
| [TheAgentCompany](the_agent_company/README.md) | **175 tasks** in the pinned adapter description | Browser, terminal, GitLab, ownCloud, Plane, RocketChat; task-specific checkpoint grading | **High-value second-stage enterprise evaluation** | Missing automated per-task initialization/reset and immediate grading; heavy shared service stack; task images and evaluator contract need verification |
| [GAIA](gaia/README.md) | **165 validation tasks: 110 dev / 55 holdout** | Web research, browser, files, multi-step reasoning; official strict answer matcher | **General-capability regression check**; targeted research-harness experiments | Smaller and less enterprise-specific; live-web drift and prior optimization exposure; gated data |
| [Agents' Last Exam](agents_last_exam/README.md) | **165 task cards at our PIN**, not 1,500 locally executable tasks | Professional artifact workflows with hidden references and deterministic task graders | **Curated expert-task evaluation** after eligibility/metric work | Gated input/reference archive, domain software, Ubuntu/Windows/GUI differences, incompatible raw point scales |
| [Galileo](galileo/README.md) | **500 scenarios: 100 dev / 400 holdout** | Multi-turn customer-service reasoning; simulated users/tools; LLM-judged action completion and tool selection | **Conversation regression suite**; defer native-tool optimization use | Current adapter uses fenced text tool calls and synthetic result messages, bypassing our native tool router; simulator and judge add noise |
| [ECBench](ecbench/README.md) | Repeated episodes of one e-commerce world, up to **365 simulated days**; not hundreds of independent tasks | Long-horizon terminal use, memory/context retention, persistent plans; final-assets objective | **Occasional long-horizon stress test** after evaluator isolation | Agent can modify simulator and scoring artifacts; scorer trusts collected JSON and loads a collected pickle; full episodes are expensive |

Public size and local readiness are different. The local GAIA and Workspace
directories have virtual environments and run artifacts. GEPA also has
both. The other seven benchmark directories do not have their own `.venv`
or `runs/` at the default paths, and none of the benchmark vendor checkouts
is present there. This does not establish whether datasets or environments
exist elsewhere. The survey did not load environment files, access gated
archives, or certify any live environment. Historical README/results claims
are not proof that the corresponding runtime is installed on this machine.

**Corrections to task counts and release assumptions.**

EnterpriseOps-Gym's public dataset currently contains 649 oracle rows:
Calendar 61, CSM 103, Drive 64, Email 67, HR 102, Hybrid 88, ITSM 103,
Teams 61. Thus there are 561 non-hybrid rows before compatibility checks;
that is not a claim that all 561 are runnable. The three distractor-tool
configurations each contain 637 rows. They are related tool-set variants,
not automatically additional independent tasks. Group variants by original
task before partitioning. Upstream describes the public release as 60% of
the benchmark; use the observed published row counts rather than assuming
all 1,150 tasks can be downloaded. This corrects our local README's full-set
availability assumption. Sources: [upstream repository](https://github.com/ServiceNow/EnterpriseOps-Gym),
[dataset card](https://huggingface.co/datasets/ServiceNow-AI/EnterpriseOps-Gym),
and [dataset size metadata](https://datasets-server.huggingface.co/size?dataset=ServiceNow-AI%2FEnterpriseOps-Gym).

The pinned EOG repository tree contains 13 sample task JSONs, and the pinned
ALE tree contains 165 task cards. These counts were verified from GitHub tree
metadata without opening task answers. Sources:
[EOG pinned tree](https://api.github.com/repos/ServiceNow/EnterpriseOps-Gym/git/trees/271f2c357f763376997dfd16807fcde2474ae41b?recursive=1),
[ALE pinned tree](https://api.github.com/repos/rdi-berkeley/agents-last-exam/git/trees/d10fb61a14f9719774c3520c5763068b28ef5546?recursive=1).
ALE's current website advertises 1,500+ collected tasks, while its current
framework README describes around 150 public tasks. Neither number is a
count of tasks eligible for our file-based sandbox adapter.
[ALE project](https://agents-last-exam.org/),
[ALE framework](https://github.com/rdi-berkeley/agents-last-exam).

Claw's current upstream release advertises 300 tasks and Pass^3 evaluation;
our local adapter covers a much smaller subset and the new controller uses
paired repeated comparisons, not the public Pass^3 metric. Workspace-Lite
has 100 tasks per language; our adapter uses English. DABstep publishes 450
main tasks plus 10 development tasks. Galileo's 500 scenarios are in
`adaptive_tool_use`; persona and tool-schema rows are not extra scenarios.
[Claw upstream](https://github.com/claw-eval/claw-eval),
[Workspace-Lite card](https://huggingface.co/datasets/Workspace-Bench/Workspace-Bench-Lite),
[DABstep card](https://huggingface.co/datasets/adyen/DABstep),
[Galileo card](https://huggingface.co/datasets/galileo-ai/agent-leaderboard-v2).

**Source findings that change the integration order.**

1. **EnterpriseOps-Gym needs more than a controller adapter.**
   [The loader](enterpriseops_gym/eogbench/dataset.py) expects local
   `<domain>/task_*.json` files and exactly one gym per task; it does not
   directly import the public Hugging Face configuration/split format.
   It parses `selected_tools` and `restricted_tools`, but
   [the runner](enterpriseops_gym/eogbench/runner.py) and
   [proxy](enterpriseops_gym/eogbench/proxy.py) do not apply those lists.
   Consequently the current adapter does not faithfully implement the
   published oracle/distractor tool-set conditions. The parser also omits
   per-gym context/user information; verify required identity/header behavior
   against the chosen fixtures. The runner does create a fresh database,
   verify it, and attempt deletion for each task, which is a useful foundation.
   Inventory actual gym counts and verifier types rather than equating a
   domain label with supported execution. Keep verifier exceptions distinct
   from failed business conditions. Freeze task files, seed databases, vendor
   code, and container image digests.

2. **TheAgentCompany's current lifecycle is unsuitable for repeated selection.**
   [The task loop](the_agent_company/tacbench/runner.py) sequentially creates
   sessions against shared services but contains no reset or task-init hook.
   [The CLI](the_agent_company/tacbench/cli.py) grades in a separate command
   after rollout; graders can then observe state changed by subsequent tasks.
   Sequential execution alone does not fix that. Implement
   `reset → initialize task/NPC/input workspace → run → grade → teardown`
   as one unit. Upstream explicitly requires task initialization and its
   evaluation entrypoint; our [grader](the_agent_company/tacbench/grade.py)
   directly imports an evaluator in an image tagged `latest`, so verify that
   contract against the PIN and pin the images too. Upstream also supports
   LLM-based evaluators/NPCs: do not assume every checkpoint is judge-free.
   [Upstream execution contract](https://github.com/TheAgentCompany/TheAgentCompany).

3. **ECBench's deterministic score is not an isolated optimization reward.**
   [The scorer](ecbench/ecbench/scorer.py) reads `final_assets` from the
   agent-writable `final_state.json`; its fallback calls `pickle.load` on
   the collected `sim_state.pkl`. The simulator itself is staged into the
   agent's writable workspace. An optimizer could improve the reported
   number without improving store management, and the pickle fallback can
   execute code on the evaluator host. Before using this as a reward, move
   authoritative simulation state/scoring outside candidate write access
   and replace the unsafe artifact boundary. Independently, repetitions of
   one deterministic world are not a diverse task corpus. Use short-horizon
   diagnostic episodes first; reserve full-year tests for occasional checks.
   [Local execution design](ecbench/README.md),
   [upstream simulation](https://github.com/QwenLM/E-CommerceBench).

4. **DABstep has a useful but approximate local oracle.**
   [The key builder](dabstep/dabbench/answers.py) chooses modal answers from
   scorer-accepted public submissions. This supports reproducible internal
   comparisons, but it is not an independent authoritative label set for
   the 450 main tasks. Hash the key and its provenance, review disputed
   references, and check successful trajectories actually compute results
   from the supplied data. Do not expose the key or submission archive to
   the candidate/proposer. The 10 public development tasks provide a small
   authoritative smoke set. The shared payments corpus also means a task-ID
   holdout tests new questions on familiar data, not a new business dataset.

5. **Galileo currently optimizes a different tool protocol.**
   [The adapter protocol](galileo/galbench/protocol.py) embeds tool schemas
   in a user prompt, parses fenced `tool_call` text, and sends simulated
   results as subsequent chat messages. This can test turn-taking and
   clarification, but cannot validate our native schema binding, tool
   execution, MCP routing, or tool-result handling. Do not mix its traces
   directly into native-tool SFT data. A native/MCP adapter with fixed
   simulator and judge configurations would substantially improve its fit.

6. **Workspace and ALE need artifact-aware grading boundaries.**
   Workspace's [extractor](workspace_bench/wsbench/extract.py) includes
   text, some chart metadata, and truncation limits; it is not a rendered
   visual evaluator. Use it for content/process gains, supplement visual
   or layout-dependent rubrics with suitable artifact checks, and prevent
   agent-produced text from becoming grader instructions. ALE's
   [eligibility gate](agents_last_exam/alebench/dataset.py) checks files,
   upload limits, prompts, and grader availability but does not certify
   the task's required OS/software fidelity. Curate a compatible subset.
   [Its report](agents_last_exam/alebench/report.py) counts positive scores
   on task-specific scales; `score > 0` is not a completion criterion.
   Establish per-task score maxima or success thresholds before aggregation.

**How to apply the survey to Standard and Pro.**

Use EOG, Claw, and Workspace as complementary objectives: business state,
workflow reliability, and delivered artifacts. Add DABstep where analytics
is a product priority. Sample within benchmark/domain/failure family, and
report per-benchmark and per-family results so a large EOG pool cannot hide
a Workspace regression. Freeze the same model binding, reasoning settings,
and benchmark-specific tool profile for each baseline/candidate pair.
MCP-only, file-capable, and browser-capable suites need different tool
profiles; changing them during comparison confounds the result.

Start by certifying task eligibility and scorer health on a small pilot;
then choose disjoint search, selection, and final holdout groups. Keep
related EOG tool-set variants and translated/templated variants together.
Preserve existing reservations: GAIA 55, Workspace 30, DABstep 350, and
Galileo 400. Already-inspected or training-used tasks cannot become a fresh
holdout through renaming. GAIA's dev tasks have also been used by local GEPA
experiments. Repeated candidate selection consumes development data even
when weights remain frozen; final evaluation must stay separate.

For later SFT/RL, prioritize verified EOG/Claw actions, checked Workspace
deliverables, and reviewed DABstep solutions. Keep original task provenance,
tool schemas, model identity, full traces, and grader version. Passing a
benchmark is useful filtering evidence, not proof every intermediate action
is desirable training behavior. Expert ALE and TAC traces become attractive
after their execution/grading gaps are addressed. Dataset access conditions
also carry through trace handling: GAIA explicitly restricts public
redistribution of its gated task content. This survey does not determine
training permissions for every dataset or teacher model.
[GAIA access conditions](https://huggingface.co/datasets/gaia-benchmark/GAIA).

**The two remaining directories are optimizers, not extra task pools.**

| Directory | Existing role | Consequence |
| --- | --- | --- |
| [gepa](gepa/README.md) | Optimizes one harness prompt fragment using selected GAIA tasks | Reuse lessons about interleaved controls, noisy tasks, and protected holdouts; do not count GAIA again as new data |
| [harness_evolution](harness_evolution/README.md) | New bounded controller for frozen Standard/Pro weights, scoped patches, paired selection, and final holdout | Initially Claw/Workspace; implementation follow-up adds EnterpriseOps, DABstep and GAIA, with live validation pending |

The next implementation order should be: certify and integrate EOG;
retain Claw/Workspace coverage; add the DABstep and GAIA adapters with their
existing holdouts; repair TAC's lifecycle; curate ALE. Revisit Galileo after
native tool binding and ECBench after separating agent output from
authoritative simulation state. Environment and deployed model IDs remain
parameters, as requested.
