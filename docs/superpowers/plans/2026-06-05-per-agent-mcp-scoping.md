# Per-agent MCP Server Scoping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the MCP proxy enforce per-agent server access — an agent sees and can call only the servers attached to it via `agent_mcp_servers`, with no org-wide fallback — by reading the already-live `AgentRuntimeContext.mcp_server_ids` and scoping every proxy layer (loader, pool, discovery, prompt schemas) by `agent_id`.

**Architecture:** The per-agent allow-list already reaches the runtime as `ctx.mcp_server_ids` (ops `agent_mcp_servers` → `config.mcp_server_ids` → `/runtime-config` → resolver). The proxy currently ignores it and scopes by `(org_id, user_id)`. We thread `agent_id` + the allow-list through the discovery path, filter the DB loader by attached server id (strict: empty ⇒ none), key the connection pool by `(org_id, user_id, agent_id)`, filter the worker's shared-registry prompt schemas to the agent's discovered MCP tools, evict per-agent pool entries on attachment changes, and delete the dead `MCPServerRegistryCache` scaffolding.

**Tech Stack:** Python 3.12, FastAPI, async SQLAlchemy, pytest + pytest-asyncio (`asyncio_mode = "auto"`). Repo: `/work/surogates`. Test runner: `uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-06-05-per-agent-mcp-scoping-design.md`

---

## Baseline (read before starting)

The current branch is **not fully green**. These 4 tests already fail on `feat/per-agent-mcp-scoping` before any change, due to a pre-existing signature drift in `routes._execute_call` (`tool_name=` vs `clean_tool_name`/`original_tool`):

```
uv run pytest tests/runtime/test_call_tool_per_call_subprocess.py -q
# 4 failed, 3 passed  (test_execute_call_* fail with TypeError: unexpected keyword 'tool_name')
```

Task 2 fixes these as a side effect (it re-signatures `_execute_call` for `agent_id` and corrects the tests). Do not treat them as regressions you caused. Record the baseline of every module you touch before editing it.

## File structure

All paths under `/work/surogates/` unless noted.

| File | Responsibility | Change |
|------|----------------|--------|
| `surogates/orchestrator/mcp_client.py` | Worker→proxy MCP HTTP client | Per-agent discovery bookkeeping; `?agent_id=` on list; return full per-agent set |
| `surogates/orchestrator/worker.py` | Per-session harness factory | Pass `agent_id` to discovery; capture discovered set; pass to harness |
| `surogates/mcp_proxy/routes.py` | Proxy endpoints | `list_tools` gains ctx dep; thread `agent_id` + `allowed_ids` |
| `surogates/mcp_proxy/loader.py` | DB config load + credential resolve | Filter by attached server id; strict empty ⇒ `{}` |
| `surogates/mcp_proxy/pool.py` | Connection + schema cache | Key by `(org,user,agent)`; `invalidate_agent()` |
| `surogates/harness/loop.py` | Agent harness / tool filter | Per-agent MCP prompt-schema filter |
| `surogates/runtime/invalidator.py` | Redis cache invalidator | Multi-target routing; evict agent pool entry |
| `surogates/mcp_proxy/app.py` | Proxy app wiring | Pass pool to invalidator; drop dead cache |
| `surogates/api/app.py` | API app wiring | Drop dead `build_mcp_server_cache` |
| `surogates/runtime/mcp_server_cache.py` | (dead) | **Delete** |
| `surogates/runtime/platform_client.py` | Platform HTTP client | Delete dead `get_agent_mcp_servers` |
| `surogates/runtime/__init__.py` | Runtime exports | Drop `MCPServerRegistryCache` export |
| `/work/surogate-ops/.../agents_shared.py` | (ops) docstring | Correct stale proxy-behavior note |

---

## Task 1: List-path identity — per-agent discovery + `?agent_id=`

Make the worker's MCP discovery agent-aware: track discovered tools per agent, send `?agent_id=` on `tools/list`, return the agent's full discovered set, and give `list_tools` the agent context dependency it lacks.

**Files:**
- Modify: `surogates/orchestrator/mcp_client.py:39`, `:41-102`
- Modify: `surogates/mcp_proxy/routes.py:127-134`
- Modify: `surogates/orchestrator/worker.py:786-795`
- Test: `tests/runtime/test_mcp_client_per_agent.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/runtime/test_mcp_client_per_agent.py`:

```python
"""McpProxyClient discovery is per-agent.

The worker shares one ToolRegistry across every agent it serves, so
discovery must (a) send the agent_id to the proxy, (b) track which
tools each agent has, and (c) return the agent's FULL discovered set
on every call (not just newly-registered names) so the harness can
filter the shared registry's prompt schemas down to this agent.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from surogates.orchestrator.mcp_client import McpProxyClient
from surogates.tools.registry import ToolRegistry


class _FakeResp:
    status_code = 200

    def __init__(self, names):
        self._names = names

    def json(self):
        return {
            "tools": [
                {"name": n, "description": "", "parameters": {}}
                for n in self._names
            ]
        }


@pytest.mark.asyncio
async def test_discover_sends_agent_id_and_returns_full_set(monkeypatch):
    reg = ToolRegistry()
    client = McpProxyClient(base_url="http://proxy", registry=reg)

    captured = []

    async def fake_post(url, headers=None, params=None, json=None):
        captured.append((url, params))
        return _FakeResp(["mcp__github__list_issues"])

    monkeypatch.setattr(client._client, "post", fake_post)

    names = await client.discover_and_register(
        org_id=UUID(int=1), user_id=UUID(int=2), session_id=UUID(int=3),
        agent_id="agent-A",
    )

    assert names == ["mcp__github__list_issues"]
    assert captured[0][1] == {"agent_id": "agent-A"}
    assert "mcp__github__list_issues" in reg.tool_names

    # Second discovery for the SAME agent returns the full set again,
    # not an empty "nothing new" list.
    names2 = await client.discover_and_register(
        org_id=UUID(int=1), user_id=UUID(int=2), session_id=UUID(int=3),
        agent_id="agent-A",
    )
    assert names2 == ["mcp__github__list_issues"]
    await client.close()


@pytest.mark.asyncio
async def test_discover_tracks_each_agent_separately(monkeypatch):
    reg = ToolRegistry()
    client = McpProxyClient(base_url="http://proxy", registry=reg)

    async def fake_post(url, headers=None, params=None, json=None):
        agent = params["agent_id"]
        names = {
            "agent-A": ["mcp__github__list_issues"],
            "agent-B": ["mcp__jira__search"],
        }[agent]
        return _FakeResp(names)

    monkeypatch.setattr(client._client, "post", fake_post)

    a = await client.discover_and_register(
        org_id=UUID(int=1), user_id=UUID(int=2), session_id=UUID(int=3),
        agent_id="agent-A",
    )
    b = await client.discover_and_register(
        org_id=UUID(int=1), user_id=UUID(int=2), session_id=UUID(int=4),
        agent_id="agent-B",
    )

    assert a == ["mcp__github__list_issues"]
    assert b == ["mcp__jira__search"]
    await client.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/runtime/test_mcp_client_per_agent.py -q`
Expected: FAIL — `discover_and_register()` got an unexpected keyword argument `agent_id`.

- [ ] **Step 3: Make discovery per-agent in `mcp_client.py`**

Replace line 39:

```python
        self._discovered: set[str] = set()
```

with:

```python
        # Discovery is tracked per agent because the worker shares one
        # ToolRegistry across every agent it serves.  Maps agent_id ->
        # the set of mcp__ tool names that agent may use.
        self._discovered_by_agent: dict[str, set[str]] = {}
```

