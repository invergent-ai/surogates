# Workspace-Bench Results

Scored against the frozen dev split (70 tasks) unless the size column says
otherwise. "Model served" is the model that actually served the calls:
sessions run under the `surogate` / `surogate-pro` tier sentinels, and the
sentinel is not a model — it resolves per request, so a run is only
comparable to another run that resolved the same way. The judge model is
recorded per run in each task's `scores.json`; a judge change re-baselines
every number, so re-judge the comparison run too.

Strict is passed/total rubrics as judged; Score is the Rubric Pass Rate
(the same ratio as a percentage), the public leaderboard's headline
metric. Not comparable to that leaderboard —
different scaffold by design (see README); rows here are only comparable
to each other. Every counted run records its row **and its failed-task
list** from the run's `report.md` — the failed tasks are the
harness-improvement backlog, and a row without them is just a number.

| Date | Run | Where | Model served | Size | Strict | Score |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-03 | `dev-001` | prod, agent `30397e4f…` | claude-opus-5 (surogate-pro tier) | 70 | 378/1306 | **28.9%** |

**dev-001** — first counted run. Judge: claude-sonnet-5 (OpenRouter
deployment `e3a0fc7b…`), temp 0. Rollout: 54/70 sessions completed, 15
failed, 1 timeout, ~4 h wall at concurrency 3. Pass@50 25.7% /
Pass@60 22.9% / Pass@80 14.3%; by difficulty: easy 44.9%, medium 28.1%,
hard 24.3% rubric accuracy. Two failure structures dominate:

1. **Provider rate-limit kills (15 tasks, every `failed` row below).**
   Two throttle windows on the tier; in each, the error carried the
   exact wait ("rate-limited for N more seconds") yet
   `call_llm_with_retry` (`surogates/harness/llm_call.py:532`) did 3
   fast retries and failed the session — task `131` died over a 6 s
   wait, task `107` after 20 min of work, one step before writing its
   report. Same defect claweval's `general-004` recorded. Top fix on
   the list; these 15 become its regression probe.
2. **Output-spec noncompliance (most `completed` rows below).** Agents
   do the substance but ignore the prescribed deliverable: own
   filenames instead of the required ones, `.md` instead of
   `.docx`/`.doc`/`.pptx`/`.pdf`, own worksheet names/headers/chart
   choices. 43/70 tasks missed at least one expected output by name;
   6 completed sessions produced no files at all. The agent has a docx
   skill and rarely reaches for it — a prompting/skill-routing
   question, not missing tooling.

Also seen once each, in the traces: a sandbox died mid-task
(`sandbox_unavailable`, task `3`, session still reported `completed`)
and a session ended mid-plan with argument-less `read_file` calls right
after two `context.compact` events (task `72`).

Failed tasks (below Pass@60), verbatim from `runs/dev-001/report.md`:

| Task | Difficulty | Rubrics | Status | Why (first signal) |
| --- | --- | --- | --- | --- |
| `107` | hard | 0/25 | failed | missing outputs: Global_Product_Strategy.md |
| `115` | easy | 0/18 | completed | missing outputs: output2.md |
| `116` | hard | 0/21 | completed | missing outputs: output20.md |
| `120` | hard | 0/22 | failed | missing outputs: ScreenShot_summary.md |
| `128` | hard | 0/17 | failed | missing outputs: ST-Raptor_run_commands_and_parameter_guide.md |
| `131` | easy | 0/22 | failed | missing outputs: output.md |
| `192` | hard | 0/17 | completed | missing outputs: Industry_Analysis_Report.md |
| `227` | medium | 0/25 | failed | missing outputs: Data_Security_Improvement_Plan_Document.docx |
| `244` | medium | 0/20 | failed | missing outputs: 2024_year_2_7_monthproduct_version_iteration_analysis_and_documentation_guidelines_recommendations_repo |
| `251` | medium | 0/22 | failed | missing outputs: 2019_Annual_Salary_Analysis_Report.docx |
| `255` | medium | 0/18 | failed | missing outputs: monthly_cash_flow_trend_table.xlsx, 2019_annualbank_deposit_income_expenditure_analysis_report.md |
| `266` | medium | 0/20 | failed | missing outputs: shengye_electric_2024_upstream_downstream_customer_supplier_analysis.docx |
| `274` | medium | 0/20 | failed | missing outputs: monthly_employee_salary_and_production_quality_analysis_report.docx |
| `287` | hard | 0/20 | completed | missing outputs: quarterly_operations_execution_overview_report.md |
| `3` | medium | 0/21 | completed | missing outputs: project_dependency_deduplication_list.md |
| `329` | medium | 0/14 | completed | missing outputs: company-compensation-data-summary-verification-and-optimization-suggestions-list.xlsx |
| `337` | medium | 0/18 | completed | rubrics failed on content |
| `340` | medium | 0/20 | completed | missing outputs: employee-birthday-event-end-to-end-preparation-and-execution-sheet.csv |
| `346` | medium | 0/16 | completed | missing outputs: fixed-assets-end-to-end-management-closed-loop-table.csv |
| `35` | medium | 0/26 | completed | missing outputs: financial-file-permission-control-manual.docx |
| `358` | easy | 0/15 | completed | JudgeError: judge returned empty content (finish_reason='length') |
| `360` | hard | 0/21 | failed | missing outputs: multidimensional_malignant_tumor_analysis_report.pdf |
| `386` | hard | 0/25 | failed | missing outputs: file_relationship_graph.json, strategic_transformation_decision_report.pptx, decision_confirmation_tabl |
| `72` | medium | 0/20 | completed | missing outputs: emergency_end_to_end_operation_manual.doc |
| `78` | medium | 0/20 | completed | rubrics failed on content |
| `95` | hard | 0/12 | completed | missing outputs: system_version_full_lifecycle_iteration_report.doc |
| `152` | hard | 1/20 | completed | missing outputs: question_mark-speech_bubble.png, question_mark-person.png, table-green.png, table-blue.png, question_ma |
| `300` | hard | 1/14 | completed | missing outputs: permission_configuration_table.csv, permission_configuration_guide.md, permission_validation_rules.json |
| `289` | hard | 1/12 | completed | missing outputs: 2015_q1_apparel_ecommerce_operations_overview_analysis_report.md |
| `83` | easy | 2/23 | completed | rubrics failed on content |
| `224` | hard | 2/19 | completed | missing outputs: Guaranteed_Progress.xlsx |
| `354` | hard | 2/19 | completed | missing outputs: 2025-key-administrative-work-implementation-plan-for-second-half-of-year.doc |
| `380` | medium | 3/25 | failed | rubrics failed on content |
| `15` | hard | 2/16 | completed | rubrics failed on content |
| `75` | medium | 3/21 | completed | rubrics failed on content |
| `242` | medium | 2/13 | completed | missing outputs: Traffic_Metrics_Comparison.xlsx |
| `232` | medium | 3/19 | completed | missing outputs: Multi_Company_Investment_Value_Analysis_Report_2024_.docx |

### Smaller runs

Pilots, regression probes and pipeline checks. Too small to read as a
score — they exist to isolate one behaviour, and a 3-task run at 70%
means nothing except that the pipeline works.

| Date | Run | Model served | Size | Strict | Score |
| --- | --- | --- | --- | --- | --- |
| 2026-09-02 | `smoke-001` | surogate-pro | 3 | 33/47 | 70.2% |

**smoke-001** — first end-to-end prod run, agent `30397e4f…`, judged by
claude-sonnet-5 (OpenRouter deployment `e3a0fc7b…`). Staging → prod sessions →
sandbox work → collection → extraction → judging all worked first try;
3/3 sessions completed, zero infra errors. Task 7 (easy) 10/10, task 3
(medium) 18/21, task 15 (hard) 5/16. The rubric misses on 3 and 15
share one shape — right substance, exact output specs ignored (required
table format, worksheet name, prescribed headers/labels, chart
title/orientation) — worth watching in the first dev run; per-rubric
evidence in `runs/smoke-001/tasks/*/scores.json`. The run also caught a
judge-side blind spot — xlsx chart metadata was invisible to
extraction, failing chart rubrics as "no evidence" — fixed in
`extract.py` (charts now reported with type/orientation/title/anchor)
and the run re-judged; aggregate unchanged at 33/47.

## Method notes

- One config change per run, recorded in the row's notes.
- Single-run deltas are provisional; re-run an affected subset 3× before
  believing a fix.
- Judging is decoupled from rollouts: `wsbench judge` re-grades stored
  traces without touching prod, and `--overwrite` re-judges after a
  judge-side fix.
- Holdout (30 tasks) is untouched on purpose. It is the overfitting
  signal and is only run when reporting a final number.
- Reference points from the public leaderboard (different scaffold, full
  workspaces, agentic judge — context, not targets): best published
  agent 68.7% Rubric Pass Rate, human reference 80.7%, cross-agent
  average 47.4%.
