# Hermes Tier 1 Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the Tier 1 production-blocking Hermes hardening gaps into Surogates without weakening Surogates' existing harness/sandbox boundary.

**Architecture:** Keep Surogates' existing `harness/` and `tools/` split, and port Hermes logic into small modules that are called from the current loop rather than copying Hermes' monolithic `run_agent.py`. Centralize retry classification, redaction, schema sanitization, streaming scrubbers, tool argument repair, stream timeout policy, mid-stream retry, and per-turn tool guardrails behind focused APIs with regression tests for each production bug listed in `docs/architecture/hermes-drift-analysis.md`.

**Tech Stack:** Python 3.12, pytest, pytest-asyncio, OpenAI-compatible client abstractions, Surogates session event store, tool registry, harness loop, Hermes source files under `study/hermes-agent/`.

---

## Source And Target Map

Hermes sources to port:
- `study/hermes-agent/agent/error_classifier.py`
- `study/hermes-agent/agent/redact.py`
- `study/hermes-agent/agent/tool_guardrails.py`
- `study/hermes-agent/agent/think_scrubber.py`
- `study/hermes-agent/agent/memory_manager.py` (`StreamingContextScrubber`)
- `study/hermes-agent/tools/schema_sanitizer.py`
- `study/hermes-agent/run_agent.py` sections for `_repair_tool_call_arguments`, partial tool-call detection, stale timeout scaling, mid-stream retry, and response surrogate sanitization

Surogates targets:
- Create: `surogates/harness/error_classifier.py`
- Modify: `surogates/harness/error_classify.py`
- Modify: `surogates/harness/llm_call.py`
- Modify: `surogates/harness/loop.py`
- Modify: `surogates/harness/reasoning.py`
- Modify: `surogates/harness/sanitize.py`
- Modify: `surogates/harness/tool_exec.py`
- Create: `surogates/harness/redact.py`
- Create: `surogates/harness/tool_guardrails.py`
- Create: `surogates/harness/stream_scrubbers.py`
- Create: `surogates/tools/schema_sanitizer.py`
- Modify: `surogates/tools/registry.py`
- Modify: `surogates/tools/mcp/client.py`
- Modify: `surogates/logging_config.py`
- Modify: `surogates/session/store.py`

New or expanded tests:
- Create: `tests/test_error_classifier_tier1.py`
- Modify: `tests/test_error_classify.py`
- Create: `tests/test_redact.py`
- Create: `tests/test_schema_sanitizer.py`
- Modify: `tests/test_tool_schemas.py`
- Create: `tests/test_stream_scrubbers.py`
- Modify: `tests/test_stream_stall.py`
- Create: `tests/test_tool_arg_repair.py`
- Create: `tests/test_tool_guardrails.py`
- Create: `tests/test_midstream_retry.py`
- Modify: `tests/test_harness_resilience.py`

---

### Task 1: Secret Redaction

**Files:**
- Create: `surogates/harness/redact.py`
- Modify: `surogates/logging_config.py`
- Modify: `surogates/session/store.py`
- Test: `tests/test_redact.py`

- [ ] **Step 1: Write failing redaction tests**
  Cover vendor API keys, `TOKEN=...` env-style assignments, URL query secrets, URL userinfo, JWTs, DB connection strings, private-key blocks, nested dict/list event payloads, and logging exception text.

- [ ] **Step 2: Run tests to verify failure**
  Run: `pytest tests/test_redact.py -q`
  Expected: FAIL because `surogates.harness.redact` and event/log redaction hooks do not exist.

- [ ] **Step 3: Port Hermes redaction module**
  Port `redact_sensitive_text`, `redact_sensitive_data`, and `RedactingFormatter` from `study/hermes-agent/agent/redact.py` into `surogates/harness/redact.py`. Preserve always-on production redaction; expose a test-only override via function argument rather than a runtime opt-out.

- [ ] **Step 4: Wire event and logging redaction**
  In `SessionStore.emit_event`, store `redact_sensitive_data(data)` instead of raw `data`. In `logging_config.StructuredFormatter.format`, redact `record.getMessage()` and formatted exception text before JSON serialization. Keep trace fields unchanged.