Replace the whole `discover_and_register` method (currently `mcp_client.py:41-102`) with:

```python
    async def discover_and_register(
        self,
        org_id: UUID,
        user_id: UUID,
        session_id: UUID,
        *,
        agent_id: str,
        is_service_account: bool = False,
    ) -> list[str]:
        """Discover *agent_id*'s MCP tools via the proxy and register them.

        Returns the FULL set of MCP tool names available to *agent_id*
        (not just names registered on this call), so the caller can
        filter the shared ``ToolRegistry`` prompt-schema surface down to
        this agent's tools.  The proxy scopes the result to the agent's
        attached servers via the ``agent_id`` query param.

        ``is_service_account`` flags the principal so the proxy can skip
        ``users.id`` foreign keys (e.g. ``audit_log``); pass ``True``
        when ``session.user_id`` is ``None`` and we fell back to the
        session's ``service_account_id``.
        """
        token = create_sandbox_token(
            org_id, user_id, session_id,
            is_service_account=is_service_account,
        )
        headers = {"Authorization": f"Bearer {token}"}
        known = self._discovered_by_agent.setdefault(agent_id, set())

        resp = await self._client.post(
            "/mcp/v1/tools/list",
            headers=headers,
            params={"agent_id": agent_id},
        )
        if resp.status_code != 200:
            logger.warning(
                "MCP proxy tool discovery failed for agent %s: %d %s",
                agent_id, resp.status_code, resp.text[:200],
            )
            return sorted(known)

        data = resp.json()
        tools = data.get("tools", [])

        for tool in tools:
            name = tool.get("name", "")
            if not name:
                continue
            known.add(name)
            # The registry is process-wide; another agent may have
            # already registered this exact tool name.  Registering is
            # idempotent, but skip the work when it is already present.
            if name in self._registry.tool_names:
                continue
            handler = self._make_proxy_handler(name)
            self._registry.register(
                name=name,
                schema=ToolSchema(
                    name=name,
                    description=tool.get("description", ""),
                    parameters=tool.get("parameters", {}),
                ),
                handler=handler,
                toolset="mcp",
            )

        registered = sorted(known)
        if registered:
            logger.info(
                "Agent %s has %d MCP tool(s) via proxy: %s",
                agent_id, len(registered), ", ".join(registered),
            )
        return registered
```

- [ ] **Step 4: Give `list_tools` the agent context**

In `surogates/mcp_proxy/routes.py`, replace the `list_tools` signature + body head (lines 127-134):

```python
@router.post("/mcp/v1/tools/list", response_model=ToolListResponse)
async def list_tools(
    request: Request,
    auth: ProxyAuthContext = Depends(get_proxy_auth),
) -> ToolListResponse:
    """Discover available MCP tools for the authenticated tenant."""
    pool: ConnectionPool = request.app.state.pool
    schemas = await _ensure_tenant_connected(pool, auth, request)
```

with:

```python
@router.post("/mcp/v1/tools/list", response_model=ToolListResponse)
async def list_tools(
    request: Request,
    auth: ProxyAuthContext = Depends(get_proxy_auth),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> ToolListResponse:
    """Discover the MCP tools available to the requesting agent."""
    pool: ConnectionPool = request.app.state.pool
    schemas = await _ensure_tenant_connected(
        pool, auth, request, agent_id=ctx.agent_id,
    )
```

(`AgentRuntimeContext` and `agent_runtime_context_dep` are already imported at `routes.py:24-28`.)

- [ ] **Step 5: Pass `agent_id` from the worker's discovery call**

In `surogates/orchestrator/worker.py`, replace the discovery call (lines 786-795) with:

```python
        if mcp_proxy_client is not None:
            try:
                principal_user_id = session.user_id or session.service_account_id
                if principal_user_id is not None:
                    await mcp_proxy_client.discover_and_register(
                        org_id=session_org_id,
                        user_id=principal_user_id,
                        session_id=session.id,
                        agent_id=ctx.agent_id,
                        is_service_account=session.user_id is None,
                    )
            except Exception:
```

