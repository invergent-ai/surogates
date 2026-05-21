# Harness Advisor Design

## Scope

Replace the harness's current consult-expert execution path with a hidden,
harness-controlled advisor pass. The advisor is a generic OpenAI-compatible
auxiliary LLM client configured similarly to the existing summary and vision
auxiliary clients. GLM5.1 remains the executor model.

This does not remove the expert skill/admin/training product surface. It only
removes `consult_expert` from the agent's available tools and replaces the
harness-forced expert consultations in the agent loop.

## Goals

- Improve complex agent behavior while keeping GLM5.1 as the main model.
- Keep advisor usage predictable and bounded by harness policy.
- Hide the advisor from the executor so GLM does not need to choose when to
  call it.
- Preserve existing agent loop durability: advice must be persisted through
  normal session events or reconstructable from them.

## Configuration

Add advisor fields to `LLMSettings`:

- `advisor_enabled: bool = False`
- `advisor_model: str = ""`
- `advisor_base_url: str = ""`
- `advisor_api_key: str = ""`
- `advisor_max_calls_per_turn: int = 2`
- `advisor_max_tokens: int = 700`

The advisor client resolves in the same order as summary and vision clients:
user preference for model where appropriate, org config, then global config.
Endpoint and credentials remain org/operator controlled and fall back to the
main LLM endpoint/key when an advisor model is configured without dedicated
advisor credentials.

## Loop Behavior

The harness controls advisor timing. The executor never sees an `advisor` tool.

Advisor calls are considered at these points:

1. Early hard-task guidance, after context reconstruction and before the first
   substantive LLM iteration.
2. Optional final guidance for multi-step turns after tool execution, before
   declaring the turn complete, if the per-turn advisor budget allows it.

The advisor receives the system prompt, recent conversation, and a short
instruction to provide focused strategic guidance. The response is injected back
into the executor-visible message list as a hidden system/user-style guidance
block:

`[Advisor guidance]\n...\n\nUse this as strategic guidance. Verify with tools
where appropriate and adapt if direct evidence contradicts it.`

The injection is not exposed as a callable tool result.

## Replacing Consult Expert

Remove the `consult_expert` tool from:

- builtin tool registration
- tool routing metadata
- prompt guidance triggered by available tools
- tool schema exposure to the executor

Replace the harness methods that currently force expert consultations with
advisor equivalents. Existing expert skill definitions, admin routes, training
collector code, feedback routes, and expert metadata remain in place for a
separate product decision.

## Events And Observability

Emit advisor events so the feature is auditable and cost tracking can be added
without scraping executor prompts:

- `advisor.request`
- `advisor.result`
- `advisor.failure`

Each event includes the advisor model, reason (`early` or `final_check`),
iteration where available, success/failure details, and token usage when
returned by the provider.

## Error Handling

Advisor failures are non-fatal. If the advisor times out, rate-limits, returns
empty content, or errors, the harness emits `advisor.failure` and continues with
the GLM executor.

Advisor calls must obey the per-turn cap. Once the cap is reached, the harness
does not call the advisor again for that turn.

## Tests

Add focused tests for:

- `LLMSettings` advisor defaults and environment overrides.
- auxiliary advisor client construction and fallback behavior.
- `consult_expert` not being registered as a builtin tool.
- prompt guidance no longer advertising expert consultation to the executor.
- harness early advisor injection on hard tasks.
- hard tool proposals do not trigger a separate advisor call.
- advisor failures continuing the executor path without crashing the session.
