# Website Channel

The website channel exposes an agent on a **public website** as a chat widget. Visitors are anonymous browser users with no platform account; identity is the server-side session cookie alone. The developer embedding the widget builds their own UI against this API — there is no hosted JS SDK.

Authentication is a two-layer pattern borrowed from Stripe's publishable keys:

1. **Publishable key** (`surg_wk_…`) — safe to embed in browser JS. Authority is recognised only together with an `Origin` header listed in the agent's allow-list. A stolen key used from a different origin is rejected.
2. **Session cookie** — issued on bootstrap. HttpOnly + Secure + SameSite=None, scoped to `/v1/website`. Signed JWT with the session id, agent id, origin, and a CSRF token baked in.

State-changing endpoints require a double-submit CSRF token: the value of the `csrf` claim in the cookie JWT must match an `X-CSRF-Token` header on every POST.

## When to use it

| Use case | Example |
|---|---|
| Support widget | A bot on your docs site that answers product questions and files tickets |
| Sales assistant | A pricing-page chat that helps visitors choose a plan |
| Discovery tool | A help-me-find-the-right-feature conversation on a marketing site |

Do **not** use the website channel for authenticated end users; use the [web channel](web.md) so the session carries user identity and per-user memory.

Do **not** use the website channel for fire-and-forget backend pipelines; use the [API channel](api.md) with `surg_sk_` service-account tokens.

## Provisioning a website agent

Website agents are managed programmatically through `surogate-ops` using the `WebsiteAgentStore` Python API:

```python
from surogates.channels.website_agent_store import WebsiteAgentStore

store = WebsiteAgentStore(session_factory)
issued = await store.create(
    org_id=org_id,
    name="support-bot",
    allowed_origins=["https://customer.com", "https://www.customer.com"],
    tool_allow_list=["web_search", "clarify", "consult_expert"],
    system_prompt="You are the Acme product support agent...",
    model="gpt-5.4",
    session_message_cap=50,
    session_idle_minutes=30,
)
print(issued.publishable_key)   # surg_wk_… — surface this ONCE
```

The raw publishable key is returned exactly once. Only a SHA-256 digest is stored; if you lose it, rotate by deleting and recreating the agent.

### Key configuration fields

| Field | Purpose |
|---|---|
| `allowed_origins` | Exact-match list (scheme + host + port). Wildcards not supported. |
| `tool_allow_list` | Subset of tools the anonymous visitor may invoke. Empty = no tools. |
| `system_prompt` | Prepended to the harness system prompt. |
| `model` | Model override for this agent's sessions. |
| `skill_pins` | Skills pinned into every session the agent serves. |
| `session_message_cap` | 0 = unbounded. Enforced on each message submission. |
| `session_token_cap` | 0 = unbounded. Enforced by the harness on each LLM call. |
| `session_idle_minutes` | Idle timeout before the session is reset in place. |
| `enabled` | Disabling stops all in-flight sessions within the auth cache TTL (~30s). |

### Default tool allow-list rationale

The default `tool_allow_list` is empty — ops must enumerate every tool explicitly. Tools that are never appropriate for anonymous visitors:

* `terminal`, `execute_code`, `patch`, `write_file`, `read_file`, `search_files`, `list_files` — filesystem/shell access
* `skill_manage` — mutates tenant assets
* `delegate_task` — can spawn arbitrary sub-agents

Common safe defaults: `web_search`, `web_extract`, `clarify`, `consult_expert`, `todo`.

## Endpoints

All website-channel endpoints live under `/v1/website/*`. They are exempt from the platform's global JWT middleware and run their own authentication.

### POST /v1/website/sessions — bootstrap

Exchanges a publishable key + allowed origin for a session cookie.

```
POST /v1/website/sessions
Authorization: Bearer surg_wk_...
Origin: https://customer.com
```

Response (`201 Created`):

```json
{
  "session_id": "8f...",
  "csrf_token": "hL7q...",
  "expires_at": 1714567890,
  "agent_name": "support-bot"
}
```

`Set-Cookie` header sets `surg_ws=…; HttpOnly; Secure; SameSite=None; Path=/v1/website; Max-Age=3600`. Your browser client holds the CSRF token in memory and echoes it on every POST.

### POST /v1/website/sessions/{id}/messages — send a message

```
POST /v1/website/sessions/8f.../messages
Origin: https://customer.com
X-CSRF-Token: hL7q...
Cookie: surg_ws=...
Content-Type: application/json

{"content": "How do I cancel my subscription?"}
```

Response (`202 Accepted`):

```json
{"event_id": 42, "status": "processing"}
```

### GET /v1/website/sessions/{id}/events — stream events (SSE)

