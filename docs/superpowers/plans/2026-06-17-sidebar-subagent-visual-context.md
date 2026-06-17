# Sidebar Sub-agent Visual Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make sub-agent sessions visually distinct in the sidebar by applying a lighter background tint to all sessions in the active group and auto-collapsing unrelated groups.

**Architecture:** All logic lives in `SessionTreePanel` in the SDK. Compute the active group root once at the panel level from the flat nodes list; pass it down to each `TreeNodeRow`, which derives its own group membership and adjusts background color and expand/collapse state accordingly. No API or data-model changes.

**Tech Stack:** React, TypeScript, Tailwind CSS (`bg-line/40` opacity modifier), Vitest + react-dom/client

## Global Constraints

- Node 22 required: prefix every npm/npx command with `PATH="$HOME/.local/node/bin:$PATH"`
- SDK commands run from `surogates/sdk/agent-chat-react/`
- Test command: `PATH="$HOME/.local/node/bin:$PATH" npx vitest run tests/session-tree-panel.test.tsx`
- Only `src/components/sessions/session-tree-panel.tsx` and `tests/session-tree-panel.test.tsx` change in the SDK
- Do not let biome `--write` reflow whole files; keep diffs minimal

---

### Task 1: Active group computation and in-group background tint

Compute `activeGroupRootId` at the panel level, pass it into `TreeNodeRow`, and apply `bg-line/40` to rows that are in the active group but not the active session itself.

**Files:**
- Modify: `sdk/agent-chat-react/src/components/sessions/session-tree-panel.tsx`
- Test: `sdk/agent-chat-react/tests/session-tree-panel.test.tsx`

**Interfaces:**
- Produces: `activeGroupRootId: string | null` computed in `SessionTreePanel`, passed to `TreeNodeRow` as a new prop. `isInActiveGroup: boolean` derived inside `TreeNodeRow` from `activeGroupRootId`.

- [ ] **Step 1: Write the failing test — parent active, children get in-group tint**

Add inside `describe("SessionTreePanel", () => { ... })` in `tests/session-tree-panel.test.tsx`:

```tsx
it("applies in-group tint to children when parent is the active session", async () => {
  const adapter: AgentChatAdapter = {
    ...createAdapter([
      session({ id: "parent", title: "Parent session", agentId: "agent-1" }),
    ]),
    async getSessionTree() {
      return {
        total: 2,
        nodes: [
          {
            id: "parent",
            parentId: null,
            rootSessionId: "parent",
            depth: 0,
            agentId: "agent-1",
            channel: "web",
            status: "completed",
            title: "Parent session",
            model: "surogate",
            messageCount: 0,
            toolCallCount: 0,
            createdAt: "2026-01-01T00:00:00Z",
            updatedAt: "2026-01-01T00:00:00Z",
          },
          {
            id: "child-1",
            parentId: "parent",
            rootSessionId: "parent",
            depth: 1,
            agentId: "agent-1",
            channel: "delegation",
            status: "completed",
            title: "Child one",
            model: "surogate",
            messageCount: 0,
            toolCallCount: 0,
            createdAt: "2026-01-01T00:01:00Z",
            updatedAt: "2026-01-01T00:01:00Z",
          },
        ],
      };
    },
  };
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);

  await act(async () => {
    root?.render(
      <SessionTreePanel
        adapter={adapter}
        agentId="agent-1"
        loadList
        sessionId="parent"
        activeSessionId="parent"
        title="Sessions"
      />,
    );
    await Promise.resolve();
  });

  const rows = Array.from(container.querySelectorAll<HTMLElement>('[role="button"]'));
  const parentRow = rows.find((r) => r.textContent?.includes("Parent session"));
  const childRow = rows.find((r) => r.textContent?.includes("Child one"));

  // Parent is the active node — full gray
  expect(parentRow?.className).toContain("bg-line");
  expect(parentRow?.className).not.toContain("bg-line/40");
  // Child is in the active group but not active — lighter tint
  expect(childRow?.className).toContain("bg-line/40");
  expect(childRow?.className).not.toContain("bg-transparent");
});
```

- [ ] **Step 2: Write the failing test — child active, parent and siblings get in-group tint**

