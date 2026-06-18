# Agent-aware "Open session" from the cross-agent inbox

**Date:** 2026-06-18
**Status:** Approved, ready for planning
**Scope:** surogates only — backend runtime + SDK (`agent-chat-react`) + web app. Ops/Studio is verify-only.
**Branch:** `fix/inbox-cross-agent-open-session` (off `master`).

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

> **Refinement (during planning):** the same/different-agent decision is made **client-side
> by comparing agent ids**, not by a backend `is_current` flag. Computing `is_current` on the
> inbox item would force the user-scoped inbox endpoint to resolve an agent (it currently does
> not, and its integration tests call it with no agent), breaking that contract. Instead the
> backend stamps each item with its owner `agent_id` + `agent_web_url`, and exposes the
> **current** agent id via the already-agent-resolved `/auth/config`. Behavior is identical.

### 1. Backend — stamp owner agent on inbox items + expose current agent
Two additive, non-breaking changes:

- **Inbox items** (`surogates/api/routes/inbox.py`): serialize `agent_id` (owner, from the
  item's `session.agent_id`) and `agent_web_url` (owner agent's `api_web_url`, read from that
  agent's runtime config via the runtime-config cache keyed by `agent_id`). The serializer
  currently takes only `item`; the list path batches `session_id -> agent_id` (one query) and
  dedupes the per-owner-agent `api_web_url` cache lookups; single-item paths resolve one each.
  The endpoint stays **user-scoped** — no agent resolution added, no contract change.
- **Current agent** (`surogates/api/routes/auth.py`): add `agent_id` to `AuthConfigResponse`
  (the `/auth/config` handler already resolves the agent via `agent_runtime_context_dep`, so
  `agent_runtime.agent_id` is in hand). This is how the web learns its own agent id.

### 2. SDK — carry the fields and pass the item to the handler
- Add optional `agentId?: string | null`, `agentWebUrl?: string | null` to
  `AgentChatInboxItem` (`types.ts`); map them in the web and ops `api/inbox.ts` response
  mappers (ops may leave them undefined).
- `InboxPanel`'s Open-session button calls `onSessionSelect?.(item.sessionId, item)` —
  adding the full item as an **optional second argument**. The `onSessionSelect` prop type
  becomes `(sessionId: string, item?: AgentChatInboxItem) => void`. This is backward
  compatible: an existing `(sessionId) => …` handler (ops) remains assignable and untouched.

### 3. Web — route by comparing the item's agent to the current agent
- The web stores its **current agent id** from `/auth/config` (the capabilities fetch it
  already makes on load).
- `handleSessionSelect` (`web/src/features/inbox/inbox-page.tsx`) becomes `(sessionId, item?)`:
  - If `item?.agentId && item.agentId !== currentAgentId && item.agentWebUrl` →
    `window.open(\`${item.agentWebUrl}/chat/${sessionId}\`, "_blank", "noopener")` (new tab,
    owner's app).
  - Otherwise (same agent, or fields absent) → today's in-place behavior:
    `setActiveSession(sessionId)` + `navigate({ to: "/chat/$sessionId" })`.

### 4. Secondary hardening — decouple list from tree (SDK)
In `SessionTreePanel.refetch` (`sdk/.../components/sessions/session-tree-panel.tsx`), stop
letting a failed `getSessionTree` discard the list. Run the two fetches independently
(`Promise.allSettled`, or await each in its own try/catch): build `nodes` from whichever
results succeeded, and only surface an error / withhold `hasEverLoaded` when the *list*
fetch itself fails. A failed active-session tree must not blank the list.

## Error handling / edge cases

- Different agent but `agent_web_url` missing/null → fall back to in-place (never worse than today).
- Item agent fields absent, or current agent id unknown (ops, pre-deploy items) → in-place.
  Fully backward compatible.
- `window.open` blocked by a popup blocker → acceptable; it fires from a direct user click, so
  browsers normally allow it. No special handling planned.

## Testing

- **Backend:** the inbox serializer includes `agent_id` + `agent_web_url` (owner) on items;
  an item whose session belongs to another agent serializes that agent's id + `agent_web_url`.
  `/auth/config` returns the current `agent_id`.
- **SDK:** `InboxPanel` passes the item as the 2nd arg to `onSessionSelect`;
  `SessionTreePanel` still renders the list when `getSessionTree` rejects (list 200, tree
  404 → list shown, no blank).
- **Web:** the handler calls `window.open` with `\`${agentWebUrl}/chat/${sessionId}\`` for a
  different-agent item and navigates in place for a same-agent item; falls back to in-place
  when `agentWebUrl` is absent.

## Local testing caveat

`agent_web_url` points at the cluster/prod host, so the *new tab itself* will not load on
localhost. Locally we verify the logic (correct branch + the URL passed to `window.open`);
the real cross-agent tab is confirmed in a prod-like environment.

## Cross-repo note

Per the standing rule, chat-menu / SDK / adapter changes are checked in both ops and the web
app. Here the SDK `InboxPanel` and `AgentChatInboxItem` changes are backward compatible, ops's
inbox is per-agent (no cross-agent case), and ops's `onSessionSelect` handler stays valid — so
ops needs verification only, not a behavioral change.