```
GET /v1/website/sessions/8f.../events?after=0
Origin: https://customer.com
Accept: text/event-stream
Cookie: surg_ws=...
```

`EventSource` cannot set custom headers, so no `X-CSRF-Token` is required (SSE is a GET; CSRF protection targets state-changing requests). Authentication is cookie + origin.

Stream format mirrors the [web channel](web.md) — `event: llm.response`, `event: tool.call`, etc. A `session.done` event with `retry: 0` is sent when the session enters a terminal state; the client should stop reconnecting.

### POST /v1/website/sessions/{id}/end — end the session

Optional. Marks the session completed and clears the cookie. Requires cookie + CSRF.

```
POST /v1/website/sessions/8f.../end
Origin: https://customer.com
X-CSRF-Token: hL7q...
Cookie: surg_ws=...
```

Response (`204 No Content`) with a `Set-Cookie` that deletes `surg_ws`.

## Browser integration sketch

Plain fetch + EventSource:

```js
const PUBLISHABLE_KEY = "surg_wk_...";
const API = "https://agent.your-org.com";

// 1. Bootstrap
const boot = await fetch(`${API}/v1/website/sessions`, {
  method: "POST",
  credentials: "include",            // accept the cookie
  headers: {
    "Authorization": `Bearer ${PUBLISHABLE_KEY}`,
    "Content-Type": "application/json",
  },
});
const { session_id, csrf_token } = await boot.json();

// 2. Subscribe to events (cookie sent automatically)
const stream = new EventSource(
  `${API}/v1/website/sessions/${session_id}/events`,
  { withCredentials: true },
);
stream.addEventListener("llm.response", (e) => render(JSON.parse(e.data)));
stream.addEventListener("session.done", () => stream.close());

// 3. Send a message
await fetch(`${API}/v1/website/sessions/${session_id}/messages`, {
  method: "POST",
  credentials: "include",
  headers: {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrf_token,
  },
  body: JSON.stringify({ content: userInput }),
});
```

### CORS requirements

For `credentials: "include"` to work, the browser requires:

* `Access-Control-Allow-Origin` set to your exact origin (not `*`)
* `Access-Control-Allow-Credentials: true`
* Preflight must return 204 with `Access-Control-Allow-Headers` listing `Content-Type, Authorization, X-CSRF-Token`

The platform's `/v1/website/*` CORS middleware handles all of this per-agent. Preflight (`OPTIONS`) is answered permissively because the browser strips auth from preflights — the actual authorisation happens on the follow-up request, which the route refuses if the origin isn't in the agent's allow-list.

## Security model

| Concern | Mitigation |
|---|---|
| Publishable key leak | Key authority is only recognised together with an `Origin` in the agent's allow-list. A key lifted from your site and used from a different origin is rejected at bootstrap. |
| Session cookie theft | Cookie is HttpOnly (inaccessible to JS) + Secure (HTTPS only) + SameSite=None (cross-site because the widget embed is cross-site by definition). The cookie's `origin` claim is re-verified on every request so replay from a different origin fails even if the attacker has the cookie. |
| Cross-site request forgery | Double-submit CSRF — the `X-CSRF-Token` header must match the `csrf` claim baked into the HttpOnly cookie. Cross-origin JS cannot read the cookie, so it cannot forge a matching header. |
| Over-privileged visitor | `tool_allow_list` materialises onto `session.config` at bootstrap; the harness enforces it before dispatch. A visitor session physically cannot invoke `terminal`, `execute_code`, or any tool outside the list, no matter what the LLM generates. |
| Runaway visitor | `session_message_cap` and `session_token_cap` bound cost per session; `session_idle_minutes` triggers in-place reset without running the memory-flush agent (visitors have no per-user memory). |
| Ops-side disable | Calling `store.update(agent_id, enabled=False)` stops new bootstraps immediately and in-flight sessions within the auth cache TTL (~30s). |
| Cross-session replay | The session cookie JWT scopes to a single `session_id`; hitting `/v1/website/sessions/{other}/messages` with it returns 404 indistinguishable from "session doesn't exist". |

## Interaction with other subsystems

* **Memory**: website sessions have `user_id=None`. The idle-reset job skips the memory-flush agent (no per-user memory to preserve) and resets in place with reason `idle_website_visitor`.
* **Training data**: Every website session participates in `TrainingDataCollector` exports on the same footing as every other channel.
* **Prompt injection**: The global `PromptInjectionDetector` does **not** run on website messages yet; adding it is straightforward but would need a review of how anonymous-visitor input compares to authenticated-user input for false-positive rates.
* **Rate limiting**: Per-token rate limits in `surogates:rate:*` do not apply to the website channel (the middleware keys off `Authorization`, which the browser doesn't carry after bootstrap). Consider per-IP limits at your edge/ingress for abuse protection.