```tsx
it("applies in-group tint to parent and siblings when a child is the active session", async () => {
  const adapter: AgentChatAdapter = {
    ...createAdapter([
      session({ id: "parent", title: "Parent session", agentId: "agent-1" }),
    ]),
    async getSessionTree() {
      return {
        total: 3,
        nodes: [
          {
            id: "parent",
            parentId: null,
            rootSessionId: "parent",
            depth: 0,
            agentId: "agent-1",
            channel: "web",
            status: "completed",
            title: "Parent session",
            model: "surogate",
            messageCount: 0,
            toolCallCount: 0,
            createdAt: "2026-01-01T00:00:00Z",
            updatedAt: "2026-01-01T00:00:00Z",
          },
          {
            id: "child-1",
            parentId: "parent",
            rootSessionId: "parent",
            depth: 1,
            agentId: "agent-1",
            channel: "delegation",
            status: "completed",
            title: "Child one",
            model: "surogate",
            messageCount: 0,
            toolCallCount: 0,
            createdAt: "2026-01-01T00:01:00Z",
            updatedAt: "2026-01-01T00:01:00Z",
          },
          {
            id: "child-2",
            parentId: "parent",
            rootSessionId: "parent",
            depth: 1,
            agentId: "agent-1",
            channel: "delegation",
            status: "completed",
            title: "Child two",
            model: "surogate",
            messageCount: 0,
            toolCallCount: 0,
            createdAt: "2026-01-01T00:02:00Z",
            updatedAt: "2026-01-01T00:02:00Z",
          },
        ],
      };
    },
  };
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);

  await act(async () => {
    root?.render(
      <SessionTreePanel
        adapter={adapter}
        agentId="agent-1"
        loadList
        sessionId="parent"
        activeSessionId="child-1"
        title="Sessions"
      />,
    );
    await Promise.resolve();
  });

  const rows = Array.from(container.querySelectorAll<HTMLElement>('[role="button"]'));
  const parentRow = rows.find((r) => r.textContent?.includes("Parent session"));
  const child1Row = rows.find((r) => r.textContent?.includes("Child one"));
  const child2Row = rows.find((r) => r.textContent?.includes("Child two"));

  // child-1 is active
  expect(child1Row?.className).toContain("bg-line");
  expect(child1Row?.className).not.toContain("bg-line/40");
  // Parent and sibling are in-group
  expect(parentRow?.className).toContain("bg-line/40");
  expect(child2Row?.className).toContain("bg-line/40");
});
```

- [ ] **Step 3: Write the failing test — unrelated sessions stay transparent**

```tsx
it("leaves unrelated top-level sessions transparent when there is an active group", async () => {
  const adapter: AgentChatAdapter = {
    ...createAdapter([
      session({ id: "unrelated", title: "Other session", agentId: "agent-1" }),
    ]),
    async getSessionTree() {
      return {
        total: 3,
        nodes: [
          {
            id: "parent",
            parentId: null,
            rootSessionId: "parent",
            depth: 0,
            agentId: "agent-1",
            channel: "web",
            status: "completed",
            title: "Parent session",
            model: "surogate",
            messageCount: 0,
            toolCallCount: 0,
            createdAt: "2026-01-01T00:00:00Z",
            updatedAt: "2026-01-01T00:00:00Z",
          },
          {
            id: "child-1",
            parentId: "parent",
            rootSessionId: "parent",
            depth: 1,
            agentId: "agent-1",
            channel: "delegation",
            status: "completed",
            title: "Child one",
            model: "surogate",
            messageCount: 0,
            toolCallCount: 0,
            createdAt: "2026-01-01T00:01:00Z",
            updatedAt: "2026-01-01T00:01:00Z",
          },
          {
            id: "unrelated",
            parentId: null,
            rootSessionId: "unrelated",
            depth: 0,
            agentId: "agent-1",
            channel: "web",
            status: "completed",
            title: "Other session",
            model: "surogate",
            messageCount: 0,
            toolCallCount: 0,
            createdAt: "2026-01-01T00:03:00Z",
            updatedAt: "2026-01-01T00:03:00Z",
          },
        ],
      };
    },
  };
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);

  await act(async () => {
    root?.render(
      <SessionTreePanel
        adapter={adapter}
        agentId="agent-1"
        loadList
        sessionId="parent"
        activeSessionId="parent"
        title="Sessions"
      />,
    );
    await Promise.resolve();
  });

  const rows = Array.from(container.querySelectorAll<HTMLElement>('[role="button"]'));
  const unrelatedRow = rows.find((r) => r.textContent?.includes("Other session"));

  expect(unrelatedRow?.className).toContain("bg-transparent");
  expect(unrelatedRow?.className).not.toContain("bg-line");
});
```