(`ctx` is resolved at `worker.py:761`. The returned set is captured in Task 4.)

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/runtime/test_mcp_client_per_agent.py -q`
Expected: PASS (2 passed).

- [ ] **Step 7: Commit**

```bash
git add surogates/orchestrator/mcp_client.py surogates/mcp_proxy/routes.py surogates/orchestrator/worker.py tests/runtime/test_mcp_client_per_agent.py
git commit -m "feat(mcp): per-agent discovery and agent_id on tools/list"
```

---

## Task 2: Agent-keyed connection pool

Key the pool's entries, schema cache, server-name prefixes, and connections by `(org_id, user_id, agent_id)` so two agents under one `(org, user)` never share an entry or an upstream connection. Add `invalidate_agent()`. Thread `agent_id` through the route call sites and fix the pre-existing `_execute_call` tests.

**Files:**
- Modify: `surogates/mcp_proxy/pool.py` (prefixes, keys, methods, new `invalidate_agent`)
- Modify: `surogates/mcp_proxy/routes.py` (call sites: `_ensure_tenant_connected`, `call_tool`, `_execute_call`)
- Test: `tests/runtime/test_pool_agent_scoped.py` (create)
- Test: `tests/runtime/test_call_tool_per_call_subprocess.py` (fix pre-existing + add `agent_id`)

- [ ] **Step 1: Write the failing pool test**

Create `tests/runtime/test_pool_agent_scoped.py`:

```python
"""ConnectionPool is keyed by (org_id, user_id, agent_id).

Two agents under one (org, user) must get disjoint cache entries, and
invalidate_agent() must evict exactly one agent's entries.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from surogates.mcp_proxy.pool import ConnectionPool, PoolEntry, _prefixed_name


ORG = UUID(int=1)
USER = UUID(int=2)


def _entry(agent_id: str, schemas):
    return PoolEntry(
        org_id=ORG, user_id=USER, agent_id=agent_id,
        server_names=[], tool_schemas=schemas,
    )


def test_prefixed_name_includes_agent_id():
    a = _prefixed_name(ORG, USER, "agent-A", "github")
    b = _prefixed_name(ORG, USER, "agent-B", "github")
    assert a != b
    assert a.endswith("__github")


def test_get_cached_schemas_is_agent_keyed():
    pool = ConnectionPool()
    pool._entries[(ORG, USER, "agent-A")] = _entry(
        "agent-A", [{"name": "mcp__github__x"}],
    )
    assert pool.get_cached_schemas(ORG, USER, "agent-A") == [
        {"name": "mcp__github__x"},
    ]
    assert pool.get_cached_schemas(ORG, USER, "agent-B") is None


@pytest.mark.asyncio
async def test_invalidate_agent_evicts_only_that_agent():
    pool = ConnectionPool()
    pool._entries[(ORG, USER, "agent-A")] = _entry("agent-A", [])
    pool._entries[(ORG, USER, "agent-B")] = _entry("agent-B", [])

    pool.invalidate_agent("agent-A")

    assert (ORG, USER, "agent-A") not in pool._entries
    assert (ORG, USER, "agent-B") in pool._entries
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/runtime/test_pool_agent_scoped.py -q`
Expected: FAIL — `_prefixed_name()` takes 3 positional args; `PoolEntry` has no `agent_id`; no `invalidate_agent`.

- [ ] **Step 3: Add `agent_id` to the prefix helpers (`pool.py:41-48`)**

Replace:

```python
def _tenant_prefix(org_id: UUID, user_id: UUID) -> str:
    """Build the server-name prefix that makes module-level entries unique."""
    return f"{org_id}_{user_id}"


def _prefixed_name(org_id: UUID, user_id: UUID, server_name: str) -> str:
    """Full tenant-scoped server name used as key in the global _servers dict."""
    return f"{_tenant_prefix(org_id, user_id)}__{server_name}"
```

with:

```python
def _tenant_prefix(org_id: UUID, user_id: UUID, agent_id: str) -> str:
    """Build the server-name prefix that makes module-level entries unique.

    Includes ``agent_id`` so two agents under the same ``(org, user)``
    never share a long-lived upstream connection in the module-level
    ``_servers`` dict — each agent connects only to its attached servers.
    """
    return f"{org_id}_{user_id}_{agent_id}"


def _prefixed_name(
    org_id: UUID, user_id: UUID, agent_id: str, server_name: str,
) -> str:
    """Full agent-scoped server name used as key in the global _servers dict."""
    return f"{_tenant_prefix(org_id, user_id, agent_id)}__{server_name}"
```

- [ ] **Step 4: Add `agent_id` to `PoolEntry` (`pool.py:84-86`)**

Replace:

```python
    org_id: UUID
    user_id: UUID
    server_names: list[str]  # original (unprefixed) server names
```

with:

```python
    org_id: UUID
    user_id: UUID
    agent_id: str
    server_names: list[str]  # original (unprefixed) server names
```

- [ ] **Step 5: Re-key `_entries` / `_locks` (`pool.py:133-134`)**

Replace:

```python
        self._entries: dict[tuple[UUID, UUID], PoolEntry] = {}
        self._locks: dict[tuple[UUID, UUID], asyncio.Lock] = {}
```

with:

```python
        self._entries: dict[tuple[UUID, UUID, str], PoolEntry] = {}
        self._locks: dict[tuple[UUID, UUID, str], asyncio.Lock] = {}
```

- [ ] **Step 6: Thread `agent_id` through the query + connect methods**

`get_cached_schemas` (`pool.py:168-176`) — replace:

```python
    def get_cached_schemas(
        self, org_id: UUID, user_id: UUID,
    ) -> list[dict[str, Any]] | None:
        """Return cached tool schemas if the tenant is connected, else None."""
        entry = self._entries.get((org_id, user_id))
```

with:

```python
    def get_cached_schemas(
        self, org_id: UUID, user_id: UUID, agent_id: str,
    ) -> list[dict[str, Any]] | None:
        """Return cached tool schemas if the agent is connected, else None."""
        entry = self._entries.get((org_id, user_id, agent_id))
```

`resolve_call_target` (`pool.py:178-202`) — replace the signature line and the two body lines that key/prefix:

```python
    def resolve_call_target(
        self, org_id: UUID, user_id: UUID, tool_name: str,
    ) -> tuple[str, str, dict[str, Any]] | None:
```
→
```python
    def resolve_call_target(
        self, org_id: UUID, user_id: UUID, agent_id: str, tool_name: str,
    ) -> tuple[str, str, dict[str, Any]] | None:
```

```python
        entry = self._entries.get((org_id, user_id))
        if entry is None:
            return None
        entry.last_used = time.monotonic()
        routing = entry.tool_index.get(tool_name)
        if routing is None:
            return None
        server_key, original_tool = routing
        # Strip the tenant prefix from the server key to recover the
        # config dict key (server_configs is indexed by the original
        # unprefixed name).
        expected_prefix = f"{_tenant_prefix(org_id, user_id)}__"
```
→
```python
        entry = self._entries.get((org_id, user_id, agent_id))
        if entry is None:
            return None
        entry.last_used = time.monotonic()
        routing = entry.tool_index.get(tool_name)
        if routing is None:
            return None
        server_key, original_tool = routing
        # Strip the agent prefix from the server key to recover the
        # config dict key (server_configs is indexed by the original
        # unprefixed name).
        expected_prefix = f"{_tenant_prefix(org_id, user_id, agent_id)}__"
```

`ensure_connected` (`pool.py:216-356`) — replace the signature:

```python
    async def ensure_connected(
        self,
        org_id: UUID,
        user_id: UUID,
        configs: dict[str, dict[str, Any]],
        *,
        is_service_account: bool = False,
    ) -> list[dict[str, Any]]:
```
→
```python
    async def ensure_connected(
        self,
        org_id: UUID,
        user_id: UUID,
        agent_id: str,
        configs: dict[str, dict[str, Any]],
        *,
        is_service_account: bool = False,
    ) -> list[dict[str, Any]]:
```

In its body, replace `key = (org_id, user_id)` (`:233`) with `key = (org_id, user_id, agent_id)`; replace `pname = _prefixed_name(org_id, user_id, name)` (`:246`) with `pname = _prefixed_name(org_id, user_id, agent_id, name)`; replace `_tenant_prefix(org_id, user_id),` (`:273`) with `_tenant_prefix(org_id, user_id, agent_id),`; replace `expected_prefix = f"{_tenant_prefix(org_id, user_id)}__"` (`:317`) with `expected_prefix = f"{_tenant_prefix(org_id, user_id, agent_id)}__"`. In the `PoolEntry(...)` constructor (`:338-349`), add `agent_id=agent_id,` immediately after `user_id=user_id,`. Replace the log line (`:352-355`):

```python
            logger.info(
                "Connected %d MCP server(s) for org=%s user=%s (%d tools)",
                len(original_names), org_id, user_id, len(clean_schemas),
            )
```
→
```python
            logger.info(
                "Connected %d MCP server(s) for org=%s user=%s agent=%s "
                "(%d tools)",
                len(original_names), org_id, user_id, agent_id,
                len(clean_schemas),
            )
```

`call_tool` (`pool.py:456-472`) — replace the signature and key:

```python
    async def call_tool(
        self,
        org_id: UUID,
        user_id: UUID,
        tool_name: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> str:
```
→
```python
    async def call_tool(
        self,
        org_id: UUID,
        user_id: UUID,
        agent_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> str:
```

and replace `key = (org_id, user_id)` (`:471`) with `key = (org_id, user_id, agent_id)`.

`_disconnect_entry` (`pool.py:591`) — replace:

```python
                prefixed = _prefixed_name(entry.org_id, entry.user_id, name)
```
→
```python
                prefixed = _prefixed_name(
                    entry.org_id, entry.user_id, entry.agent_id, name,
                )
```

`_evict_idle` (`pool.py:614-627`) — replace:

```python
            evicted: list[tuple[UUID, UUID]] = []

            for key, entry in list(self._entries.items()):
                if now - entry.last_used > self._idle_timeout:
                    await self._disconnect_entry(entry)
                    evicted.append(key)

            for key in evicted:
                self._entries.pop(key, None)
                self._locks.pop(key, None)
                logger.info(
                    "Evicted idle MCP connections for org=%s user=%s",
                    key[0], key[1],
                )
```
→
```python
            evicted: list[tuple[UUID, UUID, str]] = []

            for key, entry in list(self._entries.items()):
                if now - entry.last_used > self._idle_timeout:
                    await self._disconnect_entry(entry)
                    evicted.append(key)

            for key in evicted:
                self._entries.pop(key, None)
                self._locks.pop(key, None)
                logger.info(
                    "Evicted idle MCP connections for org=%s user=%s agent=%s",
                    key[0], key[1], key[2],
                )
```

- [ ] **Step 7: Add `invalidate_agent` (insert after `_disconnect_entry`, before `_evict_idle`, ~`pool.py:604`)**

```python
    def invalidate_agent(self, agent_id: str) -> None:
        """Immediately evict every pool entry for *agent_id*.

        Called from the Redis invalidator when the agent's MCP
        attachments or runtime config change, so a detached server stops
        being callable at once rather than at the idle-eviction TTL.
        Synchronous removal from ``_entries`` is the security boundary;
        the upstream-connection teardown is best-effort cleanup
        scheduled on the running loop.
        """
        keys = [k for k in self._entries if k[2] == agent_id]
        entries: list[PoolEntry] = []
        for key in keys:
            entry = self._entries.pop(key, None)
            self._locks.pop(key, None)
            if entry is not None:
                entries.append(entry)
        for entry in entries:
            asyncio.create_task(self._disconnect_entry(entry))
```

- [ ] **Step 8: Run the pool test to verify it passes**

Run: `uv run pytest tests/runtime/test_pool_agent_scoped.py -q`
Expected: PASS (3 passed).

- [ ] **Step 9: Thread `agent_id` through the route call sites**

In `surogates/mcp_proxy/routes.py`, `_ensure_tenant_connected` (`:78-119`): change the parameter from optional-for-audit to required, and pass it to the pool. Replace the signature:

```python
async def _ensure_tenant_connected(
    pool: ConnectionPool,
    auth: ProxyAuthContext,
    request: Request,
    *,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
```
→
```python
async def _ensure_tenant_connected(
    pool: ConnectionPool,
    auth: ProxyAuthContext,
    request: Request,
    *,
    agent_id: str,
) -> list[dict[str, Any]]:
```

Replace `cached = pool.get_cached_schemas(auth.org_id, auth.user_id)` (`:97`) with:

```python
    cached = pool.get_cached_schemas(auth.org_id, auth.user_id, agent_id)
```

Replace the `pool.ensure_connected(...)` call (`:114-119`):

```python
    return await pool.ensure_connected(
        org_id=auth.org_id,
        user_id=auth.user_id,
        configs=configs,
        is_service_account=auth.is_service_account,
    )
```
→
```python
    return await pool.ensure_connected(
        org_id=auth.org_id,
        user_id=auth.user_id,
        agent_id=agent_id,
        configs=configs,
        is_service_account=auth.is_service_account,
    )
```

In `call_tool`, replace the `resolve_call_target` call (`:180-182`):

```python
    target = pool.resolve_call_target(
        auth.org_id, auth.user_id, body.name,
    )
```
→
```python
    target = pool.resolve_call_target(
        auth.org_id, auth.user_id, ctx.agent_id, body.name,
    )
```

Replace the `_execute_call(...)` invocation (`:197-206`) to pass `agent_id`:

```python
        result_text, outcome = await _execute_call(
            pool=pool,
            org_id=auth.org_id,
            user_id=auth.user_id,
            server_config=server_config,
            clean_tool_name=body.name,
            original_tool=original_tool,
            arguments=body.arguments,
            meta=body.meta,
        )
```
→
```python
        result_text, outcome = await _execute_call(
            pool=pool,
            org_id=auth.org_id,
            user_id=auth.user_id,
            agent_id=ctx.agent_id,
            server_config=server_config,
            clean_tool_name=body.name,
            original_tool=original_tool,
            arguments=body.arguments,
            meta=body.meta,
        )
```

In `_execute_call` (`:260-314`), add the `agent_id` parameter after `user_id`:

```python
    org_id: Any,
    user_id: Any,
    server_config: dict[str, Any],
```
→
```python
    org_id: Any,
    user_id: Any,
    agent_id: str,
    server_config: dict[str, Any],
```

and pass it to the HTTP/SSE fallback `pool.call_tool(...)` (`:303-309`):

```python
        result = await pool.call_tool(
            org_id=org_id,
            user_id=user_id,
            tool_name=clean_tool_name,
            arguments=arguments,
            meta=meta,
        )
```
→
```python
        result = await pool.call_tool(
            org_id=org_id,
            user_id=user_id,
            agent_id=agent_id,
            tool_name=clean_tool_name,
            arguments=arguments,
            meta=meta,
        )
```

- [ ] **Step 10: Fix the pre-existing `_execute_call` tests + cover `agent_id`**

In `tests/runtime/test_call_tool_per_call_subprocess.py`, the `_RecordingPool.call_tool` and the four `_execute_call(...)` calls use the stale `tool_name=` kwarg and omit `clean_tool_name`/`original_tool`/`agent_id`. Replace `_RecordingPool` (`:60-70`):

```python
class _RecordingPool:
    """Captures pool.call_tool invocations so the HTTP-fallback path
    can be asserted without standing up a real long-lived session."""

    def __init__(self, result: str) -> None:
        self.calls: list[dict] = []
        self._result = result

    async def call_tool(self, **kwargs):
        self.calls.append(kwargs)
        return self._result
```

(no change needed to `_RecordingPool` itself — it records `**kwargs`). Update the HTTP-fallback assertion in `test_execute_call_http_transport_falls_back_to_pool` (`:85-100`):

```python
    result_text, outcome = await routes._execute_call(
        pool=pool,
        org_id="o-1",
        user_id="u-1",
        agent_id="agent-A",
        server_config=_make_server_config("http"),
        clean_tool_name="ping",
        original_tool="ping",
        arguments={"x": 1},
        meta={"chat_user_id": "c-1"},
    )

    assert result_text == '{"ok": true}'
    assert outcome == routes._OUTCOME_SUCCESS
    assert pool.calls == [{
        "org_id": "o-1", "user_id": "u-1", "agent_id": "agent-A",
        "tool_name": "ping", "arguments": {"x": 1},
        "meta": {"chat_user_id": "c-1"},
    }]
```

In the other three `_execute_call(...)` calls (`test_execute_call_http_transport_propagates_tool_error`, `test_execute_call_stdio_missing_command_returns_transport_error`, `test_execute_call_stdio_spawn_failure_returns_transport_error`), replace `tool_name="ping",` with:

```python
        agent_id="agent-A",
        clean_tool_name="ping",
        original_tool="ping",
```

- [ ] **Step 11: Run the call-tool tests**

Run: `uv run pytest tests/runtime/test_call_tool_per_call_subprocess.py -q`
Expected: PASS (9 passed) — the 4 pre-existing failures are now fixed.

- [ ] **Step 12: Commit**

```bash
git add surogates/mcp_proxy/pool.py surogates/mcp_proxy/routes.py tests/runtime/test_pool_agent_scoped.py tests/runtime/test_call_tool_per_call_subprocess.py
git commit -m "feat(mcp): key the connection pool by (org, user, agent) and add per-agent eviction"
```

---

## Task 3: Loader per-agent allow-list filter

Filter the DB loader by the agent's attached server ids and wire `ctx.mcp_server_ids` through. Strict: an empty allow-list returns no servers.

**Files:**
- Modify: `surogates/mcp_proxy/loader.py:46-92`, `:121-149`
- Modify: `surogates/mcp_proxy/routes.py` (`_ensure_tenant_connected` passes `allowed_ids`)
- Test: `tests/runtime/test_loader_allowlist.py` (create — empty short-circuit, no DB)
- Test: `tests/integration/test_loader_agent_scoped.py` (create — DB-backed positive filter)

- [ ] **Step 1: Write the failing unit test (empty allow-list)**

Create `tests/runtime/test_loader_allowlist.py`:

```python
"""Strict per-agent MCP loading: an empty allow-list yields no servers.

