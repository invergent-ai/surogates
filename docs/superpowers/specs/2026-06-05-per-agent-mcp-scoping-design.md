# Per-agent MCP server scoping — Design

**Date:** 2026-06-05
**Status:** Approved (reviewed; implementation-ready)
**Primary repo:** `/work/surogates` (harness) — one docstring fix in `/work/surogate-ops`

## Summary

Make the MCP proxy enforce **per-agent** MCP server access. Today the proxy
resolves a caller's servers purely by `(org_id, user_id)`, so every agent in a
project sees and can call **all** of the project's enabled MCP servers. The
per-agent allow-list already exists and already reaches the runtime as
`AgentRuntimeContext.mcp_server_ids` — the proxy simply never reads it. This
change reads and enforces it, strictly, at every layer of the proxy, and
removes the dead alternative scaffolding that was built for the same purpose.

## Background — current state

### The allow-list already flows into the runtime (but is ignored)

The management plane already maintains a per-agent allow-list end to end:

```
ops: agent_mcp_servers join  (core/db/models/operate.py)
  → resync_runtime_config_mcp_servers writes config.mcp_server_ids
      (surogate_ops/server/services/agents_shared.py:363,395)
  → GET /api/agents/agents/{agent_id}/runtime-config returns mcp_server_ids
      (surogate_ops/server/routes/agent_runtime.py:111)
  → surogates PlatformClient.get_runtime_config fetches the payload
      (surogates/runtime/platform_client.py:64)
  → runtime_config_cache → resolver.build_agent_runtime_context
      (surogates/runtime/resolver.py:59)
  → AgentRuntimeContext.mcp_server_ids  (surogates/runtime/context.py:99)
```

`ctx.mcp_server_ids` is therefore the **live, per-agent allow-list**, present
in the runtime today. The IDs are the ops MCP-server row IDs, and the
ops→surogates mirror preserves that ID (`SurogatesClient.upsert_mcp_server`
sets `id=server_id`), so they match the surogates `mcp_servers.id` space
directly.

### Where enforcement is missing

The MCP proxy resolves servers only by `(org_id, user_id)` and never consults
`ctx.mcp_server_ids`:

- `surogates/mcp_proxy/loader.py:121-149` — `_load_db_configs` selects
  `org_id == :org AND (user_id IS NULL OR user_id == :user) AND enabled`.
- `surogates/mcp_proxy/pool.py` — connection pool + schema cache keyed on
  `(org_id, user_id)`; tool-name prefix `_tenant_prefix(org_id, user_id)`.
- `surogates/mcp_proxy/routes.py:127-145` — `tools/list` has no agent context
  at all (only the sandbox `(org, user, session)` JWT).
- `surogates/mcp_proxy/routes.py:148-187` — `tools/call` already resolves
  `ctx` via `agent_runtime_context_dep` (so `ctx.agent_id` is present on the
  call path today) but uses it only to stamp audit rows.

### Dead scaffolding (built for per-agent, never wired to enforcement)

- `surogates/runtime/mcp_server_cache.py` — `MCPServerRegistryCache.get()` has
  zero production call sites.
- `surogates/runtime/platform_client.py:160` — `get_agent_mcp_servers`, called
  only by the dead cache builders.
- `surogates/mcp_proxy/app.py` + `surogates/api/app.py` —
  `build_mcp_server_cache` / `app.state.mcp_server_cache`.

## Decisions (from brainstorming)

1. **Strict scoping.** An agent sees and can call **only** the servers attached
   to it via `agent_mcp_servers`. Empty allow-list ⇒ zero MCP tools (no
   org-wide fallback). The `(org, user)`-only visibility path is removed.
2. **Foundation = `ctx.mcp_server_ids`.** Reactivate the already-live
   allow-list rather than mirror a new join table. Single source of truth.
3. **Full agent-keyed pool.** Connections and schema cache become agent-scoped,
   so an agent never connects to — or resolves credentials for — a server it
   isn't attached to. Least-privilege by construction at every layer.
4. **Remove the dead scaffolding** (no-legacy-fallbacks rule).
5. **No prompt-schema leakage.** Worker-local MCP handlers may stay registered
   after discovery, but model-visible schemas are filtered per session to the
   current agent's discovered MCP tool set.

## Architecture / data flow (after)

```
tools/list  (worker discover_and_register)
   + ?agent_id=session.agent_id                                   ← ADD
   → list_tools: + Depends(agent_runtime_context_dep) → ctx       ← ADD
   → _ensure_tenant_connected(..., agent_id, allowed_ids=ctx.mcp_server_ids)
   → load_mcp_configs → _load_db_configs(org_id, allowed_ids):
         WHERE org_id=:org AND id IN :allowed AND enabled
         allowed_ids empty ⇒ {}                                   ← CHANGE
   → pool entry + schema cache keyed (org_id, user_id, agent_id)  ← CHANGE
   → only the agent's attached servers are connected

tools/call  (ctx.agent_id already present today)
   → same agent-keyed connect
   → resolve_call_target only ever sees the agent's servers       ← CHANGE
   → a non-attached server is structurally unreachable (no extra guard needed)

worker registry / prompt schemas
   → discovery response is tracked by agent_id                     ← ADD
   → current session's MCP tool names are added to the session tool filter
   → previously-discovered MCP tools for other agents are not advertised
   → if a stale handler is invoked anyway, tools/call still enforces scope
```