- [ ] **Step 4: Run tests to confirm they fail**

```bash
cd /home/monica/invergent/surogates/sdk/agent-chat-react
PATH="$HOME/.local/node/bin:$PATH" npx vitest run tests/session-tree-panel.test.tsx 2>&1 | grep -E "FAIL|✓|×" | tail -20
```

Expected: the three new tests FAIL, all pre-existing tests PASS.

- [ ] **Step 5: Add `activeGroupRootId` computation to `SessionTreePanel`**

In `src/components/sessions/session-tree-panel.tsx`, add this `useMemo` right after the `roots` memo (the line `const roots = useMemo(() => buildTree(nodes), [nodes]);`):

```ts
const activeGroupRootId = useMemo<string | null>(() => {
  if (!activeSessionId) return null;
  const parentOf = new Map<string, string | null>();
  for (const n of nodes) parentOf.set(n.id, n.parentId ?? null);
  let current: string | null = activeSessionId;
  while (current !== null) {
    const parent = parentOf.get(current);
    if (parent == null) break;
    current = parent;
  }
  return current;
}, [activeSessionId, nodes]);
```

- [ ] **Step 6: Add `activeGroupRootId` prop to `TreeNodeRow`**

Replace the `TreeNodeRow` function signature (the `{` destructure + type annotation block starting around line 194) with:

```ts
function TreeNodeRow({
  entry,
  depth,
  activeSessionId,
  activeGroupRootId,
  canStop,
  canDelete,
  onSelect,
  onStop,
  onDelete,
}: {
  entry: TreeEntry;
  depth: number;
  activeSessionId: string;
  activeGroupRootId: string | null;
  canStop: boolean;
  canDelete: boolean;
  onSelect: (sessionId: string) => void;
  onStop: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
}) {
```

Then add `isInActiveGroup` right after the existing `const isChildSession = entry.parentId != null;` line:

```ts
const isInActiveGroup =
  activeGroupRootId !== null &&
  (entry.id === activeGroupRootId || entry.parentId === activeGroupRootId);
```

- [ ] **Step 7: Apply the in-group background class**

Replace the `className` prop on the outer `div` (the one with `role="button"`, around line 239):

```tsx
className={cn(
  "group flex items-center gap-2 w-full py-2 pr-2 text-left cursor-pointer transition-colors border-l-2",
  "min-h-11 md:min-h-0",
  isActive
    ? "bg-line text-foreground border-l-primary"
    : isInActiveGroup
      ? "bg-line/40 text-foreground/80 hover:bg-input hover:text-foreground border-l-transparent"
      : "bg-transparent text-foreground/80 hover:bg-input hover:text-foreground border-l-transparent",
)}
```

- [ ] **Step 8: Thread `activeGroupRootId` through both render call sites**

**Render site 1** — in `SessionTreePanel`, the `topLevel.map(...)` block:

```tsx
{topLevel.map((entry) => (
  <TreeNodeRow
    key={entry.id}
    entry={entry}
    depth={0}
    activeSessionId={activeSessionId ?? ""}
    activeGroupRootId={activeGroupRootId}
    canStop={Boolean(adapter.stopSession)}
    canDelete={Boolean(adapter.deleteSession)}
    onSelect={handleSelect}
    onStop={handleStop}
    onDelete={handleDelete}
  />
))}
```

**Render site 2** — inside `TreeNodeRow`, the recursive `entry.children.map(...)`:

```tsx
{hasChildren && expanded &&
  entry.children.map((child) => (
    <TreeNodeRow
      key={child.id}
      entry={child}
      depth={depth + 1}
      activeSessionId={activeSessionId}
      activeGroupRootId={activeGroupRootId}
      canStop={canStop}
      canDelete={canDelete}
      onSelect={onSelect}
      onStop={onStop}
      onDelete={onDelete}
    />
  ))}
```

- [ ] **Step 9: Run the full test suite**

```bash
cd /home/monica/invergent/surogates/sdk/agent-chat-react
PATH="$HOME/.local/node/bin:$PATH" npx vitest run tests/session-tree-panel.test.tsx 2>&1 | tail -20
```

