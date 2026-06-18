# Cross-agent inbox "Open session" — REVISION Plan (slug-based, cross-repo)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Why this revision:** Local testing showed `api_web_url` in the runtime config is **never populated** (no code path writes it into the runtime-config blob; null for every agent), so the original new-tab path (using `agent_web_url`) can never fire. The agent's hosted page is **slug-based**: `https://<slug>.<domain>/chat/<id>` (confirmed in prod, e.g. `https://pdf-reader-2u6vbt.cloud.surogate.ai/chat/<id>`), where `<slug>` is `agent.name`. This revision sources the URL from the agent slug instead.

**What stays (already built, unchanged):** Task 2 (`/auth/config` returns `agent_id`), Task 4 (`SessionTreePanel` list/tree decoupling), and the SDK callback change + `agentId` field. Only the URL-source pieces change: `agent_web_url` → `agent_slug`, plus a new ops field and web URL construction.

**Spec:** `docs/superpowers/specs/2026-06-18-inbox-cross-agent-open-session-design.md` (revised).

## Global Constraints

- **Branches (cross-repo):** `surogates` → `fix/inbox-cross-agent-open-session` (off `master`, current). `surogate-ops` → `fix/inbox-cross-agent-open-session` (off `main`, already created). Do not switch/create other branches.
- **No AI mentions** in any commit/PR/GitHub text; no `Co-Authored-By`/"Generated with" trailers. Conventional, technical messages.
- **Node 22** for the frontends: prefix npm/npx with `PATH="$HOME/.local/node/bin:$PATH"`.
- **Python tests** via `uv run pytest`.
- Backward compatible: all new fields optional; localhost / non-`<slug>.<domain>` hosts fall back to in-place.

---

## Task 1: ops — expose the agent slug in the runtime config

**Repo:** `surogate-ops` (work from `/home/monica/invergent/surogate-ops`, branch `fix/inbox-cross-agent-open-session`).

**Files:**
- Modify: `surogate_ops/server/models/agent_runtime.py` (response model)
- Modify: `surogate_ops/server/routes/agent_runtime.py` (handler)
- Test: the existing agent_runtime endpoint test (find it under `tests/`), or add a focused one.

**Interfaces:**
- Produces: `AgentRuntimeConfigResponse.slug: Optional[str]` in the runtime-config JSON, set to `agent.name`.

- [ ] **Step 1: Write the failing test.** Find the existing test for the agent-runtime config endpoint (search `tests/` for `agent_runtime` / the runtime-config route). Add/extend a test asserting the response includes `slug` equal to the agent's `name`. If there is genuinely no endpoint test to extend, add a focused unit test that calls the handler (or constructs `AgentRuntimeConfigResponse`) and asserts `slug == agent.name`. Follow the existing ops test conventions in that file.

- [ ] **Step 2: Run it, verify it fails** (no `slug` field yet). Use the repo's pytest invocation (e.g. `uv run pytest <path> -v` or the ops standard).

- [ ] **Step 3: Add the field to the model.** In `surogate_ops/server/models/agent_runtime.py`, add to `AgentRuntimeConfigResponse` (next to `api_web_url`):

```python
    slug: Optional[str] = None
```

- [ ] **Step 4: Set it in the handler.** In `surogate_ops/server/routes/agent_runtime.py`, the handler already loads `agent` (the `Agent` row). In the `return AgentRuntimeConfigResponse(...)`, add:

```python
        slug=agent.name,
```

(`agent.name` doubles as the DNS slug — see the `Agent` model docstring.)

- [ ] **Step 5: Run the test, verify it passes.**

- [ ] **Step 6: Commit.**

```bash
git add surogate_ops/server/models/agent_runtime.py surogate_ops/server/routes/agent_runtime.py <test file>
git commit -m "feat(agent-runtime): expose agent slug (name) in the runtime config"
```

---

## Task 2: surogates — stamp `agent_slug` on inbox items (replace `agent_web_url`)

**Repo:** `surogates` (work from `/home/monica/invergent/surogates`, branch `fix/inbox-cross-agent-open-session`).