The empty case short-circuits before any DB access, so it is unit-
testable without a database.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from surogates.mcp_proxy.loader import _load_db_configs


@pytest.mark.asyncio
async def test_empty_allowlist_returns_no_servers():
    # session_factory is never touched when allowed_ids is empty.
    result = await _load_db_configs(
        session_factory=None,
        org_id=UUID(int=1),
        allowed_ids=frozenset(),
    )
    assert result == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/runtime/test_loader_allowlist.py -q`
Expected: FAIL — `_load_db_configs()` takes `user_id`, not `allowed_ids`.

- [ ] **Step 3: Filter `_load_db_configs` by attached id (`loader.py:121-149`)**

Replace the whole function:

```python
async def _load_db_configs(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: UUID,
    user_id: UUID,
) -> dict[str, dict[str, Any]]:
    """Load MCP server configs from the database.

    Single query fetches org-wide (user_id IS NULL) and user-specific
    rows.  User-specific configs overwrite org-wide configs with the
    same name.
    """
    from sqlalchemy import or_

    configs: dict[str, dict[str, Any]] = {}

    async with session_factory() as db:
        result = await db.execute(
            select(McpServer)
            .where(McpServer.org_id == org_id)
            .where(or_(McpServer.user_id.is_(None), McpServer.user_id == user_id))
            .where(McpServer.enabled.is_(True))
            .order_by(McpServer.user_id.asc().nulls_first())
        )
        for row in result.scalars().all():
            # User-specific rows come after org-wide (nulls first),
            # so they naturally overwrite.
            configs[row.name] = _row_to_config(row)

    return configs