- [ ] **Step 5: Verify redaction**
  Run: `pytest tests/test_redact.py tests/integration/test_audit_log.py -q`
  Expected: PASS, and no test snapshot contains raw secret values.

### Task 2: Schema Sanitizer

**Files:**
- Create: `surogates/tools/schema_sanitizer.py`
- Modify: `surogates/tools/registry.py`
- Modify: `surogates/tools/mcp/client.py`
- Modify: `surogates/harness/tool_schemas.py`
- Test: `tests/test_schema_sanitizer.py`
- Test: `tests/test_tool_schemas.py`

- [ ] **Step 1: Write failing sanitizer tests**
  Cover top-level `anyOf`/`oneOf`/`allOf` removal, `type: ["string", "null"]` collapse, nullable union collapse, object nodes with missing `properties`, primitive string schema nodes, and orphaned `required` entries.

- [ ] **Step 2: Run tests to verify failure**
  Run: `pytest tests/test_schema_sanitizer.py tests/test_tool_schemas.py -q`
  Expected: FAIL because exported schemas still contain provider-hostile constructs.

- [ ] **Step 3: Port sanitizer**
  Port `sanitize_tool_schemas`, `strip_nullable_unions`, and recursive node normalization from `study/hermes-agent/tools/schema_sanitizer.py` into `surogates/tools/schema_sanitizer.py`.

- [ ] **Step 4: Wire schema export**
  Call `sanitize_tool_schemas` from `ToolRegistry.get_schemas` before returning OpenAI-format schemas. Ensure MCP-loaded tool schemas pass through the same registry export path, or sanitize in `surogates/tools/mcp/client.py` immediately before registration if MCP bypasses `get_schemas`.

- [ ] **Step 5: Verify schema compatibility**
  Run: `pytest tests/test_schema_sanitizer.py tests/test_tool_schemas.py tests/test_coerce.py -q`
  Expected: PASS with input schemas left unmutated unless a sanitized copy is returned.

### Task 3: Streaming Think And Memory-Context Scrubbers

**Files:**
- Create: `surogates/harness/stream_scrubbers.py`
- Modify: `surogates/harness/llm_call.py`
- Modify: `surogates/harness/reasoning.py`
- Modify: `surogates/memory/manager.py`
- Test: `tests/test_stream_scrubbers.py`
- Test: `tests/test_stream_stall.py`

- [ ] **Step 1: Write failing scrubber tests**
  Cover split tags across chunks for `<think>`, `<thinking>`, `<reasoning>`, `<thought>`, `<REASONING_SCRATCHPAD>`, and `<memory-context>`. Include cases where normal user-visible text mentions a tag mid-sentence and must not be suppressed.

- [ ] **Step 2: Run tests to verify failure**
  Run: `pytest tests/test_stream_scrubbers.py -q`
  Expected: FAIL because only complete-string regex stripping exists.

- [ ] **Step 3: Port scrubbers**
  Port Hermes `StreamingThinkScrubber` from `agent/think_scrubber.py` and `StreamingContextScrubber` from `agent/memory_manager.py` into `surogates/harness/stream_scrubbers.py`. Keep one-shot helpers in `reasoning.py` and `memory/manager.py` as wrappers for complete messages.

- [ ] **Step 4: Wire streaming deltas**
  Instantiate fresh scrubbers inside each `call_llm_streaming_inner` call. Apply the think scrubber before appending/emitting text deltas, and apply the memory-context scrubber before emitting user-visible deltas. Keep provider-native `reasoning_content` deltas in `reasoning_parts`, but do not emit scrubbed reasoning as normal content.

- [ ] **Step 5: Verify stream behavior**
  Run: `pytest tests/test_stream_scrubbers.py tests/test_stream_stall.py tests/test_midstream_interrupt.py -q`
  Expected: PASS, with no leaked reasoning or memory-context text when boundaries are split across chunks.

