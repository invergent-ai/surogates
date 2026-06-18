# Cross-agent inbox "Open session" — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** From the web app's user-scoped (cross-agent) inbox, "Open session" opens a different agent's session in that agent's app (new tab) and a same-agent session in place; and the session list never blanks when the active session's tree fetch fails.

**Architecture:** Backend stamps each inbox item with its owner `agent_id` + `agent_web_url` and exposes the current agent id via `/auth/config`; the SDK carries those fields and passes the item to `onSessionSelect`; the web compares the item's agent to its own and routes (new tab vs in place). Separately, `SessionTreePanel` decouples its list fetch from the active-session tree fetch so a failed tree doesn't discard the list.

**Tech Stack:** Python/FastAPI + SQLAlchemy (surogates runtime), React + TypeScript SDK (`agent-chat-react`, vitest), React web app (`web`, Vite, Node 22).

**Spec:** `docs/superpowers/specs/2026-06-18-inbox-cross-agent-open-session-design.md`

## Global Constraints

- **Branch:** `fix/inbox-cross-agent-open-session` (already created off `master`). Do not switch branches.
- **No AI mentions in any commit/PR/GitHub text.** Technical content only; no `Co-Authored-By`/"Generated with" trailers.
- **Backward compatible:** all new inbox-item fields are optional; `onSessionSelect`'s new arg is optional; ops and pre-deploy items keep working (in-place). Do not change the inbox endpoint's user-scoped contract (no agent resolution added there).
- **Node 22 for the frontends:** prefix npm/npx with `PATH="$HOME/.local/node/bin:$PATH"` (system node is 18).
- **Commit messages:** Conventional, technical, no attribution.

## File structure

- `surogates/session/store.py` — add `get_agent_ids_for_sessions` (batch session→agent_id).
- `surogates/api/routes/inbox.py` — `_resolve_agent_fields` helper; `_serialize_item` gains optional agent fields; wire all 5 serialize sites.
- `surogates/api/routes/auth.py` — `AuthConfigResponse` gains `agent_id`.
- `tests/test_inbox_serialize.py` (new, unit) — serializer + helper.
- `tests/integration/test_inbox_api.py` — assert listed items carry `agent_id`.
- `sdk/agent-chat-react/src/types.ts` — `AgentChatInboxItem` gains `agentId?`, `agentWebUrl?`.
- `sdk/agent-chat-react/src/components/inbox/inbox-panel.tsx` — pass item to `onSessionSelect`.
- `sdk/agent-chat-react/src/components/sessions/session-tree-panel.tsx` — decouple list/tree fetch.
- `web/src/api/inbox.ts`, `surogate-ops/frontend/src/api/inbox.ts` — map the new fields.
- `web/src/api/auth.ts`, `web/src/stores/capabilities-slice.ts` — read + store current `agentId`.
- `web/src/features/inbox/inbox-page.tsx` — agent-aware routing.
- `sdk/agent-chat-react/tests/inbox-panel.test.tsx`, `tests/session-tree-panel.test.tsx` — SDK tests.

---

## Task 1: Backend — stamp owner `agent_id` + `agent_web_url` on inbox items

**Files:**
- Modify: `surogates/session/store.py`
- Modify: `surogates/api/routes/inbox.py`
- Test: `tests/test_inbox_serialize.py` (new, unit — no DB)

**Interfaces:**
- Produces: `SessionStore.get_agent_ids_for_sessions(session_ids: list[UUID]) -> dict[UUID, str]`; `_serialize_item(item, agent_fields: dict | None = None) -> dict` where the dict gains `"agent_id"` and `"agent_web_url"`; `async _resolve_agent_fields(request, session_ids) -> dict[UUID, dict]`.

- [ ] **Step 1: Write the failing unit test**

Create `tests/test_inbox_serialize.py`:

```python
# Copyright (c) 2026, Invergent SA, developed by Flavius Burca
# SPDX-License-Identifier: AGPL-3.0-only
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from surogates.api.routes.inbox import _resolve_agent_fields, _serialize_item


def _item(session_id):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=1, org_id=uuid.uuid4(), user_id=uuid.uuid4(), session_id=session_id,
        source_event_id=1, kind="task_complete", status="pending",
        title="t", body=None, payload={}, action_ref=None,
        created_at=now, updated_at=now, read_at=None, responded_at=None,
    )


def test_serialize_item_includes_agent_fields():
    sid = uuid.uuid4()
    out = _serialize_item(_item(sid), {"agent_id": "agent-x", "agent_web_url": "https://x.example"})
    assert out["agent_id"] == "agent-x"
    assert out["agent_web_url"] == "https://x.example"


def test_serialize_item_defaults_agent_fields_to_none():
    out = _serialize_item(_item(uuid.uuid4()))
    assert out["agent_id"] is None
    assert out["agent_web_url"] is None


class _FakeStore:
    def __init__(self, mapping):
        self._mapping = mapping
    async def get_agent_ids_for_sessions(self, session_ids):
        return {s: self._mapping[s] for s in session_ids if s in self._mapping}


class _FakeCache:
    def __init__(self, urls):
        self._urls = urls
    async def get(self, agent_id):
        if agent_id not in self._urls:
            raise LookupError(agent_id)
        return {"api_web_url": self._urls[agent_id]}


def _request(store, cache):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        session_store=store, runtime_config_cache=cache)))


@pytest.mark.asyncio
async def test_resolve_agent_fields_maps_owner_and_url():
    sid = uuid.uuid4()
    req = _request(_FakeStore({sid: "agent-x"}), _FakeCache({"agent-x": "https://x.example"}))
    fields = await _resolve_agent_fields(req, [sid])
    assert fields[sid] == {"agent_id": "agent-x", "agent_web_url": "https://x.example"}


@pytest.mark.asyncio
async def test_resolve_agent_fields_url_none_on_cache_miss():
    sid = uuid.uuid4()
    req = _request(_FakeStore({sid: "agent-x"}), _FakeCache({}))
    fields = await _resolve_agent_fields(req, [sid])
    assert fields[sid] == {"agent_id": "agent-x", "agent_web_url": None}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_inbox_serialize.py -v`
Expected: FAIL — `_resolve_agent_fields` not importable / `_serialize_item` missing agent fields.

- [ ] **Step 3: Add the store batch helper**

In `surogates/session/store.py`, add a method to the `SessionStore` class (near `get_session`, ~line 174). Confirm `Session` is already imported in this module (it is — `list_inbox` returns `InboxItem` and the file imports the models):

```python
    async def get_agent_ids_for_sessions(
        self, session_ids: list[UUID]
    ) -> dict[UUID, str]:
        """Map each session id to its owning agent id (one query)."""
        if not session_ids:
            return {}
        async with self._sf() as db:
            result = await db.execute(
                select(Session.id, Session.agent_id).where(
                    Session.id.in_(session_ids)
                )
            )
            return {row.id: str(row.agent_id) for row in result.all()}
```

- [ ] **Step 4: Add the resolver + extend the serializer in `inbox.py`**

In `surogates/api/routes/inbox.py`, add `from fastapi import Request` if not already imported (it is — the routes use `request: Request`). Add the helper above `_serialize_item`:

```python
async def _resolve_agent_fields(
    request: Request, session_ids: list[UUID]
) -> dict:
    """Map session_id -> {"agent_id", "agent_web_url"} for serialization.

    `agent_web_url` is best-effort: a runtime-config cache miss leaves it
    None so the item still serializes (the web then falls back to in-place).
    """
    store = request.app.state.session_store
    agent_by_session = await store.get_agent_ids_for_sessions(list(session_ids))
    cache = getattr(request.app.state, "runtime_config_cache", None)
    url_by_agent: dict[str, str | None] = {}
    out: dict = {}
    for sid, agent_id in agent_by_session.items():
        if agent_id not in url_by_agent:
            web_url = None
            if cache is not None:
                try:
                    payload = await cache.get(agent_id)
                    web_url = payload.get("api_web_url")
                except LookupError:
                    web_url = None
            url_by_agent[agent_id] = web_url
        out[sid] = {"agent_id": agent_id, "agent_web_url": url_by_agent[agent_id]}
    return out
```

Change `_serialize_item` (line 67) to accept optional agent fields and add them to the dict:

```python
def _serialize_item(item, agent_fields: dict | None = None) -> dict:
    fields = agent_fields or {}
    return {
        "id": item.id,
        "org_id": str(item.org_id),
        "user_id": str(item.user_id),
        "session_id": str(item.session_id),
        "source_event_id": item.source_event_id,
        "kind": item.kind,
        "status": item.status,
        "title": item.title,
        "body": item.body,
        "payload": item.payload,
        "action_ref": item.action_ref,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "read_at": item.read_at.isoformat() if item.read_at else None,
        "responded_at": item.responded_at.isoformat()
        if item.responded_at
        else None,
        "agent_id": fields.get("agent_id"),
        "agent_web_url": fields.get("agent_web_url"),
    }
```

- [ ] **Step 5: Run the unit test to verify it passes**

Run: `uv run pytest tests/test_inbox_serialize.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Wire the 5 serialize call sites**

In `inbox.py`, update each serialize site to resolve and pass agent fields. The LIST handler (line ~125) — replace the return:

```python
    agent_fields = await _resolve_agent_fields(
        request, [item.session_id for item in items]
    )
    return {
        "items": [
            _serialize_item(item, agent_fields.get(item.session_id))
            for item in items
        ],
        "next_cursor": next_cursor,
    }
```

For each single-item return (`return _serialize_item(item)` at ~208, ~226, ~259, ~354), replace with:

```python
    agent_fields = await _resolve_agent_fields(request, [item.session_id])
    return _serialize_item(item, agent_fields.get(item.session_id))
```

If any of those four handlers does not already have `request: Request` in its signature, add it (FastAPI injects it). Verify by reading each handler's signature first.

- [ ] **Step 7: Run the full inbox test module + typecheck the module**

Run: `uv run pytest tests/test_inbox_serialize.py -v`
Expected: PASS.
Run: `uv run python -c "import surogates.api.routes.inbox, surogates.session.store"`
Expected: no import error.

- [ ] **Step 8: Commit**

```bash
git add surogates/session/store.py surogates/api/routes/inbox.py tests/test_inbox_serialize.py
git commit -m "feat(inbox): stamp owner agent_id and agent_web_url on inbox items"
```

---

## Task 2: Backend — expose the current agent id via `/auth/config`

**Files:**
- Modify: `surogates/api/routes/auth.py`
- Test: `tests/integration/test_inbox_api.py` (add one assertion) OR a focused unit test if the auth route is unit-testable; integration preferred since the route depends on `agent_runtime_context_dep`.

**Interfaces:**
- Produces: `AuthConfigResponse.agent_id: str` in the `GET /v1/auth/config` JSON.

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_inbox_api.py` (it already builds the app + client; reuse its fixtures). If `/auth/config` requires an agent context the existing harness can't supply, instead assert at the unit level by constructing `AuthConfigResponse` — but prefer the integration assertion. Add:

```python
async def test_auth_config_returns_current_agent_id(client, session_factory, session_store):
    # /auth/config resolves the agent via agent_runtime_context_dep; supply
    # the agent the same way the runtime expects (e.g. ?agent_id=<id>).
    # Use whatever agent the harness configures; assert the field is present
    # and equals that agent id.
    response = await client.get("/v1/auth/config?agent_id=test-agent")
    assert response.status_code == 200, response.text
    assert response.json()["agent_id"] == "test-agent"
```

If the harness cannot resolve `test-agent` (runtime-config cache miss → 404), adjust to the agent id the harness does configure, or seed the runtime-config cache in a fixture; the assertion is that the response includes `agent_id` equal to the resolved agent.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/integration/test_inbox_api.py::test_auth_config_returns_current_agent_id -v`
Expected: FAIL — response JSON has no `agent_id` key.

- [ ] **Step 3: Add `agent_id` to the response**

In `surogates/api/routes/auth.py`, add the field to `AuthConfigResponse` (line ~81):

```python
class AuthConfigResponse(BaseModel):
    """Runtime auth shape exposed by ``GET /v1/auth/config``."""

    agent_id: str
    self_registration_enabled: bool
    firebase: FirebaseWebConfig | None = None
    slash_commands: list[str] = Field(default_factory=list)
```

In the `auth_config` handler, set `agent_id=agent_runtime.agent_id` in all three `AuthConfigResponse(...)` constructions (the two early returns and the final return).

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/integration/test_inbox_api.py::test_auth_config_returns_current_agent_id -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/auth.py tests/integration/test_inbox_api.py
git commit -m "feat(auth): return current agent_id from /auth/config"
```

---

## Task 3: SDK — carry agent fields + pass the item to `onSessionSelect`