```

with:

```python
async def _load_db_configs(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: UUID,
    allowed_ids: frozenset[str],
) -> dict[str, dict[str, Any]]:
    """Load the agent's attached MCP server configs from the database.

    Strict per-agent scoping: only servers whose id is in *allowed_ids*
    (the agent's ``mcp_server_ids`` from its runtime config) are
    returned.  An empty allow-list short-circuits to ``{}`` — an agent
    with no attached servers gets no MCP tools.  ``org_id`` is retained
    as a defense-in-depth bound.

    When two attached rows share a ``name`` (an org row + a user row),
    the user-specific row wins (``nulls_first`` ordering means it is
    applied last); ties beyond that resolve by stable ``id`` order.
    """
    if not allowed_ids:
        return {}

    # ``UUID`` is imported at module scope (loader.py:34). ctx.mcp_server_ids
    # are strings; McpServer.id is a UUID column.
    id_values = [UUID(str(i)) for i in allowed_ids]

    configs: dict[str, dict[str, Any]] = {}

    async with session_factory() as db:
        result = await db.execute(
            select(McpServer)
            .where(McpServer.org_id == org_id)
            .where(McpServer.id.in_(id_values))
            .where(McpServer.enabled.is_(True))
            .order_by(
                McpServer.user_id.asc().nulls_first(),
                McpServer.id.asc(),
            )
        )
        for row in result.scalars().all():
            # Deterministic precedence: org-wide (nulls first) then
            # user-specific overwrite by name, then stable id order.
            configs[row.name] = _row_to_config(row)

    return configs
```

- [ ] **Step 4: Thread `allowed_ids` through `load_mcp_configs` (`loader.py:46-92`)**

Replace the signature:

```python
async def load_mcp_configs(
    org_id: UUID,
    user_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    vault: CredentialVault,
    audit_store: AuditStore | None = None,
    *,
    is_service_account: bool = False,
    agent_id: str | None = None,
) -> dict[str, dict[str, Any]]:
```
→
```python
async def load_mcp_configs(
    org_id: UUID,
    user_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    vault: CredentialVault,
    audit_store: AuditStore | None = None,
    *,
    allowed_ids: frozenset[str],
    is_service_account: bool = False,
    agent_id: str | None = None,
) -> dict[str, dict[str, Any]]:
```

Replace the `_load_db_configs` call (`:73`):

```python
    merged = await _load_db_configs(session_factory, org_id, user_id)
```
→
```python
    # Per-agent allow-list scopes WHICH servers load; user_id still
    # drives per-caller credential resolution below.
    merged = await _load_db_configs(session_factory, org_id, allowed_ids)
```

(Leave the `_resolve_credentials_safe(...)` call unchanged — it still uses `user_id` for credential resolution.)

- [ ] **Step 5: Pass `allowed_ids` from the route (`routes.py:101-109`)**

In `_ensure_tenant_connected`, replace the `load_mcp_configs(...)` call:

```python
    configs = await load_mcp_configs(
        org_id=auth.org_id,
        user_id=auth.user_id,
        session_factory=request.app.state.session_factory,
        vault=request.app.state.vault,
        audit_store=getattr(request.app.state, "audit_store", None),
        is_service_account=auth.is_service_account,
        agent_id=agent_id,
    )
```
→
```python
    configs = await load_mcp_configs(
        org_id=auth.org_id,
        user_id=auth.user_id,
        session_factory=request.app.state.session_factory,
        vault=request.app.state.vault,
        audit_store=getattr(request.app.state, "audit_store", None),
        allowed_ids=allowed_ids,
        is_service_account=auth.is_service_account,
        agent_id=agent_id,
    )
```

Add the `allowed_ids` parameter to `_ensure_tenant_connected` and pass it from both routes. Replace the signature:

```python
async def _ensure_tenant_connected(
    pool: ConnectionPool,
    auth: ProxyAuthContext,
    request: Request,
    *,
    agent_id: str,
) -> list[dict[str, Any]]:
```
→
```python
async def _ensure_tenant_connected(
    pool: ConnectionPool,
    auth: ProxyAuthContext,
    request: Request,
    *,
    agent_id: str,
    allowed_ids: frozenset[str],
) -> list[dict[str, Any]]:
```

In `list_tools`, replace the call:

```python
    schemas = await _ensure_tenant_connected(
        pool, auth, request, agent_id=ctx.agent_id,
    )
```
→
```python
    schemas = await _ensure_tenant_connected(
        pool, auth, request,
        agent_id=ctx.agent_id,
        allowed_ids=frozenset(ctx.mcp_server_ids),
    )
```

In `call_tool`, replace the `_ensure_tenant_connected(...)` call (`:171-173`):

```python
    schemas = await _ensure_tenant_connected(
        pool, auth, request, agent_id=ctx.agent_id,
    )
```
→
```python
    schemas = await _ensure_tenant_connected(
        pool, auth, request,
        agent_id=ctx.agent_id,
        allowed_ids=frozenset(ctx.mcp_server_ids),
    )
```

- [ ] **Step 6: Run the unit test**

Run: `uv run pytest tests/runtime/test_loader_allowlist.py -q`
Expected: PASS (1 passed).

- [ ] **Step 7: Write the DB-backed positive-filter test**

Create `tests/integration/test_loader_agent_scoped.py`:

```python
"""DB-backed: the loader returns exactly the agent's attached servers.

Two server rows exist under one org; the loader returns only the ids in
the allow-list, proving per-agent scoping at the SQL layer.
"""

from __future__ import annotations

import uuid

import pytest

from surogates.db.models import McpServer
from surogates.mcp_proxy.loader import _load_db_configs

from .conftest import create_org


@pytest.mark.asyncio
async def test_loader_returns_only_attached_ids(session_factory):
    org_id = await create_org(session_factory)

    keep_id = uuid.uuid4()
    drop_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(McpServer(
            id=keep_id, org_id=org_id, user_id=None, name="github",
            transport="stdio", command="cat", enabled=True,
        ))
        db.add(McpServer(
            id=drop_id, org_id=org_id, user_id=None, name="jira",
            transport="stdio", command="cat", enabled=True,
        ))
        await db.commit()

    configs = await _load_db_configs(
        session_factory=session_factory,
        org_id=org_id,
        allowed_ids=frozenset({str(keep_id)}),
    )

    assert set(configs) == {"github"}