**Files:**
- Modify: `surogates/api/routes/inbox.py` (`_resolve_agent_fields`, `_serialize_item`)
- Test: `tests/test_inbox_serialize.py`

**Interfaces:**
- Consumes: the runtime-config payload now has `slug` (Task 1). `payload = await runtime_config_cache.get(agent_id)` → `payload.get("slug")`.
- Produces: serialized inbox item has `agent_id` + **`agent_slug`** (no more `agent_web_url`). `_resolve_agent_fields` returns `{session_id: {"agent_id", "agent_slug"}}`.

- [ ] **Step 1: Update the failing tests.** In `tests/test_inbox_serialize.py`, replace `agent_web_url` with `agent_slug` and make the fake cache return a slug. Specifically:
  - `test_serialize_item_includes_agent_fields`: pass `{"agent_id": "agent-x", "agent_slug": "agent-x-slug"}`; assert `out["agent_slug"] == "agent-x-slug"` (and `out["agent_id"] == "agent-x"`).
  - `test_serialize_item_defaults_agent_fields_to_none`: assert `out["agent_slug"] is None` (and `agent_id is None`).
  - `_FakeCache.get` returns `{"slug": self._urls[agent_id]}` (rename the attr if you like); the two `_resolve_agent_fields` tests assert `fields[sid] == {"agent_id": "agent-x", "agent_slug": "agent-x-slug"}` and the cache-miss case `{"agent_id": "agent-x", "agent_slug": None}`.

- [ ] **Step 2: Run, verify it fails.** `uv run pytest tests/test_inbox_serialize.py -v`.

- [ ] **Step 3: Update `_resolve_agent_fields`** in `surogates/api/routes/inbox.py`: read the slug instead of the url, and key the result as `agent_slug`:

```python
    for sid, agent_id in agent_by_session.items():
        if agent_id not in slug_by_agent:
            slug = None
            if cache is not None:
                try:
                    payload = await cache.get(agent_id)
                    slug = payload.get("slug")
                except LookupError:
                    slug = None
            slug_by_agent[agent_id] = slug
        out[sid] = {"agent_id": agent_id, "agent_slug": slug_by_agent[agent_id]}
    return out
```

(Rename the local `url_by_agent` → `slug_by_agent` and update the docstring to say `agent_slug` is best-effort.)

- [ ] **Step 4: Update `_serialize_item`** — replace the `agent_web_url` line with:

```python
        "agent_slug": fields.get("agent_slug"),
```

(Keep `"agent_id": fields.get("agent_id"),`.)

- [ ] **Step 5: Run the tests, verify they pass.** `uv run pytest tests/test_inbox_serialize.py -v`.

- [ ] **Step 6: Commit.**

```bash
git add surogates/api/routes/inbox.py tests/test_inbox_serialize.py
git commit -m "feat(inbox): stamp owner agent_slug (was agent_web_url) on inbox items"
```

---

## Task 3: SDK — `agentSlug` field (replace `agentWebUrl`) + web mapper

**Repo:** `surogates`. **Files:**
- Modify: `sdk/agent-chat-react/src/types.ts`
- Modify: `web/src/api/inbox.ts`
- Test: `sdk/agent-chat-react/tests/inbox-panel.test.tsx`

**Interfaces:**
- Produces: `AgentChatInboxItem.agentSlug?: string | null` (replaces `agentWebUrl`). Web `toInboxItem` maps `agentSlug: item.agent_slug ?? null`.

- [ ] **Step 1: Update the test.** In `sdk/agent-chat-react/tests/inbox-panel.test.tsx`, the test that asserts the item passes to `onSessionSelect`: set `items[0].agentSlug = "other-agent"` (instead of `agentWebUrl`), and assert the passed item's `agentSlug` is `"other-agent"` (keep asserting `agentId`). If a test references `agentWebUrl` anywhere, update it.

- [ ] **Step 2: Run, verify it fails.** `PATH="$HOME/.local/node/bin:$PATH" npm test --prefix sdk/agent-chat-react -- inbox-panel`.

- [ ] **Step 3: Rename the type field.** In `sdk/agent-chat-react/src/types.ts`, in `AgentChatInboxItem`, replace `agentWebUrl?: string | null;` with:

```ts
  agentSlug?: string | null;
```

(Keep `agentId?: string | null;`.)

- [ ] **Step 4: Update the web mapper.** In `web/src/api/inbox.ts`: in `InboxItemResponse` replace `agent_web_url?: string | null;` with `agent_slug?: string | null;`; in `toInboxItem` replace the `agentWebUrl` mapping with `agentSlug: item.agent_slug ?? null,` (keep `agentId`).

- [ ] **Step 5: Run the SDK test, verify it passes.** `PATH="$HOME/.local/node/bin:$PATH" npm test --prefix sdk/agent-chat-react -- inbox-panel`.

- [ ] **Step 6: Commit.**

```bash
git add sdk/agent-chat-react/src/types.ts web/src/api/inbox.ts sdk/agent-chat-react/tests/inbox-panel.test.tsx
git commit -m "feat(agent-chat): inbox item carries agentSlug (was agentWebUrl)"
```

---

## Task 4: web — build the cross-agent URL from the current host + slug

**Repo:** `surogates`. **Files:**
- Modify: `web/src/features/inbox/inbox-page.tsx`

**Interfaces:**
- Consumes: `item.agentSlug`, `currentAgentId` (from the capabilities store, unchanged).

> No web test harness — verify with `PATH="$HOME/.local/node/bin:$PATH" npm run typecheck --prefix web` (must exit 0) + the local manual check.

- [ ] **Step 1: Replace the routing in `handleSessionSelect`.** In `web/src/features/inbox/inbox-page.tsx`, add a host-derivation helper and use the slug:

```ts
// Build the owning agent's hosted-page URL from THIS app's host: prod serves
// each agent at <slug>.<domain> (e.g. agent.cloud.surogate.ai), so swap the
// current first label for the owner's slug. Returns null when the host has no
// derivable domain (e.g. localhost:5174 in dev) — caller falls back to in-place.
function crossAgentChatUrl(agentSlug: string, sessionId: string): string | null {
  const { protocol, host } = window.location;
  const dot = host.indexOf(".");
  if (dot <= 0) return null; // localhost / single-label host → not derivable
  const domain = host.slice(dot + 1);
  return `${protocol}//${agentSlug}.${domain}/chat/${sessionId}`;
}
```

And the handler:

```ts
  function handleSessionSelect(sessionId: string, item?: AgentChatInboxItem) {
    if (
      item?.agentId &&
      currentAgentId &&
      item.agentId !== currentAgentId &&
      item.agentSlug
    ) {
      const url = crossAgentChatUrl(item.agentSlug, sessionId);
      if (url) {
        window.open(url, "_blank", "noopener");
        return;
      }
    }
    setActiveSession(sessionId);
    void navigate({ to: "/chat/$sessionId", params: { sessionId } });
  }
```

(Remove the old `agentWebUrl`-based branch.)

- [ ] **Step 2: Typecheck.** `PATH="$HOME/.local/node/bin:$PATH" npm run typecheck --prefix web` → exit 0.

- [ ] **Step 3: Commit.**

```bash
git add web/src/features/inbox/inbox-page.tsx
git commit -m "feat(web): build cross-agent inbox URL from host + owner slug"
```

---

## Self-Review

**Spec coverage:** ops slug (Task 1) → surogates stamps `agent_slug` (Task 2) → SDK `agentSlug` + web mapper (Task 3) → web builds `<slug>.<domain>/chat/<id>` (Task 4). `/auth/config` agent_id + list/tree decoupling already built. Edge cases: missing slug / non-derivable domain → in-place (Task 4 guard).

**Placeholder scan:** R1's test step references "the existing test" because the ops test file must be located first — the assertion (response `slug == agent.name`) is concrete. All other steps have exact code.

**Type consistency:** ops `slug` (response) → `payload.get("slug")` (Task 2) → serialized `agent_slug` (snake) → `agentSlug` (camel, SDK + web mapper + web handler). `crossAgentChatUrl(agentSlug, sessionId)` used by `handleSessionSelect`. `agentId`/`currentAgentId` unchanged from the original Task 5.
