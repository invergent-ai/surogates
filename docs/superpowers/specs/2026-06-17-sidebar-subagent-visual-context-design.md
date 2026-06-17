# Sidebar sub-agent visual context

**Date:** 2026-06-17
**Repos affected:** `surogates` (SDK - primary), `surogate-ops` (frontend - dep bump after SDK publish)
**Tracking:** `~/invergent/Misc/issues/session-sidebar-issues.md`

## Problem

Sub-agent sessions in the sidebar are visually indistinguishable from regular sessions
except for a 12 px indent and a collapse chevron on the parent row. It is not obvious
at a glance which rows belong to the current context and which are unrelated past
conversations.

## Goal

Make the active selection context legible without adding new UI elements. The user
should immediately see which sessions are "part of where I am right now" vs. unrelated.

## Design

### Color model (3 states per row)

| State | Condition | Style |
|---|---|---|
| **Active** | `entry.id === activeSessionId` | `bg-line` (unchanged, the current gray) |
| **In-group** | same group as active, not the active node | `bg-line/40` (lighter tint of the same gray) |
| **Unrelated** | not in the active group | `bg-transparent` (unchanged) |

The exact opacity divisor for the in-group tint (`/40`) should be confirmed visually
during implementation. The goal: clearly lighter than active, clearly distinct from
the transparent unrelated rows.

### Group definition

A **group** is one top-level session (a node with `parentId === null`) plus all its
recursive descendants.

The **active group** is the group that contains the current `activeSessionId`.

To find which group a given node belongs to: walk its `parentId` chain to the root
(the first ancestor with `parentId === null`). All nodes sharing that root are in
the same group. In practice the tree is at most two levels deep (parent + direct
children), so the root of any node is either itself (if `parentId === null`) or its
direct parent.

### Expand / collapse behavior

| Event | Effect on parent rows |
|---|---|
| `activeSessionId` enters a group | That group's root auto-expands |
| `activeSessionId` moves to a different group | Previous group's root auto-collapses |
| `activeSessionId` becomes `undefined` / `null` (agent deselected or navigated away) | All parent rows collapse |
| User clicks the chevron | Manual toggle; wins until the next group-change event overrides it |

The auto-expand/collapse is driven by `isInActiveGroup`. A `useEffect` in `TreeNodeRow`
keyed on `isInActiveGroup` sets `expanded` to `true` when entering the group and
`false` when leaving.

### Unchanged

- Row structure, icons, delete/stop buttons
- `border-l-primary` active indicator on the active row
- Sessions with no children (no group membership, no lighter tint)
- The collapsible Sessions section header (landed in prior fix)

## Scope of changes

### SDK (`surogates/sdk/agent-chat-react`)

**`src/components/sessions/session-tree-panel.tsx`** - only file changed in the SDK.

1. Compute `activeGroupRootId` at the panel level once per render. Walk the flat
   `nodes` list to find the active node, then walk its `parentId` to find the root.
   A node with `parentId === null` is its own root.

2. Pass `activeGroupRootId` into every `TreeNodeRow` as a prop.

3. In `TreeNodeRow`: derive
   `isInActiveGroup = activeGroupRootId !== null && (entry.id === activeGroupRootId || entry.parentId === activeGroupRootId)`.
   This covers the depth-1 case (root and its direct children).

4. Apply `bg-line/40` to rows where `isInActiveGroup && !isActive`.

5. Drive `expanded` from `isInActiveGroup`:
   ```ts
   useEffect(() => {
     if (hasChildren) setExpanded(isInActiveGroup);
   }, [isInActiveGroup, hasChildren]);
   ```

**`tests/session-tree-panel.test.tsx`** - new cases added to the existing suite:

- Parent active: children rendered with the in-group class; unrelated parent rows
  collapsed (their children absent from the DOM).
- Child active: that child gets the active class; parent and siblings get the
  in-group class; unrelated parent rows collapsed.
- No active session (`activeSessionId` undefined): all parent rows collapsed.

### `surogate-ops` (separate PR, after SDK publish)

No code change beyond a dep bump. Update `@invergent/agent-chat-react` in
`surogate-ops/frontend/package.json` to the new published version, run
`npm install`, verify `npm run typecheck` passes.

## Non-goals

- Session rename / tooltip (issue #2, tracked separately)
- Deep-linking to skill selection (separate enhancement)
- Any change to the session list API or data model
