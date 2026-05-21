# Harness Advisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the executor-visible `consult_expert` tool with hidden harness-controlled advisor calls backed by a generic OpenAI-compatible auxiliary client.

**Architecture:** Add advisor configuration and an auxiliary client builder, then route the existing forced expert hook points through advisor calls that inject focused guidance into the executor-visible message list. Remove only the consult-expert tool exposure path; keep expert skill/admin/training code intact.

**Tech Stack:** Python, Pydantic settings, OpenAI-compatible `AsyncOpenAI`, pytest, existing Surogates session event log.

---

### Task 1: Advisor Configuration And Client

**Files:**
- Modify: `surogates/config.py`
- Modify: `surogates/harness/auxiliary_client.py`
- Test: `tests/test_browser_base.py`

- [ ] **Step 1: Write failing config/client tests**

Add tests asserting advisor defaults, env override for `SUROGATES_LLM_ADVISOR_MODEL`, and `build_advisor_auxiliary_llm()` returning `None` when disabled or unconfigured.

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_browser_base.py::test_llm_settings_advisor_defaults tests/test_browser_base.py::test_llm_settings_advisor_env_override tests/test_browser_base.py::test_build_advisor_auxiliary_llm_requires_enabled_and_model -q`

Expected: fail because fields and builder do not exist.

- [ ] **Step 3: Implement minimal config/client**

Add `advisor_enabled`, `advisor_model`, `advisor_base_url`, `advisor_api_key`, `advisor_max_calls_per_turn`, and `advisor_max_tokens` to `LLMSettings`. Add `build_advisor_auxiliary_llm(settings, tenant=None)` mirroring summary/vision endpoint and credential fallback.

- [ ] **Step 4: Verify tests pass**

Run the same pytest command. Expected: pass.

### Task 2: Advisor Events And Loop Hook

**Files:**
- Modify: `surogates/session/events.py`
- Modify: `surogates/harness/loop.py`
- Test: `tests/test_expert_routing.py`

- [ ] **Step 1: Write failing advisor harness tests**

Rewrite the forced consultation tests to assert `_maybe_consult_required_advisor()` injects `[Advisor guidance: ...]`, emits advisor events, caps duplicate calls, and continues when advisor fails. Add a guard test that the harness does not expose a hard-tool advisor hook.

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_expert_routing.py -q`

Expected: fail because advisor methods/events do not exist and old expert expectations remain.

- [ ] **Step 3: Implement minimal advisor service methods**

Add `ADVISOR_REQUEST`, `ADVISOR_RESULT`, and `ADVISOR_FAILURE` event types. Add advisor state/counter in `_run_loop`, call the advisor before hard tasks, and inject formatted guidance into `messages`.

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_expert_routing.py -q`. Expected: pass.

### Task 3: Remove Consult Expert Tool Exposure

**Files:**
- Modify: `surogates/tools/runtime.py`
- Modify: `surogates/tools/router.py`
- Modify: `surogates/harness/prompt.py`
- Modify: `surogates/harness/prompts/guidance/expert.md` if it becomes unused by tests
- Test: `tests/test_expert.py`

- [ ] **Step 1: Write failing removal tests**

Change tests so `ToolRuntime.register_builtins()` does not register `consult_expert`, `TOOL_LOCATIONS` does not include it, and prompt guidance no longer advertises voluntary expert delegation.

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_expert.py -q`

Expected: fail on the old registration/routing expectations.

- [ ] **Step 3: Remove tool exposure**

Remove the expert module from builtin registration, remove `consult_expert` from `TOOL_LOCATIONS`, and remove consult-expert prompt guidance injection from `PromptBuilder`.

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_expert.py -q`. Expected: pass.

### Task 4: Focused Regression Suite

**Files:**
- Verify only

- [ ] **Step 1: Run focused tests**

Run: `pytest tests/test_browser_base.py tests/test_expert_routing.py tests/test_expert.py tests/test_artifacts.py::test_prompt_includes_tool_gated_guidance -q`

Expected: pass.

- [ ] **Step 2: Inspect diff**

Run: `git -C /work/surogates diff --stat && git -C /work/surogates diff --check`

Expected: scoped changes and no whitespace errors.
