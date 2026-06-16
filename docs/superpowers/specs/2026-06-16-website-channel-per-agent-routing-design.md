# Website channel — per-agent routing (the "adapting")

**Date:** 2026-06-16
**Status:** Approved design, pre-implementation
**Repos touched:** `surogates` (route + SDK), `surogate-ops` (Studio + ops service)

## Problem

The website widget is presented per-agent in Studio (each agent mints its own
`surg_wk_…` publishable key + allowed site), but the **runtime gates the
channel on global process settings** — `settings.website.enabled /
publishable_key / allowed_origins`. One server = one key = one site, for all
agents. Studio's per-agent config is written to `agent.env_vars` and never
read by the runtime.

This is **not a dropped feature**. The per-agent mechanism is the
`channel_routing` table — the same one Slack and Telegram already use. The
website channel simply was never wired onto it. Most of the infrastructure
already exists; three pieces need to "adapt" onto it.

## What already exists (verified)

- `ChannelRouting` table, `ChannelKind.website`: maps
  `channel_identifier` (the publishable key) → `(org_id, agent_id, api_web_url)`.
- `GET /api/widget/p/{publishable_key}` — **live** pairing route on the ops
  server (`widget_pairing.py`), returns `{ agent_id, api_web_url }`.
- `admin_channels.py` — creates `ChannelRouting` rows (incl. website kind).
- `api_web_url` plumbed through runtime-config → `AgentRuntimeContext.api_web_url`.
- `channel_routing_cache` wired into the surogates API app (`app.state`).
- Slack/Telegram adapters already resolve agent via `channel_routing` (the
  pattern to mirror — see `channels/slack.py::resolve`).

## Target contract (per-agent)

```
Embed:  <script …website-widget@2…></script>
        SurogatesWidget.mount({ publishableKey })

1. Pair:  GET  {platform}/api/widget/p/<key>   → { agent_id, api_web_url }   (already live)
2. Boot:  POST {api_web_url}/v1/website/sessions   (Authorization: Bearer <key> + Origin)
            → route resolves channel_routing(website:<key>) → (agent_id, org_id)
            → ✓ active row exists   ✓ Origin in global allow-list   → HttpOnly cookie + CSRF
3. Chat:  POST …/sessions/{id}/messages (+ X-CSRF-Token),  GET …/events (SSE)
```

> **`api_web_url` (decided 2026-06-16):** it is the **agent's website-API base
> URL** — where the widget bootstraps (the surogates host serving
> `/v1/website/*`), *not* the customer's embedding origin. Origin validation
> stays **global** (`settings.website.allowed_origins`); per-agent origin
> scoping is deferred. So the routing row carries `(org_id, agent_id,
> api_web_url=<agent API base>)` and **no** per-agent origin.

**Security model:** the publishable key is both the identifier and the auth at
bootstrap — resolved+validated via `channel_routing` (key→agent), exactly like
Slack's `app_id`. **Origin allow-listing stays global** (`settings.website.allowed_origins`)
for now — `channel_routing` has no per-agent origins field, and adding one was
deferred (decided 2026-06-16). So a leaked key works only from a globally
allow-listed origin; per-agent origin scoping is a future follow-up. The
global on-switch `settings.website.enabled` is also kept (deployment-level
channel toggle). Only the **global single publishable key** is retired —
replaced by per-agent key→routing lookup. No new secret, no per-agent k8s.

## The three changes

### 1. `surogates` — `api/routes/website.py` (core)

Replace **only the agent-resolution + key check** with `channel_routing`;
keep the global enabled + origin gates:

- Keep `_require_website_enabled(settings)` — global deployment on-switch.
- Resolve `(agent_id, org_id)` from `channel_routing(website:<key>)` using
  `request.app.state.channel_routing_cache`. No active row → **404** (replaces
  the global `settings.website.publishable_key` compare AND the
  subdomain/`?agent_id=` resolution).
- Keep the global Origin check: `origin_allowed(request_origin,
  parse_allowed_origins(settings.website.allowed_origins))` → **403** on miss.
- Drop the `agent_runtime_context_dep` dependency from bootstrap (agent now
  comes from the routing row). The lifecycle `enabled=false` gate is handled by
  ops deactivating the routing row; per-agent lifecycle re-check is a follow-up.
- `settings.website.publishable_key` becomes unused; `enabled`,
  `allowed_origins`, `session_message_cap` stay.

Helper shape mirrors `channels/slack.py::resolve` (cache lookup → row).

### 2. `surogate-ops` — Studio save creates the routing row

On saving the Website channel for an agent:

- **Upsert** a `ChannelRouting` row: `(website, channel_identifier=key,
  agent_id, org_id=project_id, api_web_url=<allowed site>, active=true)` — via
  a small service reusing `admin_channels` create logic (extract a shared
  `upsert_website_routing(...)`).
- Keep the **appearance** env-vars (title/subtitle/logo/accent/welcome/position)
  — still used to render the copyable embed snippet.
- Drop the now-dead enable/key/origin env-vars from the save (`ENABLED`,
  `PUBLISHABLE_KEY`, `ALLOWED_ORIGINS` no longer feed the runtime).
- The frontend "Allowed Origins" list maps to `api_web_url` (one origin per
  row; multiple sites = multiple rows — out of scope for now).

### 3. `surogates/sdk/website-widget` — key-only mount via pairing

- Add a mount path that takes **`publishableKey` only**: call
  `GET {pairingBase}/api/widget/p/<key>` → `{ agent_id, api_web_url }`, then
  bootstrap against `api_web_url`.
- `pairingBase` defaults to a platform URL, overridable via `mount({ pairingUrl })`.
- Keep `apiUrl` accepted (back-compat) — if provided, skip pairing.
- Bump the widget (new minor) so a fresh `@2` publish carries it.

## Testing

Unit tests per piece (no live cluster needed):

- **Route:** active-row hit → 201 + cookie; missing/inactive row → 404; Origin
  mismatch → 403; agent disabled → 503. Mock the routing cache + runtime context.
- **Ops:** `upsert_website_routing` creates a new row, updates an existing one
  (key rotation / origin change), and is idempotent.
- **SDK:** mocked `fetch` — key-only mount calls pairing then bootstraps against
  the returned `api_web_url`; `apiUrl` provided → no pairing call.

Live end-to-end is **gated on a deployment that serves the website channel**
(densemax currently 404s all API paths). We will not fake a green bootstrap;
the unit suites are the acceptance bar for this session.

## Out of scope (YAGNI)

- Multiple origins per key (list field) — multiple rows instead.
- Migrating existing agents' env-var config into routing rows (one-off backfill;
  separate task).
- Re-enabling the global `settings.website.*` path as a fallback — retired.

## Open risks

- The website route currently uses `agent_runtime_context_dep` for org/agent +
  lifecycle gate; switching to key→routing must still apply the `enabled=false`
  → 503 lifecycle gate for the resolved agent.
- Existing deployments relying on the global key will stop bootstrapping once
  the route switches — acceptable (the global single-key model was never the
  intended per-agent design), but note for rollout.