## Component changes (file by file)

All paths under `/work/surogates/surogates/` unless noted.

### Identity plumbing
- **`orchestrator/worker.py:790`** — pass `agent_id=session.agent_id` into
  `mcp_proxy_client.discover_and_register(...)`.
- **`orchestrator/mcp_client.py`** — `discover_and_register` gains an
  `agent_id` parameter and appends `?agent_id={agent_id}` to the
  `/mcp/v1/tools/list` POST. The per-call handler **already** appends
  `?agent_id=` (`mcp_client.py:171-173`, since `call_tool` 400s without it), so
  this brings the discovery/list path to parity with the call path.
  Replace the process-wide `_discovered: set[str]` with per-agent discovery
  bookkeeping, e.g. `_discovered_by_agent: dict[str, set[str]]`, and return the
  current agent's discovered MCP tool names on every call. The shared
  `ToolRegistry` may keep handlers registered process-wide, but the prompt
  schema surface must be session-filtered so tools discovered for agent A are
  not advertised to agent B.
- **`harness/loop.py` / session tool filtering** — plumb the current agent's
  discovered MCP tool names into `_tool_filter_for_session` (or an equivalent
  schema-filtering hook). For sessions without explicit `allowed_tools`, remove
  `mcp__*` names from the broad worker registry set and add back only the
  current agent's discovered names. For sessions with explicit `allowed_tools`,
  intersect any `mcp__*` entries with the current agent's discovered names.
  This is required because the worker owns one `ToolRegistry` across sessions;
  strict proxy enforcement alone prevents unauthorized calls, but without this
  prompt-schema filter an agent can still see stale MCP tool schemas from a
  previous session.
- **JWT is intentionally NOT changed.** `agent_runtime_context_dep` resolves
  `agent_id` from the `?agent_id=` query param, so neither
  `tenant/auth/jwt.py` nor `mcp_proxy/auth.py` needs a new claim.

### Enforcement
- **`mcp_proxy/routes.py`** —
  - `list_tools`: add `ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep)`;
    pass `agent_id=ctx.agent_id` and `allowed_ids=ctx.mcp_server_ids` into
    `_ensure_tenant_connected`.
  - `call_tool`: already has `ctx`; pass `allowed_ids=ctx.mcp_server_ids`
    through the same path.
  - `_ensure_tenant_connected`: thread `agent_id` + `allowed_ids` into
    `load_mcp_configs` and the pool key / `get_cached_schemas`.
- **`mcp_proxy/loader.py`** — `_load_db_configs(org_id, allowed_ids)`:
  - Replace the `(user_id IS NULL OR user_id == :user)` **access** filter with
    `McpServer.id.in_(allowed_ids)`.
  - **Empty `allowed_ids` ⇒ return `{}`** (strict; short-circuit before the
    query).
  - `user_id` is still threaded through `load_mcp_configs` → `_resolve_credentials`
    for **caller credential** resolution (unchanged); it is no longer an access
    gate for MCP server rows. In other words, attaching a user-owned MCP server
    row to an agent controls server visibility, while credential refs still
    resolve against the request principal's user/org vaults.
- **`mcp_proxy/pool.py`** —
  - Pool **entry key** and **schema-cache key** become `(org_id, user_id, agent_id)`:
    `_entries`, `get_cached_schemas(...)`, `resolve_call_target(...)`, and the
    connect path all gain `agent_id`.
  - Add an internal server key / prefix that includes `agent_id` for the
    module-level `surogates.tools.mcp.client._servers` dict. Do **not** reuse
    `_prefixed_name(org_id, user_id, server_name)` across agents, or two agents
    under the same `(org, user)` can share the same long-lived upstream
    connection key even though the pool entries are separate.
  - **Exposed tool names remain unchanged** (`mcp__{server}__{tool}`). If the
    internal key includes `agent_id`, strip the full internal prefix when
    building `clean_schemas` and `tool_index`, so model-visible and
    worker-visible tool names do not churn.
  - `call_tool(...)` remains only for HTTP/SSE fallback, but it must also accept
    and use `agent_id` because HTTP servers still rely on the long-lived pool
    entry and reverse index.