Expected: all tests PASS.

- [ ] **Step 10: Commit**

```bash
cd /home/monica/invergent/surogates
git add sdk/agent-chat-react/src/components/sessions/session-tree-panel.tsx \
        sdk/agent-chat-react/tests/session-tree-panel.test.tsx
git commit -m "feat(sdk): in-group background tint for sub-agent sessions"
```

---

### Task 2: Auto-expand/collapse driven by group membership

Auto-expands a parent row when its group becomes active; auto-collapses when the active session moves to a different group or there is no active session.

**Files:**
- Modify: `sdk/agent-chat-react/src/components/sessions/session-tree-panel.tsx`
- Test: `sdk/agent-chat-react/tests/session-tree-panel.test.tsx`

**Interfaces:**
- Consumes: `activeGroupRootId: string | null` and `isInActiveGroup: boolean` introduced in Task 1.

- [ ] **Step 1: Write the failing test — unrelated parent collapses when active session moves to another group**

```tsx
it("collapses an unrelated parent when the active session moves to a different group", async () => {
  const treeNodes = [
    {
      id: "group-a",
      parentId: null as string | null,
      rootSessionId: "group-a",
      depth: 0,
      agentId: "agent-1",
      channel: "web",
      status: "completed",
      title: "Group A parent",
      model: "surogate",
      messageCount: 0,
      toolCallCount: 0,
      createdAt: "2026-01-01T00:00:00Z",
      updatedAt: "2026-01-01T00:00:00Z",
    },
    {
      id: "group-a-child",
      parentId: "group-a",
      rootSessionId: "group-a",
      depth: 1,
      agentId: "agent-1",
      channel: "delegation",
      status: "completed",
      title: "Group A child",
      model: "surogate",
      messageCount: 0,
      toolCallCount: 0,
      createdAt: "2026-01-01T00:01:00Z",
      updatedAt: "2026-01-01T00:01:00Z",
    },
    {
      id: "group-b",
      parentId: null as string | null,
      rootSessionId: "group-b",
      depth: 0,
      agentId: "agent-1",
      channel: "web",
      status: "completed",
      title: "Group B parent",
      model: "surogate",
      messageCount: 0,
      toolCallCount: 0,
      createdAt: "2026-01-01T00:02:00Z",
      updatedAt: "2026-01-01T00:02:00Z",
    },
  ];
  const adapter: AgentChatAdapter = {
    ...createAdapter([]),
    async getSessionTree() {
      return { total: treeNodes.length, nodes: treeNodes };
    },
  };
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);

  // Active = group-a: child is visible
  await act(async () => {
    root?.render(
      <SessionTreePanel
        adapter={adapter}
        agentId="agent-1"
        sessionId="group-a"
        activeSessionId="group-a"
        title="Sessions"
      />,
    );
    await Promise.resolve();
  });

  expect(container.textContent).toContain("Group A child");

  // Switch to group-b: group-a should collapse
  await act(async () => {
    root?.render(
      <SessionTreePanel
        adapter={adapter}
        agentId="agent-1"
        sessionId="group-b"
        activeSessionId="group-b"
        title="Sessions"
      />,
    );
    await Promise.resolve();
  });

  expect(container.textContent).not.toContain("Group A child");
  expect(container.textContent).toContain("Group B parent");
});
```

- [ ] **Step 2: Write the failing test — all parents collapse when activeSessionId is undefined**