**Files:**
- Modify: `sdk/agent-chat-react/src/types.ts`
- Modify: `sdk/agent-chat-react/src/components/inbox/inbox-panel.tsx`
- Modify: `web/src/api/inbox.ts`, `surogate-ops/frontend/src/api/inbox.ts`
- Test: `sdk/agent-chat-react/tests/inbox-panel.test.tsx`

**Interfaces:**
- Consumes: backend items now include `agent_id`, `agent_web_url`.
- Produces: `AgentChatInboxItem.agentId?: string | null`, `.agentWebUrl?: string | null`; `InboxPanelProps.onSessionSelect?: (sessionId: string, item?: AgentChatInboxItem) => void`.

- [ ] **Step 1: Write the failing test**

In `sdk/agent-chat-react/tests/inbox-panel.test.tsx`, add a test that asserts the item is passed as the 2nd arg. Use the file's existing `createAdapter`/`inboxItem` helpers and render harness:

```tsx
it("passes the inbox item to onSessionSelect", async () => {
  const calls: Array<{ id: string; agentId?: string | null }> = [];
  const items = [inboxItem({ id: 1, title: "Done" })];
  items[0].agentId = "other-agent";
  items[0].agentWebUrl = "https://other.example";
  const adapter = createAdapter(items);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <InboxPanel
        adapter={adapter}
        onSessionSelect={(sessionId, item) =>
          calls.push({ id: sessionId, agentId: item?.agentId })
        }
      />,
    );
    await Promise.resolve();
  });
  // select the item, then click "Open session"
  const row = container.querySelector<HTMLButtonElement>('button[aria-label^="Open inbox item"]');
  await act(async () => { row?.click(); await Promise.resolve(); });
  const openBtn = container.querySelector<HTMLButtonElement>('button[aria-label="Open session"]');
  await act(async () => { openBtn?.click(); await Promise.resolve(); });
  expect(calls).toEqual([{ id: "session-1", agentId: "other-agent" }]);
});
```

