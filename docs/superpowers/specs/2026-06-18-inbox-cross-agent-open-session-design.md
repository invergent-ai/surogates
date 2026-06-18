# Agent-aware "Open session" from the cross-agent inbox

**Date:** 2026-06-18
**Status:** Approved, ready for planning
**Scope:** cross-repo — `surogates` (runtime inbox + SDK `agent-chat-react` + web app) and a one-field `surogate-ops` backend change (expose the agent slug in the runtime config). Ops *frontend* is verify-only.
**Branches:** `surogates` `fix/inbox-cross-agent-open-session` (off `master`); `surogate-ops` `fix/inbox-cross-agent-open-session` (off `main`).

## Problem

The agent web app's inbox is **per-user and cross-agent**: `store.list_inbox` filters by
`user_id` only (`surogates/session/store.py:921`), so a user with accounts on several
agents sees inbox items from all of them in whichever agent's web app they're viewing.

When you click **Open session** on an item that belongs to a *different* agent, three
things go wrong (all reproduced locally on 2026-06-18):

1. **Wrong agent context** — it loads in the current agent's app instead of the owning agent's.
2. **Same window** — it navigates in place; it should open the owning agent's app in a new tab.
3. **Session list empties** — the left list goes blank until you open a session that belongs
   to the current agent.

### Confirmed root cause

- **No agent identity on inbox items, at any layer.** The runtime serializer
  `_serialize_item` (`surogates/api/routes/inbox.py:67-85`) emits `session_id` but no
  `agent_id`/slug/url; the SDK type `AgentChatInboxItem`
  (`sdk/agent-chat-react/src/types.ts:431`) and the web mapping `toInboxItem`
  (`web/src/api/inbox.ts:37`) carry none. The SDK "Open session" button passes only
  `item.sessionId` to `onSessionSelect` (`sdk/.../components/inbox/inbox-panel.tsx:204`),
  and the web handler unconditionally does `setActiveSession` + `navigate("/chat/$sessionId")`
  (`web/src/features/inbox/inbox-page.tsx:25-28`). The web frontend doesn't even track its
  own agent (resolved server-side from Host/`?agent_id`, `surogates/runtime/resolver.py:136-159`).
  → symptoms #1 and #2.
- **Symptom #3** is a downstream effect: a foreign session is unloadable in the current
  agent's scope by design — `_get_session_for_tenant` 404s when `session.agent_id != agent_id`
  ("callers cannot probe session existence across scopes", `surogates/api/routes/sessions.py:531-562`).
  The SDK `SessionTreePanel.refetch` fetches the list and the active session's tree together in
  one `Promise.all` (`sdk/.../components/sessions/session-tree-panel.tsx:402-405`); the foreign
  tree's 404 rejects the whole call, the `catch` never sets `nodes`/`hasEverLoaded`, and the
  render guards return `null` — discarding the *successful* list fetch and blanking the panel.

### Why ops is not affected

Ops/Studio's inbox is **per-agent**: its backend query joins the session and filters
`SessionRow.agent_id == agent_id` (`surogate-ops/.../core/surogates_client.py:1770`), so an
ops agent inbox only ever lists that agent's items. "Open session" there always targets the
correct agent. Ops is touched only to stay compatible with the SDK change (verify-only).

## Goals

- A different-agent inbox session opens in that agent's app, in a new browser tab.
- A same-agent inbox session opens in place (unchanged from today).
- The session list never blanks because the *active* session's tree fetch failed.
- Backward compatible: ops and any pre-deploy items keep working (in-place) with no change.

## Non-goals

- No change to ops behavior (its per-agent inbox is already correct).
- No change to inbox scoping (the cross-agent, user-scoped inbox is intended).
- No new "agent switcher" UI; we only fix the Open-session target.

## Design