```tsx
it("collapses all parents when there is no active session", async () => {
  const adapter: AgentChatAdapter = {
    ...createAdapter([]),
    async getSessionTree() {
      return {
        total: 2,
        nodes: [
          {
            id: "parent",
            parentId: null,
            rootSessionId: "parent",
            depth: 0,
            agentId: "agent-1",
            channel: "web",
            status: "completed",
            title: "Parent session",
            model: "surogate",
            messageCount: 0,
            toolCallCount: 0,
            createdAt: "2026-01-01T00:00:00Z",
            updatedAt: "2026-01-01T00:00:00Z",
          },
          {
            id: "child-1",
            parentId: "parent",
            rootSessionId: "parent",
            depth: 1,
            agentId: "agent-1",
            channel: "delegation",
            status: "completed",
            title: "Child session",
            model: "surogate",
            messageCount: 0,
            toolCallCount: 0,
            createdAt: "2026-01-01T00:01:00Z",
            updatedAt: "2026-01-01T00:01:00Z",
          },
        ],
      };
    },
  };
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);

  // Active = parent: child visible
  await act(async () => {
    root?.render(
      <SessionTreePanel
        adapter={adapter}
        agentId="agent-1"
        sessionId="parent"
        activeSessionId="parent"
        title="Sessions"
      />,
    );
    await Promise.resolve();
  });

  expect(container.textContent).toContain("Child session");

  // No active session: parent collapses
  await act(async () => {
    root?.render(
      <SessionTreePanel
        adapter={adapter}
        agentId="agent-1"
        activeSessionId={undefined}
        title="Sessions"
      />,
    );
    await Promise.resolve();
  });

  expect(container.textContent).not.toContain("Child session");
});
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
cd /home/monica/invergent/surogates/sdk/agent-chat-react
PATH="$HOME/.local/node/bin:$PATH" npx vitest run tests/session-tree-panel.test.tsx 2>&1 | grep -E "FAIL|✓|×" | tail -20
```

Expected: both new tests FAIL (parent is always expanded right now).

- [ ] **Step 4: Add `useEffect` to drive `expanded` from `isInActiveGroup`**

In `TreeNodeRow`, after all the const declarations (after `const subtitle = ...`), add:

```ts
useEffect(() => {
  if (hasChildren) setExpanded(isInActiveGroup);
}, [isInActiveGroup, hasChildren]);
```

The `useEffect` dependency array intentionally omits `setExpanded` (it's stable from `useState`) and omits `expanded` (we only want to react to group membership changes, not create a loop). The user's manual chevron click still calls `setExpanded` directly and wins until the next group-change fires the effect.

- [ ] **Step 5: Run the full test suite**

```bash
cd /home/monica/invergent/surogates/sdk/agent-chat-react
PATH="$HOME/.local/node/bin:$PATH" npx vitest run tests/session-tree-panel.test.tsx 2>&1 | tail -20
```

Expected: all tests PASS, including all pre-existing tests.

- [ ] **Step 6: Commit**

```bash
cd /home/monica/invergent/surogates
git add sdk/agent-chat-react/src/components/sessions/session-tree-panel.tsx \
        sdk/agent-chat-react/tests/session-tree-panel.test.tsx
git commit -m "feat(sdk): auto-collapse unrelated session groups in sidebar"
```

---

### Task 3: surogate-ops dep bump

After the SDK changes above are merged and CI publishes a new version, bump the dep in surogate-ops.

**Note:** This task requires CI to have published the new SDK version first. Check the tag created by CI in the `surogates` repo to get the exact version number.

**Files:**
- Modify: `surogate-ops/frontend/package.json`
- Modify: `surogate-ops/frontend/package-lock.json` (auto-updated by `npm install`)

- [ ] **Step 1: Create a branch in surogate-ops**

```bash
cd /home/monica/invergent/surogate-ops
git fetch && git checkout master && git pull
git checkout -b feat/sidebar-subagent-visual-context
```

- [ ] **Step 2: Confirm the published SDK version**

```bash
cd /home/monica/invergent/surogates && git tag --sort=-version:refname | grep agent-chat | head -5
```

Use the version number from the latest `agent-chat-react` tag (e.g. if the tag is `agent-chat-react@2.8.1`, the version is `2.8.1`).

- [ ] **Step 3: Bump the dep and install**

In `surogate-ops/frontend/package.json`, update the `@invergent/agent-chat-react` entry to the new version:

```json
"@invergent/agent-chat-react": "^<version-from-step-2>",
```

Then:

```bash
cd /home/monica/invergent/surogate-ops/frontend
PATH="$HOME/.local/node/bin:$PATH" npm install
```

- [ ] **Step 4: Run typecheck**

```bash
cd /home/monica/invergent/surogate-ops/frontend
PATH="$HOME/.local/node/bin:$PATH" npm run typecheck
```

Expected: exits 0. The two pre-existing failures (`work-agent-chat-page`, `billing-tab`) are on `master` already and unrelated.

- [ ] **Step 5: Commit**

```bash
cd /home/monica/invergent/surogate-ops
git add frontend/package.json frontend/package-lock.json
git commit -m "chore(deps): bump @invergent/agent-chat-react for sidebar group context"
```