```

- [ ] **Step 8: Run the integration test**

Run: `uv run pytest tests/integration/test_loader_agent_scoped.py -q`
Expected: PASS (1 passed). (Requires Docker for the Postgres testcontainer — same as the rest of `tests/integration`.)

- [ ] **Step 9: Commit**

```bash
git add surogates/mcp_proxy/loader.py surogates/mcp_proxy/routes.py tests/runtime/test_loader_allowlist.py tests/integration/test_loader_agent_scoped.py
git commit -m "feat(mcp): scope DB server loading to the agent's attached ids (strict)"
```

---

## Task 4: Per-agent prompt-schema filter

The worker shares one `ToolRegistry`, so its prompt schemas accumulate every agent's `mcp__*` tools. Filter the model-visible schema set per session to this agent's discovered MCP tools (proxy enforcement already blocks the *call*; this blocks the *visibility*).

**Files:**
- Modify: `surogates/orchestrator/worker.py` (capture discovered set; pass to harness)
- Modify: `surogates/harness/loop.py` (`__init__` kwarg; `_apply_mcp_schema_filter`; call it)
- Test: `tests/harness/test_mcp_schema_filter.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/harness/test_mcp_schema_filter.py`:

```python
"""The harness filters model-visible MCP schemas to this agent's set.

The worker's ToolRegistry is process-wide, so it may hold mcp__ tools
discovered for other agents.  _apply_mcp_schema_filter must drop the
foreign ones while leaving non-MCP tools untouched.
"""

from __future__ import annotations

from surogates.harness.loop import AgentHarness


class _Reg:
    def __init__(self, names):
        self.tool_names = set(names)


def _harness(registry_names, mine):
    h = AgentHarness.__new__(AgentHarness)
    h._tools = _Reg(registry_names)
    h._mcp_tool_names = frozenset(mine)
    return h


def test_foreign_mcp_tools_dropped_default_session():
    h = _harness(
        registry_names={
            "send_message", "mcp__github__list", "mcp__jira__search",
        },
        mine={"mcp__github__list"},
    )
    # Default worker session: no explicit allow-list.
    result = h._apply_mcp_schema_filter(
        {"send_message", "mcp__github__list", "mcp__jira__search"},
        explicit_allowed=False,
    )
    assert result == {"send_message", "mcp__github__list"}


def test_none_filter_materialised_and_scoped():
    h = _harness(
        registry_names={"send_message", "mcp__github__list", "mcp__jira__x"},
        mine={"mcp__github__list"},
    )
    result = h._apply_mcp_schema_filter(None, explicit_allowed=False)
    assert "mcp__jira__x" not in result
    assert "mcp__github__list" in result
    assert "send_message" in result