> **Revision (2026-06-18, after local testing):** the cross-agent URL is built from the
> owning agent's **slug** (`agent.name`), NOT from `api_web_url`. Investigation (DB + code)
> showed `api_web_url` in the runtime config is **never populated** — no code path writes it
> into the runtime-config blob, and it is null for every agent — so the originally-specced
> new-tab path (using `agent_web_url`) could never fire, in any environment. The agent's
> hosted page is slug-based: `https://<slug>.<domain>/chat/<id>` (confirmed in prod, e.g.
> `https://pdf-reader-2u6vbt.cloud.surogate.ai/chat/<id>`). The same/different-agent decision
> is still made client-side by comparing agent ids (via `/auth/config`'s `agent_id`).
> **Unaffected by this revision (already built):** `/auth/config` returning `agent_id`
> (Design 2 below) and the list/tree decoupling (Design 4).

### 1. Backend — expose the owner agent's slug + stamp it on inbox items
Cross-repo: the slug lives in **ops** (`agent.name`); the surogates runtime only knows the
agent id, so ops must surface the slug in the runtime config it serves.

- **ops** (`surogate_ops/server/routes/agent_runtime.py`): add the agent's slug (`agent.name`)
  to the runtime-config response (`AgentRuntimeConfigResponse`). The handler already loads the
  `Agent` row, so it is a one-field addition. This lets the surogates runtime learn an agent's
  slug from its (cached) runtime config.
- **surogates** (`surogates/api/routes/inbox.py`): the runtime-config payload now carries the
  slug; stamp `agent_slug` (owner, from that agent's runtime-config payload, best-effort/None
  on cache miss) on each inbox item alongside `agent_id`. (Replaces the always-null
  `agent_web_url`.) The list path still batches `session_id -> agent_id` and dedupes the
  per-owner-agent slug lookups. The inbox endpoint stays **user-scoped**.
- **Current agent** (`surogates/api/routes/auth.py`): `/auth/config` returns `agent_id` —
  DONE, unchanged by this revision.

### 2. SDK — carry the fields and pass the item to the handler
- Add optional `agentId?: string | null` and **`agentSlug?: string | null`** to
  `AgentChatInboxItem` (`types.ts`); map them in the **web** `api/inbox.ts` response mapper.
  (ops frontend is a separate repo on the published SDK — verify-only, no ops mapper change.)
- `InboxPanel`'s Open-session button calls `onSessionSelect?.(item.sessionId, item)` (item as
  an optional 2nd arg) — DONE for `agentId` + the callback; this revision swaps the
  `agentWebUrl` field for `agentSlug`.

### 3. Web — build the owner's URL from the current host + slug
- The web stores its **current agent id** from `/auth/config` (DONE).
- `handleSessionSelect(sessionId, item?)`:
  - If `item?.agentId && currentAgentId && item.agentId !== currentAgentId && item.agentSlug`
    AND the current host is a `<slug>.<domain>` form (has a parseable multi-label domain, not
    `localhost`): derive `domain` from `window.location.host` (drop the current first label),
    then `window.open(\`${protocol}//${item.agentSlug}.${domain}/chat/${sessionId}\`, "_blank",
    "noopener")`.
  - Otherwise (same agent, missing slug, or no derivable domain — e.g. localhost) → in-place
    `setActiveSession(sessionId)` + `navigate({ to: "/chat/$sessionId" })`.

### 4. Secondary hardening — decouple list from tree (SDK)
In `SessionTreePanel.refetch` (`sdk/.../components/sessions/session-tree-panel.tsx`), stop
letting a failed `getSessionTree` discard the list. Run the two fetches independently
(`Promise.allSettled`, or await each in its own try/catch): build `nodes` from whichever
results succeeded, and only surface an error / withhold `hasEverLoaded` when the *list*
fetch itself fails. A failed active-session tree must not blank the list.

## Error handling / edge cases

- Different agent but `agent_slug` missing/null, OR no derivable domain (e.g. `localhost`,
  single-label host) → fall back to in-place (never worse than today).
- Item agent fields absent, or current agent id unknown (pre-deploy items) → in-place.
  Fully backward compatible.
- `window.open` blocked by a popup blocker → acceptable; it fires from a direct user click, so
  browsers normally allow it. No special handling planned.

## Testing

- **ops:** the runtime-config response includes the agent's slug (`name`).
- **surogates backend:** the inbox serializer includes `agent_id` + `agent_slug` (owner) on
  items; an item whose session belongs to another agent serializes that agent's id + slug.
  `/auth/config` returns the current `agent_id` (done).
- **SDK:** `InboxPanel` passes the item as the 2nd arg to `onSessionSelect` (done);
  `AgentChatInboxItem` carries `agentSlug`; `SessionTreePanel` still renders the list when
  `getSessionTree` rejects (done).
- **Web:** no component test harness — verify by typecheck + manual. Handler builds
  `<protocol>//<agentSlug>.<domain>/chat/<sessionId>` from `window.location` for a
  different-agent item (when a domain is derivable), navigates in place for a same-agent item,
  and falls back to in-place when the slug or domain is unavailable.

## Local testing caveat

The web app runs at `localhost:5174` locally (no `<slug>.<domain>` host), so the domain is not
derivable and a cross-agent click correctly falls back to **in-place** — the new tab cannot be
exercised locally. The cross-agent tab is confirmed in a prod-like environment (web served at
`<slug>.cloud.surogate.ai`). Locally, verify: cross-agent detection (agent ids differ), the
in-place fallback, and the list-no-blank fix.

## Cross-repo note

This revision touches **two repos**: `surogate-ops` (expose the agent slug in the runtime
config) and `surogates` (carry the slug through the inbox + build the URL in web). Per the
standing rule, the shared SDK change is backward compatible; the ops *frontend* inbox is
per-agent (no cross-agent case) and stays verify-only — the only ops change is the backend
runtime-config field. Branches: `surogates` off `master`, `surogate-ops` off `main` (both
`fix/inbox-cross-agent-open-session`).