### Task 4: JSON Tool Argument Repair And Truncation Safety

**Files:**
- Modify: `surogates/harness/tool_exec.py`
- Modify: `surogates/harness/loop.py`
- Modify: `surogates/harness/llm_call.py`
- Test: `tests/test_tool_arg_repair.py`
- Modify: `tests/test_harness_resilience.py`

- [ ] **Step 1: Write failing tool-argument tests**
  Cover trailing commas, unescaped tabs/control characters, one missing closing brace, one extra closing brace, truncated arguments with `finish_reason == "tool_calls"`, and invalid arguments for high-impact tools such as `write_file` and `terminal`.

- [ ] **Step 2: Run tests to verify failure**
  Run: `pytest tests/test_tool_arg_repair.py tests/test_harness_resilience.py -q`
  Expected: FAIL because malformed JSON falls back to `{}` or is handled as a generic invalid tool call.

- [ ] **Step 3: Port repair helper**
  Move Hermes `_repair_tool_call_arguments` logic from `run_agent.py` into `surogates/harness/tool_exec.py` as `repair_tool_call_arguments(raw_args: str, tool_name: str) -> str`. Use strict parsing first, then repair control characters, trailing commas, unclosed braces/brackets, excess braces/brackets, and tabs.

- [ ] **Step 4: Stop executing partial tool calls**
  In `llm_call.py`, set `usage_data["partial_tool_call"] = True` when streaming ends with `finish_reason == "tool_calls"` and any accumulated tool argument is structurally incomplete. In `loop.py`, if `partial_tool_call` is true, append the assistant message and synthetic tool errors that ask the model to retry with complete arguments; do not execute any partial tool call.

- [ ] **Step 5: Replace `{}` fallback**
  In `execute_single_tool`, parse repaired arguments. If repair still fails, return a tool result containing `{"error": "Invalid JSON arguments: ..."}` and skip dispatch. Do not coerce or execute `{}` for malformed arguments.

- [ ] **Step 6: Verify safety**
  Run: `pytest tests/test_tool_arg_repair.py tests/test_harness_resilience.py tests/test_streaming_executor.py -q`
  Expected: PASS, with malformed `write_file` and `terminal` calls never dispatched with empty arguments.

### Task 5: Central Error Classifier And Retry Decisions

**Files:**
- Create: `surogates/harness/error_classifier.py`
- Modify: `surogates/harness/error_classify.py`
- Modify: `surogates/harness/llm_call.py`
- Modify: `surogates/harness/resilience.py`
- Modify: `surogates/harness/credentials.py`
- Test: `tests/test_error_classifier_tier1.py`
- Test: `tests/test_error_classify.py`
- Test: `tests/test_harness_resilience.py`
- Test: `tests/test_credential_pool.py`

- [ ] **Step 1: Write failing classifier tests**
  Cover all Tier 1 bug cases from the drift analysis: Anthropic OAuth 1M-context beta forbidden, llama.cpp grammar rejection, 402 transient usage cap versus credits exhausted, SSL transient alerts versus server disconnect, server disconnect on large sessions as context overflow, bare Anthropic 400 as context overflow when the session is large, OpenRouter `metadata.raw` nested upstream errors, and Bedrock/Alibaba 429 patterns such as `throttlingexception` and `servicequotaexceededexception`.

- [ ] **Step 2: Run tests to verify failure**
  Run: `pytest tests/test_error_classifier_tier1.py tests/test_error_classify.py tests/test_harness_resilience.py -q`
  Expected: FAIL because retry/compress/rotate/fallback decisions are currently split across string matches.

- [ ] **Step 3: Port classifier types and rules**
  Port `FailoverReason`, `ClassifiedError`, `classify_api_error`, `extract_error_context`, and supporting pattern tables from `study/hermes-agent/agent/error_classifier.py` into `surogates/harness/error_classifier.py`. Keep Surogates-specific UI categories separate.