### Cache invalidation
- **`runtime/invalidator.py`** — on `agent.runtime_config_changed` /
  `agent.mcp_servers_changed`, in addition to evicting `runtime_config_cache`,
  **evict the agent's proxy pool entry + schema cache** so a detached server
  stops being callable immediately (not at TTL).
  - The current invalidator maps one channel prefix to one cache object; update
    it to support multiple targets per channel or special-case these two agent
    channels.
  - Add a pool invalidation method, e.g. `ConnectionPool.invalidate_agent(agent_id)`,
    that immediately removes matching `(org_id, user_id, agent_id)` entries from
    `_entries` / `_locks` and then shuts down their module-level `_servers`
    connections asynchronously. Immediate removal is the security boundary;
    background shutdown is cleanup.
  - `agent.mcp_servers_changed:<agent_id>` currently routes to the dead
    `mcp_server_cache`; after deleting that cache, route it to
    `runtime_config_cache.invalidate(agent_id)` plus `pool.invalidate_agent(agent_id)`.

### Dead-wiring removal (no-legacy-fallbacks)
- Delete `runtime/mcp_server_cache.py` (`MCPServerRegistryCache`).
- Delete `platform_client.get_agent_mcp_servers` (`runtime/platform_client.py`).
- Delete `build_mcp_server_cache` / `app.state.mcp_server_cache` in
  `mcp_proxy/app.py` and `api/app.py`, and their `app.state` references.
- Keep `AgentRuntimeContext.mcp_server_ids` (now the live mechanism).

### Documentation fix (ops)
- **`/work/surogate-ops/surogate_ops/server/services/agents_shared.py:363-367`**
  — the docstring's description of how the proxy consumes
  `config.mcp_server_ids` becomes accurate after this change; update its
  wording to match (it now genuinely gates the proxy).

## Enforcement semantics

- **Strict.** No attachments ⇒ `tools/list` returns no MCP tools; `tools/call`
  returns `404 "No MCP servers configured for this tenant."` (existing
  message; now per-agent).
- **No schema leakage.** A shared worker may retain already-registered MCP
  handlers, but the tool schemas passed to the model are filtered to the
  current agent's discovery result. An unauthorized stale handler invocation is
  still rejected by the proxy call path.
- **Least-privilege by construction.** Because every layer (load, connect,
  cache, route) is agent-scoped, `resolve_call_target` can only return a server
  in the agent's set; a non-attached server is unreachable without a bolt-on
  authorization check.
- **Defense in depth.** `org_id` is retained alongside the `id IN allowed_ids`
  filter — an agent's `mcp_server_ids` can only reference servers under its org.

## Out of scope

- **Management plane.** Ops already maintains the join, `config.mcp_server_ids`,
  attach/detach endpoints, and the invalidation events. No ops logic change —
  only the one docstring fix above.
- **Per-user MCP servers.** `user_id` remains row ownership metadata, and
  credential lookup still uses the caller's user/org vaults; it is no longer an
  access gate for MCP server rows. No new per-user surface is added.
- **CLAUDE.md staleness** (the deleted `agent_chart`/Helm references) — tracked
  separately; not part of this change.

## Testing strategy

- **loader** — empty `allowed_ids` ⇒ `{}`; subset selection returns exactly the
  attached rows; an id outside the org is not returned; user-specific rows are
  selected only by attached ID, not by caller `user_id`.
- **pool** — two agents under one `(org, user)` with different `mcp_server_ids`
  get disjoint schemas and connections; cache lookups keyed by agent; internal
  `_servers` keys include `agent_id` even when exposed tool names do not.
- **worker registry** — after agent A discovers `mcp__a_server__tool`, agent B
  with no MCP attachments does not see that schema in `Loop` tool schemas even
  though the shared `ToolRegistry` still has the handler registered.
- **call path** — an agent cannot call a server it isn't attached to (`404`);
  can call one it is; HTTP/SSE fallback resolves through the agent-keyed pool.
- **invalidation** — detaching a server evicts the agent's pool entry; the tool
  disappears from `tools/list`, the worker stops advertising the schema after
  redis invalidation / rediscovery, and calls `404` without a restart.
- **regression** — an agent attached to every project server behaves as before.
- **test cleanup** — delete or replace `tests/runtime/test_mcp_server_cache.py`
  and the `MCPServerRegistryCache` assertions in `tests/runtime/test_invalidator.py`
  / `tests/runtime/test_mcp_proxy_state.py`; add focused tests for
  `runtime_config_cache + pool.invalidate_agent` on the two agent channels.

## Risks & rollout

- **Behavior change (operational):** existing agents that relied on org-wide
  visibility lose servers until explicitly attached. Before rollout, check
  current `agent_mcp_servers` coverage against what agents actually use, and
  backfill attachments where needed.
- **Name collisions** among an agent's own attached servers (an org row + a
  user row sharing a `name`) are now possible because selection is by `id` but
  config dictionaries and exposed tool names are still keyed by `name`.
  Preserve deterministic precedence (`org-wide < user-specific`, then stable
  `id` ordering); do not leave SQL row order to decide the winner.
- **More upstream connections** overall (per-agent rather than per-tenant), each
  smaller and lazily established — accepted for the isolation guarantee.

## Open questions

None outstanding. Foundation (`ctx.mcp_server_ids`), strictness (no fallback),
pool granularity (full agent-keyed), and dead-wiring removal are all decided.