def test_explicit_allowlist_intersects_mcp_only():
    h = _harness(
        registry_names={"a", "mcp__github__list", "mcp__github__write"},
        mine={"mcp__github__list"},
    )
    # Admin allowed write too, but the agent never discovered it.
    result = h._apply_mcp_schema_filter(
        {"a", "mcp__github__list", "mcp__github__write"},
        explicit_allowed=True,
    )
    assert result == {"a", "mcp__github__list"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/harness/test_mcp_schema_filter.py -q`
Expected: FAIL — `AgentHarness` has no `_apply_mcp_schema_filter` / `_mcp_tool_names`.

- [ ] **Step 3: Add the `mcp_tool_names` kwarg to `AgentHarness.__init__`**

In `surogates/harness/loop.py`, add the parameter to the keyword-only group (after `turn_gate: Any | None = None,` at `:325`):

```python
        turn_gate: Any | None = None,
```
→
```python
        turn_gate: Any | None = None,
        mcp_tool_names: frozenset[str] | None = None,
```

In the body, after `self._tools = tool_registry` (`:328`), add:

```python
        # MCP tool names discovered for THIS session's agent via the
        # proxy.  The registry is process-wide; this scopes the
        # model-visible schema set to the agent's own MCP tools.
        self._mcp_tool_names: frozenset[str] = frozenset(mcp_tool_names or ())
```

- [ ] **Step 4: Add `_apply_mcp_schema_filter` (insert just before `_tool_filter_for_session`, ~`loop.py:2855`)**

```python
    def _apply_mcp_schema_filter(
        self, tool_filter: set[str] | None, *, explicit_allowed: bool,
    ) -> set[str]:
        """Restrict model-visible MCP tool schemas to this agent's set.

        The worker shares one ``ToolRegistry`` across every agent it
        serves, so the registry accumulates ``mcp__*`` tools discovered
        for other agents.  ``self._mcp_tool_names`` is the set discovered
        for this session's agent.

        * ``None`` (no filter) is materialised to the full registry so
          foreign ``mcp__*`` tools can be subtracted.
        * Without an explicit ``allowed_tools`` config, this agent's full
          discovered MCP set is advertised.
        * With an explicit ``allowed_tools`` config, only the ``mcp__*``
          entries that are BOTH allowed and discovered survive.
        """
        all_names = set(self._tools.tool_names)
        base = all_names if tool_filter is None else set(tool_filter)
        non_mcp = {t for t in base if not t.startswith("mcp__")}
        if explicit_allowed:
            mcp_allowed = {
                t for t in base if t.startswith("mcp__")
            } & self._mcp_tool_names
        else:
            mcp_allowed = self._mcp_tool_names & all_names
        return non_mcp | mcp_allowed
```

- [ ] **Step 5: Call the filter on both return paths of `_tool_filter_for_session`**

In `_tool_filter_for_session`, the scheduled-child branch returns at `:2910-2912` and the main path at `:2918-2920`. Insert the MCP filter immediately before each `return self._ensure_always_available_tools(...)`.

Replace (scheduled-child, `:2910-2912`):

```python
            return self._ensure_always_available_tools(
                tool_filter, explicit_allowed=explicit_allowed,
            )
```
→
```python
            tool_filter = self._apply_mcp_schema_filter(
                tool_filter, explicit_allowed=explicit_allowed,
            )
            return self._ensure_always_available_tools(
                tool_filter, explicit_allowed=explicit_allowed,
            )
```

Replace (main path, `:2918-2920`):

```python
        return self._ensure_always_available_tools(
            tool_filter, explicit_allowed=explicit_allowed,
        )
```
→
```python
        tool_filter = self._apply_mcp_schema_filter(
            tool_filter, explicit_allowed=explicit_allowed,
        )
        return self._ensure_always_available_tools(
            tool_filter, explicit_allowed=explicit_allowed,
        )
```

- [ ] **Step 6: Capture the discovered set in the worker and pass it to the harness**

In `surogates/orchestrator/worker.py`, replace the discovery block (the version from Task 1, `:786-801`) to capture the return value:

```python
        discovered_mcp_tools: set[str] = set()
        if mcp_proxy_client is not None:
            try:
                principal_user_id = session.user_id or session.service_account_id
                if principal_user_id is not None:
                    discovered_mcp_tools = set(
                        await mcp_proxy_client.discover_and_register(
                            org_id=session_org_id,
                            user_id=principal_user_id,
                            session_id=session.id,
                            agent_id=ctx.agent_id,
                            is_service_account=session.user_id is None,
                        )
                    )
            except Exception:
                logger.warning(
                    "MCP proxy tool discovery failed for session %s; "
                    "built-in tools still available",
                    session.id, exc_info=True,
                )
```

In the `AgentHarness(...)` construction (`:1082-1136`), add the kwarg next to `turn_gate=turn_gate,` (`:1135`):

```python
            turn_gate=turn_gate,
```
→
```python
            turn_gate=turn_gate,
            mcp_tool_names=frozenset(discovered_mcp_tools),
```

- [ ] **Step 7: Run the filter test**

Run: `uv run pytest tests/harness/test_mcp_schema_filter.py -q`
Expected: PASS (3 passed).

- [ ] **Step 8: Commit**

```bash
git add surogates/harness/loop.py surogates/orchestrator/worker.py tests/harness/test_mcp_schema_filter.py
git commit -m "feat(mcp): filter model-visible MCP schemas to the session agent's tools"
```

---

## Task 5: Invalidate the agent's pool entry on attachment changes

When ops publishes `agent.runtime_config_changed:<id>` or `agent.mcp_servers_changed:<id>`, evict that agent's runtime-config cache **and** its proxy pool entry, so a detached server stops being callable immediately. Wire the pool into the proxy's invalidator.

**Files:**
- Modify: `surogates/runtime/invalidator.py`
- Modify: `surogates/mcp_proxy/app.py`
- Test: `tests/runtime/test_invalidator.py` (add cases)

- [ ] **Step 1: Write the failing test**

Add to `tests/runtime/test_invalidator.py`:

```python
def test_runtime_config_change_also_invalidates_pool():
    from surogates.runtime.invalidator import handle_invalidation_message

    rt = MagicMock()
    pool = MagicMock()
    handle_invalidation_message(
        channel="agent.runtime_config_changed:agent-7",
        payload=b"",
        runtime_config_cache=rt,
        mcp_pool=pool,
    )
    rt.invalidate.assert_called_once_with("agent-7")
    pool.invalidate_agent.assert_called_once_with("agent-7")


def test_mcp_servers_changed_invalidates_config_and_pool():
    from surogates.runtime.invalidator import handle_invalidation_message

    rt = MagicMock()
    pool = MagicMock()
    handle_invalidation_message(
        channel="agent.mcp_servers_changed:agent-7",
        payload=b"",
        runtime_config_cache=rt,
        mcp_pool=pool,
    )
    rt.invalidate.assert_called_once_with("agent-7")
    pool.invalidate_agent.assert_called_once_with("agent-7")


def test_pool_not_touched_for_non_agent_channels():
    from surogates.runtime.invalidator import handle_invalidation_message

    pool = MagicMock()
    handle_invalidation_message(
        channel="project.firebase_config_changed:p-1",
        payload=b"",
        firebase_cache=MagicMock(),
        mcp_pool=pool,
    )
    pool.invalidate_agent.assert_not_called()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/runtime/test_invalidator.py -q -k "pool"`
Expected: FAIL — `handle_invalidation_message()` got an unexpected keyword argument `mcp_pool`.

- [ ] **Step 3: Rework the invalidator routing (`invalidator.py`)**

Replace the `mcp_servers_changed` routing row (`:53-62`) — the dead `mcp_server_cache` target becomes `runtime_config_cache`:

```python
    ("agent.mcp_servers_changed:", "mcp_server_cache"),
```
→
```python
    # admin attach/detach of MCP servers on an agent.  Identifier is
    # the agent_id.  Refreshes the runtime config (so ctx.mcp_server_ids
    # updates) and evicts the agent's proxy pool entry (see
    # _POOL_INVALIDATION_PREFIXES below).
    ("agent.mcp_servers_changed:", "runtime_config_cache"),
```

After the `_CHANNEL_ROUTING` tuple and before `INVALIDATION_CHANNELS` (`:78`), add:

```python
# Channels whose identifier is an agent_id and that must also evict the
# MCP proxy's per-agent connection pool entry, so a detached server stops
# being callable at once rather than at the idle TTL.
_POOL_INVALIDATION_PREFIXES: frozenset[str] = frozenset({
    "agent.runtime_config_changed:",
    "agent.mcp_servers_changed:",
})
```

In `handle_invalidation_message`, replace the `mcp_server_cache: Any = None,` parameter (`:94`) with `mcp_pool: Any = None,`. Remove `"mcp_server_cache": mcp_server_cache,` from the `caches` dict (`:112`). Replace the dispatch loop (`:116-122`):

```python
    for prefix, cache_kwarg in _CHANNEL_ROUTING:
        if channel.startswith(prefix):
            identifier = channel[len(prefix):]
            cache = caches.get(cache_kwarg)
            if identifier and cache is not None:
                cache.invalidate(identifier)
            return
```
→
```python
    for prefix, cache_kwarg in _CHANNEL_ROUTING:
        if channel.startswith(prefix):
            identifier = channel[len(prefix):]
            if not identifier:
                return
            cache = caches.get(cache_kwarg)
            if cache is not None:
                cache.invalidate(identifier)
            if mcp_pool is not None and prefix in _POOL_INVALIDATION_PREFIXES:
                mcp_pool.invalidate_agent(identifier)
            return
```

In `run_invalidator`, replace the `mcp_server_cache: Any = None,` parameter (`:133`) with `mcp_pool: Any = None,`, and in the `handle_invalidation_message(...)` forwarding call replace `mcp_server_cache=mcp_server_cache,` (`:164`) with `mcp_pool=mcp_pool,`.

Finally, delete the now-superseded legacy test in `tests/runtime/test_invalidator.py` (`:153-166`) — its channel now routes to the runtime-config cache + pool (covered by the Step 1 cases). Remove the whole function:

```python
def test_handler_routes_mcp_servers_changed_to_mcp_server_cache():
    """Admin CRUD on the per-tenant MCP server
    registry publishes agent.mcp_servers_changed:<agent_id> on
    Redis; the proxy invalidates its cache so the next call sees
    the new server list."""
    from surogates.runtime.invalidator import handle_invalidation_message

    mc = MagicMock()
    handle_invalidation_message(
        channel="agent.mcp_servers_changed:a-1",
        payload=b"",
        mcp_server_cache=mc,
    )
    mc.invalidate.assert_called_once_with("a-1")
```

- [ ] **Step 4: Wire the pool into the proxy invalidator (`mcp_proxy/app.py`)**

In `_install_shared_runtime_plumbing_for_proxy`, remove `MCPServerRegistryCache` from the import (`:107-110`):

```python
    from surogates.runtime import (
        MCPServerRegistryCache, PerTenantRateLimiter, PlatformClient,
        RuntimeConfigCache, run_invalidator,
    )
```
→
```python
    from surogates.runtime import (
        PerTenantRateLimiter, PlatformClient,
        RuntimeConfigCache, run_invalidator,
    )
```

Delete the dead loader + cache build (`:130-135`):

```python
    async def _mcp_loader(agent_id: str) -> list[dict]:
        return await client.get_agent_mcp_servers(agent_id)

    mcp_server_cache = MCPServerRegistryCache(
        loader=_mcp_loader, ttl_seconds=30.0,
    )

```
→ (remove entirely)

Replace the `app.state` + invalidator wiring (`:137-147`):

```python
    app.state.platform_client = client
    app.state.runtime_config_cache = cache
    app.state.rate_limiter = rate_limiter
    app.state.mcp_server_cache = mcp_server_cache
    app.state.runtime_invalidator_task = asyncio.create_task(
        run_invalidator(
            app.state.redis, runtime_config_cache=cache,
            mcp_server_cache=mcp_server_cache,
        ),
        name="surogates-mcp-proxy-runtime-invalidator",
    )
```
→
```python
    app.state.platform_client = client
    app.state.runtime_config_cache = cache
    app.state.rate_limiter = rate_limiter
    app.state.runtime_invalidator_task = asyncio.create_task(
        run_invalidator(
            app.state.redis,
            runtime_config_cache=cache,
            mcp_pool=getattr(app.state, "pool", None),
        ),
        name="surogates-mcp-proxy-runtime-invalidator",
    )
```

(`getattr(..., None)` — not `app.state.pool` — because `test_mcp_proxy_state.py` exercises `_install_shared_runtime_plumbing_for_proxy` on a bare app with no pool wired; the real lifespan always sets `app.state.pool` first at `app.py:62`.)

In `_shutdown_shared_runtime_plumbing_for_proxy`, remove the dead state nulling (`:170-171`):

```python
    if hasattr(app.state, "mcp_server_cache"):
        app.state.mcp_server_cache = None
```
→ (remove entirely)

- [ ] **Step 5: Run the invalidator + proxy-state tests**

Run: `uv run pytest tests/runtime/test_invalidator.py tests/runtime/test_mcp_proxy_state.py -q`
Expected: PASS — the 3 new pool cases pass, the superseded legacy case is gone, and `test_mcp_proxy_state.py` still passes (it has no `mcp_server_cache` references and `mcp_pool` is resolved via `getattr`).

- [ ] **Step 6: Commit**

```bash
git add surogates/runtime/invalidator.py surogates/mcp_proxy/app.py tests/runtime/test_invalidator.py
git commit -m "feat(mcp): evict the agent's proxy pool entry on runtime-config/attachment changes"
```

---

## Task 6: Remove the dead `MCPServerRegistryCache` scaffolding

Delete the cache module, its platform-client loader, the api-side builder, the runtime export, and the now-orphaned tests. Single source of truth is `ctx.mcp_server_ids`.

**Files:**
- Delete: `surogates/runtime/mcp_server_cache.py`
- Delete: `tests/runtime/test_mcp_server_cache.py`
- Modify: `surogates/runtime/__init__.py`, `surogates/runtime/platform_client.py`, `surogates/api/app.py`
- Modify: `tests/runtime/test_invalidator.py`, `tests/runtime/test_mcp_proxy_state.py`

- [ ] **Step 1: Delete the dead module + its test**

```bash
git rm surogates/runtime/mcp_server_cache.py tests/runtime/test_mcp_server_cache.py
```

- [ ] **Step 2: Remove the runtime export (`runtime/__init__.py:17,55`)**

Delete the import line `from surogates.runtime.mcp_server_cache import MCPServerRegistryCache` and the `"MCPServerRegistryCache",` entry in `__all__`.

- [ ] **Step 3: Confirm test cleanup is already complete**

The only test references to the dead symbols were the legacy invalidator case (removed in the previous task) and `test_mcp_server_cache.py` (deleted in Step 1). `test_mcp_proxy_state.py` has no references. Confirm there is nothing left to fix:

```bash
grep -rn "mcp_server_cache\|MCPServerRegistryCache" tests/ --include=*.py
```
Expected: no matches (the only hit would be `tests/runtime/test_mcp_server_cache.py`, which Step 1 deleted).

- [ ] **Step 4: Delete `get_agent_mcp_servers` (`platform_client.py:160`)**

Remove the whole `async def get_agent_mcp_servers(self, agent_id: str) -> list[dict]:` method.

- [ ] **Step 5: Delete `build_mcp_server_cache` + its wiring (`api/app.py`)**

Remove the `MCPServerRegistryCache,` name from the import at `api/app.py:179`. Delete the build call (`:257-260`):

```python
    # per-agent MCP server registry cache.
    mcp_server_cache = build_mcp_server_cache(
        settings=settings, platform_client=client,
    )

```
Delete `app.state.mcp_server_cache = mcp_server_cache` (`:280`) and `mcp_server_cache=mcp_server_cache,` from the `run_invalidator(...)` call (`:291`). Delete the `build_mcp_server_cache` function (`:347-362`). Delete the shutdown nulling (`:602-603`):

```python
    if hasattr(app.state, "mcp_server_cache"):
        app.state.mcp_server_cache = None
```

(The api app has no `ConnectionPool`; it does not pass `mcp_pool`.)

- [ ] **Step 6: Verify nothing references the dead symbols**

Run:

```bash
grep -rn "MCPServerRegistryCache\|get_agent_mcp_servers\|build_mcp_server_cache\|mcp_server_cache" surogates/ tests/ --include=*.py
```
Expected: no matches.

- [ ] **Step 7: Run the affected suites**

Run: `uv run pytest tests/runtime/test_invalidator.py tests/runtime/test_mcp_proxy_state.py tests/runtime/test_app_state.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "chore(mcp): remove dead MCPServerRegistryCache scaffolding"
```

---

## Task 7: Fix the stale ops docstring (surogate-ops repo)

The docstring in surogate-ops describing how the proxy consumes `config.mcp_server_ids` is now accurate; correct its wording. This is a different repository — branch separately.

**Files:**
- Modify: `/work/surogate-ops/surogate_ops/server/services/agents_shared.py:363-367`

- [ ] **Step 1: Branch in the ops repo**

```bash
cd /work/surogate-ops
git checkout -b chore/mcp-per-agent-docstring
```

- [ ] **Step 2: Read the current docstring**

Read `surogate_ops/server/services/agents_shared.py:360-398` and locate the sentence claiming the runtime's MCP proxy reads its server list from `config.mcp_server_ids`.

- [ ] **Step 3: Correct the wording**

Update the docstring so it states the proxy now enforces per-agent scoping by intersecting the agent's `mcp_server_ids` at discovery/call time (strict; empty ⇒ no servers), replacing any text implying `(org, user)`-only scoping. Keep it to the docstring — no logic change.

- [ ] **Step 4: Commit**

```bash
git add surogate_ops/server/services/agents_shared.py
git commit -m "docs: correct MCP proxy scoping note now that per-agent is enforced"
cd /work/surogates
```

---

## Task 8: Full regression

- [ ] **Step 1: Run the MCP-related suites together**

```bash
uv run pytest \
  tests/runtime/test_mcp_client_per_agent.py \
  tests/runtime/test_pool_agent_scoped.py \
  tests/runtime/test_loader_allowlist.py \
  tests/runtime/test_call_tool_per_call_subprocess.py \
  tests/runtime/test_invalidator.py \
  tests/runtime/test_mcp_proxy_state.py \
  tests/harness/test_mcp_schema_filter.py \
  -q
```
Expected: all PASS.

- [ ] **Step 2: Run the broader runtime + harness suites for regressions**

```bash
uv run pytest tests/runtime tests/harness -q
```
Expected: PASS (no new failures vs. the recorded baseline).

- [ ] **Step 3: Run the integration loader test (requires Docker)**

```bash
uv run pytest tests/integration/test_loader_agent_scoped.py -q
```
Expected: PASS.

- [ ] **Step 4: Final confirmation**

Confirm the dead-symbol grep from Task 6 Step 6 is still empty and the working tree is clean (`git status`).

---

## Notes for the executor

- **No org-wide fallback.** Empty `ctx.mcp_server_ids` ⇒ zero MCP tools and `tools/call` 404. This is intentional; do not add a fallback.
- **Credentials stay `(org, user)`-scoped.** Only *visibility/connection* is agent-scoped. Do not change `_resolve_credentials`.
- **Tool names stay stable.** Exposed names remain `mcp__{server}__{tool}`; only the internal `_servers` key and pool entry key gain `agent_id`.
- **Commit messages must not reference task/step numbers** (repo convention).