- [ ] **Step 4: Use classifier in retry loop**
  In `call_llm_with_retry`, replace ad hoc status/message branches with `classify_api_error(...)`. Use `retryable` for backoff, `should_compress` for context compression, `should_rotate` for credential rotation, and `should_fallback` for fallback activation. Keep existing thinking-signature recovery as a classifier reason rather than a separate branch.

- [ ] **Step 5: Keep UI projection stable**
  Refactor `error_classify.py` so `classify_harness_error` maps `ClassifiedError.reason` to existing frontend categories (`rate_limit`, `auth_failed`, `context_overflow`, `network`, `provider_error`, `timeout`, `invalid_response`). Preserve current `ErrorInfo` fields and existing tests.

- [ ] **Step 6: Verify retry behavior**
  Run: `pytest tests/test_error_classifier_tier1.py tests/test_error_classify.py tests/test_harness_resilience.py tests/test_credential_pool.py -q`
  Expected: PASS, and retry decisions come from one classifier path.

### Task 6: Stale Timeout Scaling And Mid-Stream Tool Retry

**Files:**
- Modify: `surogates/harness/llm_call.py`
- Modify: `surogates/harness/loop.py`
- Modify: `surogates/harness/model_metadata.py`
- Test: `tests/test_stream_stall.py`
- Test: `tests/test_midstream_retry.py`

- [ ] **Step 1: Write failing timeout and retry tests**
  Cover default 180s stale timeout, 240s+ timeout for medium contexts, 300s+ timeout for >100k tokens, disabled stale timeout for local endpoints, and silent retry when a transient stream error occurs after at least one tool-call name has streamed but before complete arguments arrive.

- [ ] **Step 2: Run tests to verify failure**
  Run: `pytest tests/test_stream_stall.py tests/test_midstream_retry.py -q`
  Expected: FAIL because timeout is a constant and partial tool-call stream drops end the call.

- [ ] **Step 3: Add timeout policy**
  Add `compute_stream_stale_timeout(messages, base_url, model, explicit_timeout=None) -> float` in `llm_call.py` or `model_metadata.py`. Return `float("inf")` for local endpoints when the timeout is implicit, at least 300 seconds for requests above 100k estimated tokens, at least 240 seconds for large-but-under-100k requests, and the explicit configured timeout unchanged.

- [ ] **Step 4: Track partial tool names**
  During streaming accumulation, maintain `partial_tool_names` whenever a tool name is present but arguments are incomplete. Return it in `usage_data` together with any stream exception classification.

- [ ] **Step 5: Implement silent retry**
  In `call_llm_with_retry`, when the classifier marks a stream failure retryable and `partial_tool_names` is non-empty, close and replace the request client, emit a reconnect `llm.delta`, discard any streaming executor work for that response, and retry without appending the broken assistant message.

- [ ] **Step 6: Verify stream resilience**
  Run: `pytest tests/test_stream_stall.py tests/test_midstream_retry.py tests/test_streaming_executor.py -q`
  Expected: PASS, with partial tool-call stream drops retried silently and local endpoints no longer killed by the 180s default.

### Task 7: Tool Guardrails

**Files:**
- Create: `surogates/harness/tool_guardrails.py`
- Modify: `surogates/harness/loop.py`
- Modify: `surogates/harness/tool_exec.py`
- Test: `tests/test_tool_guardrails.py`

- [ ] **Step 1: Write failing guardrail tests**
  Cover repeated exact failures, repeated same-tool failures with different args, repeated idempotent read results, warning injection after two repeats, and hard halt after the configured threshold.

- [ ] **Step 2: Run tests to verify failure**
  Run: `pytest tests/test_tool_guardrails.py -q`
  Expected: FAIL because no per-turn guardrail state exists.

- [ ] **Step 3: Port guardrail module**
  Port `ToolGuardrailConfig`, `ToolCallSignature`, `ToolGuardrailDecision`, `ToolGuardrails`, `canonical_tool_args`, and result hashing from `study/hermes-agent/agent/tool_guardrails.py` into `surogates/harness/tool_guardrails.py`.