(If `inboxItem`/`createAdapter` don't yet allow `agentId`, set it on the item object as above after construction.)

- [ ] **Step 2: Run it to verify it fails**

Run: `PATH="$HOME/.local/node/bin:$PATH" npm test --prefix sdk/agent-chat-react -- inbox-panel`
Expected: FAIL — `item` is `undefined` (only `sessionId` passed), so `agentId` is undefined and the assertion mismatches.

- [ ] **Step 3: Add the type fields**

In `sdk/agent-chat-react/src/types.ts`, in `interface AgentChatInboxItem` (line ~431), add after `respondedAt`:

```ts
  agentId?: string | null;
  agentWebUrl?: string | null;
```

- [ ] **Step 4: Pass the item from the button**

In `sdk/agent-chat-react/src/components/inbox/inbox-panel.tsx`:
- Change the prop type (line ~42): `onSessionSelect?: (sessionId: string, item?: AgentChatInboxItem) => void;`
- Change the button handler (line ~204): `onClick={() => onSessionSelect?.(item.sessionId, item)}`

- [ ] **Step 5: Map the fields in the API layers**

In `web/src/api/inbox.ts` `toInboxItem` (line ~37), add to the returned object:

```ts
    agentId: item.agent_id ?? null,
    agentWebUrl: item.agent_web_url ?? null,
```

And add `agent_id?: string | null; agent_web_url?: string | null;` to that file's `InboxItemResponse` interface. Do the same in `surogate-ops/frontend/src/api/inbox.ts` (so ops compiles; ops will simply carry nulls).

- [ ] **Step 6: Run the test to verify it passes**

Run: `PATH="$HOME/.local/node/bin:$PATH" npm test --prefix sdk/agent-chat-react -- inbox-panel`
Expected: PASS, including the existing inbox-panel tests.

- [ ] **Step 7: Commit**

```bash
git add sdk/agent-chat-react/src/types.ts sdk/agent-chat-react/src/components/inbox/inbox-panel.tsx web/src/api/inbox.ts surogate-ops/frontend/src/api/inbox.ts sdk/agent-chat-react/tests/inbox-panel.test.tsx
git commit -m "feat(agent-chat): pass inbox item (with owner agent) to onSessionSelect"
```

---

## Task 4: SDK — decouple session list from the active-session tree fetch

**Files:**
- Modify: `sdk/agent-chat-react/src/components/sessions/session-tree-panel.tsx`
- Test: `sdk/agent-chat-react/tests/session-tree-panel.test.tsx`

**Interfaces:**
- No public API change; `refetch` no longer discards the list when `getSessionTree` rejects.

- [ ] **Step 1: Write the failing test**

In `tests/session-tree-panel.test.tsx`, add (using the file's `createAdapter`/`session` helpers):

```tsx
it("keeps the list when the active session's tree fetch fails", async () => {
  const sessions = [session({ id: "s-1", title: "First session", agentId: "agent-1" })];
  const adapter: AgentChatAdapter = {
    ...createAdapter(sessions),
    async listSessions() { return { sessions, total: sessions.length }; },
    async getSessionTree() { throw new Error("404 not found"); },
  };
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <SessionTreePanel adapter={adapter} loadList sessionId="foreign-session" activeSessionId="foreign-session" />,
    );
    await Promise.resolve();
  });
  expect(container.textContent).toContain("First session");
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PATH="$HOME/.local/node/bin:$PATH" npm test --prefix sdk/agent-chat-react -- session-tree-panel`
Expected: FAIL — `Promise.all` rejects on the tree 404, the panel renders `null`, "First session" is absent.

- [ ] **Step 3: Decouple the two fetches in `refetch`**

In `session-tree-panel.tsx`, replace the `Promise.all` block (lines ~391-410) so a failed tree doesn't discard the list:

```ts
        const [listResult, treeResult] = await Promise.allSettled([
          sessionListPromise,
          sessionTreePromise,
        ]);
        if (!mounted.current || currentRequestId !== requestId.current) return;
        // The list is the panel's backbone; only a list failure is fatal.
        if (loadList && adapter.listSessions && listResult.status === "rejected") {
          throw listResult.reason;
        }
        const sessionList =
          listResult.status === "fulfilled" ? listResult.value : null;
        const sessionTree =
          treeResult.status === "fulfilled" ? treeResult.value : null;
        let nextNodes = mergeTreeNodes([
          sessionList?.sessions.map(sessionToTreeNode) ?? [],
          sessionTree?.nodes ?? [],
        ]);
```

The rest of the `try` (pendingDelete filtering, fingerprint, `setNodes`, `setError(null)`, `setHasEverLoaded(true)`) stays as-is. The existing `catch` now only triggers when the list itself fails.

- [ ] **Step 4: Run the test to verify it passes**

Run: `PATH="$HOME/.local/node/bin:$PATH" npm test --prefix sdk/agent-chat-react -- session-tree-panel`
Expected: PASS (this test plus the existing ones).

- [ ] **Step 5: Commit**

```bash
git add sdk/agent-chat-react/src/components/sessions/session-tree-panel.tsx sdk/agent-chat-react/tests/session-tree-panel.test.tsx
git commit -m "fix(agent-chat): keep session list when active session tree fetch fails"
```

---

## Task 5: Web — store current agent id + route by agent

**Files:**
- Modify: `web/src/api/auth.ts` (read `agent_id` from `/auth/config`)
- Modify: `web/src/stores/capabilities-slice.ts` (store `agentId`)
- Modify: `web/src/features/inbox/inbox-page.tsx` (route)

**Interfaces:**
- Consumes: `/auth/config` now returns `agent_id`; inbox items now carry `agentId`/`agentWebUrl`.
- Produces: app store exposes `agentId: string | null`.

> No component test harness exists in `web` (confirmed). Verify via `typecheck` + the local manual repro (the :5174 image/pdf-agent setup). The routing logic is covered by intent here; the SDK tests cover the callback contract.

- [ ] **Step 1: Read `agent_id` in the auth/config client**

In `web/src/api/auth.ts`, where `/api/v1/auth/config` is fetched (line ~176), include `agent_id` in the parsed result type and return it (the response now has it). Ensure the returned config object exposes `agent_id: string`.

- [ ] **Step 2: Store `agentId` in the capabilities slice**

In `web/src/stores/capabilities-slice.ts`: add `agentId: string | null` to the slice state (default `null`), and in `fetchCapabilities` (line ~30) extend the `set` call:

```ts
    set({ slashCommands: config.slash_commands ?? null, agentId: config.agent_id ?? null });
```

- [ ] **Step 3: Route by agent in the inbox page**

In `web/src/features/inbox/inbox-page.tsx`, read `agentId` from the store and change `handleSessionSelect` to accept the item and branch:

```tsx
  const currentAgentId = useAppStore((s) => s.agentId);

  function handleSessionSelect(sessionId: string, item?: AgentChatInboxItem) {
    if (item?.agentId && item.agentId !== currentAgentId && item.agentWebUrl) {
      window.open(`${item.agentWebUrl}/chat/${sessionId}`, "_blank", "noopener");
      return;
    }
    setActiveSession(sessionId);
    void navigate({ to: "/chat/$sessionId", params: { sessionId } });
  }
```

Import the `AgentChatInboxItem` type from `@invergent/agent-chat-react`. Ensure `InboxPage` still fetches capabilities (it calls `fetchUser`/`fetchSessions`; add `fetchCapabilities` to its mount effect if not already present so `agentId` is populated — check first).

- [ ] **Step 4: Typecheck the web app**

Run: `PATH="$HOME/.local/node/bin:$PATH" npm run typecheck --prefix web`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/auth.ts web/src/stores/capabilities-slice.ts web/src/features/inbox/inbox-page.tsx
git commit -m "feat(web): open cross-agent inbox sessions in the owning agent's app"
```

---

## Task 6: Verify — full builds + backward compatibility

**Files:** none (verification only)

- [ ] **Step 1: SDK test suite**

Run: `PATH="$HOME/.local/node/bin:$PATH" npm test --prefix sdk/agent-chat-react`
Expected: all SDK tests pass (inbox-panel, session-tree-panel, others).

- [ ] **Step 2: Backend inbox tests**

Run: `uv run pytest tests/test_inbox_serialize.py tests/integration/test_inbox_api.py -m "not browser_e2e and not browser_e2e_k8s and not live" -v`
Expected: pass (integration tests require the test DB per conftest; if unavailable, at minimum `tests/test_inbox_serialize.py` passes and you note the integration run was skipped).

- [ ] **Step 3: Web typecheck**

Run: `PATH="$HOME/.local/node/bin:$PATH" npm run typecheck --prefix web`
Expected: no errors.

- [ ] **Step 4: Confirm ops backward-compatibility**

Ops consumes the *published* SDK, not this local checkout, so no ops build runs here. Confirm by inspection that the SDK changes are backward compatible: `onSessionSelect`'s new 2nd arg is optional (ops's `(sessionId) => …` handler stays valid), and the new `AgentChatInboxItem` fields are optional. Note in the PR that ops needs no code change and will pick up the fix on the next SDK version bump.

- [ ] **Step 5: Manual local verification (web)**

With the local stack up and `web/.env.local` pointed at an agent that has a cross-agent inbox item: open `:5174` → Inbox → "Open session" on a different-agent item. Confirm it calls `window.open` with `<agent_web_url>/chat/<sessionId>` (the new tab won't load on localhost — see spec caveat) and that a same-agent item still opens in place. Confirm the session list no longer blanks.

---

## Self-Review

**Spec coverage:**
- Backend stamps `agent_id` + `agent_web_url` → Task 1. Current agent via `/auth/config` → Task 2.
- SDK carries fields + passes item → Task 3. List/tree decoupling → Task 4.
- Web stores current agent + routes (new tab vs in place) → Task 5.
- Edge cases (missing url / absent fields → in-place) → Task 5 branch condition (`item.agentId && item.agentId !== currentAgentId && item.agentWebUrl`).
- Ops verify-only / backward compat → Task 6 Step 4.

**Placeholder scan:** none — every step has concrete code/commands. The one judgment call (which agent id the auth-config integration test can resolve) is called out explicitly in Task 2 Step 1 with the fallback.

**Type consistency:** `agent_id`/`agent_web_url` (snake, backend + API JSON) map to `agentId`/`agentWebUrl` (camel, SDK type + web/ops mappers + web handler). `onSessionSelect(sessionId, item?)` signature is identical in the SDK prop type (Task 3), the button call (Task 3), and the web handler (Task 5). `get_agent_ids_for_sessions` returns `dict[UUID, str]`, consumed by `_resolve_agent_fields` (Task 1). `AuthConfigResponse.agent_id` (Task 2) → `config.agent_id` (Task 5 Step 1-2).
