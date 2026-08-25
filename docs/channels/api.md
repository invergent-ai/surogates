# API Channel

The API channel is how software talks to an agent. It has two shapes:

- **OpenAI-compatible chat completions** — point any OpenAI client or SDK at the agent and it works. This is the surface most integrations want.
- **Fire-and-forget prompt submission** — for pipelines that submit thousands of prompts and sweep results out of the database later.

Both authenticate with the same API key and run against the same agent. Neither is a lesser agent: a request runs the full thing, with its skills, tools, memory, workspace, and browser.

## OpenAI-compatible endpoint

Point the client at the agent's own URL plus `/v1/api`:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://profesor-romana-o6eskd.cloud.surogate.ai/v1/api",
    api_key="surg_sk_...",
)

response = client.chat.completions.create(
    model="profesor-romana-o6eskd",
    messages=[{"role": "user", "content": "Care e capitala României?"}],
)
print(response.choices[0].message.content)
```

`GET /v1/api/models` advertises the agent as a single model, named after its slug — the same name that appears in its hostname. Use it to check the endpoint is reachable before sending traffic.

The `/v1/api` suffix is not decoration. `/v1/api/*` is the only path where an API key is accepted, so the `/chat/completions` an OpenAI client appends lands inside the existing auth boundary.

### What works

| | |
|---|---|
| **Text** | `chat.completions.create(...)` |
| **Streaming** | `stream=True` — content arrives as `chat.completion.chunk` deltas, terminated by `[DONE]` |
| **Reasoning** | streamed as `delta.reasoning_content`, and present as `message.reasoning_content` on a buffered response |
| **Images** | `image_url` content parts, either a `data:` URL or an `http(s)` URL the server fetches |
| **Usage** | real token counts, including `prompt_tokens_details.cached_tokens` and `completion_tokens_details.reasoning_tokens` |
| **Skills and tools** | every skill, tool, MCP server and browser the agent has |

### What is deliberately not supported

**Client-declared `tools`.** Passing `tools` or `functions` returns a `400` with code `tools_not_supported`. The agent runs its own tools inside the turn and returns the final answer; a tool call sent back to the client would wait forever for a `role: "tool"` reply nothing is listening for. The refusal is loud on purpose — silently ignoring the field would present as a hang.

**Sampling parameters.** `temperature`, `top_p`, `max_tokens`, `seed` and friends are accepted and ignored. An agent resolves its own model and generation settings; honouring the caller's would serve something other than the configured agent.

**The agent's intermediate tool calls** never appear as OpenAI `tool_calls`, for the same reason.

### Errors

Errors use the envelope OpenAI SDKs parse, not FastAPI's `{"detail": ...}`:

```json
{"error": {"message": "...", "type": "invalid_request_error", "param": null, "code": "tools_not_supported"}}
```

| Status | Meaning |
|---|---|
| `400` | malformed request, unsupported content part, client-declared tools |
| `401` | missing credential |
| `403` | wrong principal kind, or a key bound to a different agent |
| `402` | the agent's allowance is exhausted, or the buyer's package excludes API access |
| `422` | the message tripped the prompt-injection screen |
| `502` | the agent failed the turn, or completed without producing an answer |
| `504` | a non-streaming turn outran its budget — retry with `stream: true` |

A failed or empty turn is always an error, never a `200` carrying an empty string: a caller cannot tell that from a deliberate empty answer and would record it as the agent's reply.

## Conversations

Chat completions are stateless — the client resends the whole `messages` array each call. An agent is not: a session carries memory, a workspace, a live browser. So the endpoint keeps one session per conversation and appends to it, which is what makes a follow-up cheap and gives the agent everything it had last turn.

Send the history as you normally would and the conversation continues in place. Two response headers tell you what happened:

| Header | Meaning |
|---|---|
| `X-Surogate-Session` | the session this turn ran in |
| `X-Surogate-Conversation-Action` | `create`, `append`, or `fork` |
| `X-Surogate-Conversation-Fork-Reason` | why, when the action was `fork` |

### Rewriting history forks

Appending is only sound while the client appends. Regenerate, edit the last message, branch, or trim your own context, and the session would otherwise accumulate the debris — the stale question *and* its answer stay in the agent's context, and it answers with them still in view.

So each request is reconciled against the session's transcript. A clean prefix appends; anything else forks a fresh session seeded with the history you actually sent. That is normal traffic, not an error — it is what makes "regenerate" behave the way a user expects.

### Serving several end users behind one key

A conversation is identified by the caller's own user turns, scoped to the API key. If **one key serves several of your end users**, two of them holding character-identical conversations would resolve to the same session, and each would see the other's turns.

Prevent it in one of two ways:

- pass a distinct `user` on every request (standard OpenAI field), or
- set `X-Surogate-Conversation: <your conversation id>` — collision-free, and the recommended integration for anything multi-tenant.

An explicit conversation id also lets you keep no transcript of your own: send just the latest message with the header, and the agent supplies the history.

## Authentication

Keys are minted per agent from **Studio → the agent → Channels → Web → Manage**. The raw token is shown exactly once; only a SHA-256 digest is stored and the plaintext cannot be recovered. Revoking takes effect immediately on the replica serving the next request and within a minute across the rest.

A key is bound to the agent it was minted for. Presenting it against another agent — even one in the same organisation — returns `403`. Keys carry no user identity and no permissions; they cannot reach admin, auth, or any other `/v1/` route.

## Billing

API turns meter exactly like every other channel. The key's owner is the billed party, and a turn draws on the agent's per-user allowance through the same authorize/settle path the web, Slack, Telegram and website channels use. Free or uncapped agents are unaffected.

`api` is a sellable channel, so an offer that restricts channels excludes API access unless it is included. A buyer whose package leaves it out gets `402 channel_not_included`.

## Fire-and-forget submission

For batch pipelines that do not want a response at all:

```
POST /v1/api/prompts
Authorization: Bearer surg_sk_...

{
  "prompt": "Write a haiku about distributed systems.",
  "idempotency_key": "dataset-42/row-1337",
  "metadata": {"dataset_id": "ds_123", "row_index": 1337}
}
```

Returns `202` with a `session_id`. The worker processes it asynchronously and the pipeline reads results from the database. `idempotency_key` is scoped per org: two requests carrying the same key resolve to the same session, so retries under timeouts are safe. Anything in `metadata` lands on `sessions.config['pipeline_metadata']`, so results join back to the source dataset without a side table.

`POST /v1/api/prompts:batch` accepts up to 100 prompts in one round-trip, each processed independently, response order matching input order.

### Reading results

| Signal | Source |
|---|---|
| Final answer | `events` rows with `type = 'llm.response'` |
| Tool calls / results | `events` rows with `type IN ('tool.call', 'tool.result')` |
| Completion status | `sessions.status` |
| Cost / token usage | `sessions.input_tokens`, `sessions.output_tokens`, `sessions.estimated_cost_usd` |
| Pipeline metadata | `sessions.config->'pipeline_metadata'` |

The `v_session_messages` view returns conversation-shaped events in training-data format. See [docs/audit/views.md](../audit/views.md) for the full catalog.

## Interaction with other subsystems

- **Training data**: API sessions participate in `TrainingDataCollector` exports on the same footing as every other channel.
- **Idle reset**: the session-reset CronJob resets API sessions in place without running the memory-flush agent — service accounts have no per-user memory.
- **Memory**: API sessions use the org-shared memory directory, not user-scoped memory.
- **Inbox**: API sessions raise no inbox items. There is no conversation a person opens to clear one.

## Verifying an integration

`scripts/openai_conformance.py` drives a live agent through the whole surface with the real OpenAI SDK — models list, multi-turn memory, streaming, reasoning, images, long turns, and the auth refusals — and prints a pass/fail table:

```bash
python scripts/openai_conformance.py \
    --base-url https://profesor-romana-o6eskd.cloud.surogate.ai/v1/api \
    --api-key surg_sk_...
```