- [ ] **Step 4: Wire before and after hooks**
  Create one `ToolGuardrails` instance per harness turn in `loop.py`. Call `before_call` before dispatch; if it returns a halt/block decision, append a synthetic tool result and stop executing the remaining repeated calls. Call `after_call` after each tool result; if it returns a warning, inject the guidance into the next model-visible tool result.

- [ ] **Step 5: Preserve event semantics**
  Emit normal `tool.call` and `tool.result` events for guardrail-generated responses so replay and audit stay consistent. Include a machine-readable `guardrail` object in the tool result JSON with `code`, `action`, and `count`.

- [ ] **Step 6: Verify loop control**
  Run: `pytest tests/test_tool_guardrails.py tests/test_harness_pending.py tests/test_budget.py -q`
  Expected: PASS, with loops stopped before exhausting the session budget.

### Task 8: Final Tier 1 Verification

**Files:**
- Modify: `surogates/harness/error_classifier.py`
- Modify: `surogates/harness/error_classify.py`
- Modify: `surogates/harness/llm_call.py`
- Modify: `surogates/harness/loop.py`
- Modify: `surogates/harness/redact.py`
- Modify: `surogates/harness/sanitize.py`
- Modify: `surogates/harness/stream_scrubbers.py`
- Modify: `surogates/harness/tool_exec.py`
- Modify: `surogates/harness/tool_guardrails.py`
- Modify: `surogates/tools/registry.py`
- Modify: `surogates/tools/schema_sanitizer.py`

- [ ] **Step 1: Run focused Tier 1 suite**
  Run: `pytest tests/test_redact.py tests/test_schema_sanitizer.py tests/test_stream_scrubbers.py tests/test_tool_arg_repair.py tests/test_error_classifier_tier1.py tests/test_stream_stall.py tests/test_midstream_retry.py tests/test_tool_guardrails.py -q`
  Expected: PASS.

- [ ] **Step 2: Run adjacent harness/tool suite**
  Run: `pytest tests/test_error_classify.py tests/test_harness_resilience.py tests/test_retry.py tests/test_streaming_executor.py tests/test_tool_schemas.py tests/test_coerce.py tests/test_credential_pool.py tests/test_sanitize.py -q`
  Expected: PASS.

- [ ] **Step 3: Run static diff checks**
  Run: `git diff --check`
  Expected: no whitespace errors.

- [ ] **Step 4: Confirm Tier 1 coverage**
  Check each Tier 1 bullet in `docs/architecture/hermes-drift-analysis.md` against the tests above:
  `error_classifier.py`, `redact.py`, `tool_guardrails.py`, `schema_sanitizer.py`, streaming think/context scrubbers, mid-stream retry with tool call in flight, stale-stream timeout scaling, and JSON tool-arg repair all have a targeted test and a Surogates integration point.

---

## Implementation Notes

- Keep the Surogates tool router. Hermes dispatch is intentionally not ported because Surogates' `HARNESS`/`SANDBOX` split is part of the untrusted-code boundary.
- Prefer direct ports for pure functions and small classes, but adapt integration code to Surogates' session/event/lease model.
- Preserve current user-facing `ErrorInfo` categories; the new classifier is the retry/failover decision engine, while `error_classify.py` remains the UI projection.
- Do not execute malformed or partial tool calls. Returning a structured tool error is safer than coercing `{}`.
- Redaction must happen before persistence and before stderr logging. Tests should assert the raw secret is absent, not only that a replacement marker exists.
- Run focused tests after each task and commit each task independently when executing this plan.

## Self-Review

- Spec coverage: all eight Tier 1 items from the drift analysis map to Tasks 1-7, with Task 8 verifying coverage explicitly.
- Placeholder scan: this plan contains no deferred implementation placeholders; each task names source files, target files, tests, commands, and expected outcomes.
- Type consistency: new classifier APIs are named `ClassifiedError`, `FailoverReason`, and `classify_api_error`; redaction APIs are `redact_sensitive_text` and `redact_sensitive_data`; guardrail APIs follow Hermes names and are referenced consistently.
