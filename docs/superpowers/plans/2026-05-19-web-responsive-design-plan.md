# Web Responsive Design Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/work/surogates/web` and the SDK panels it embeds fully responsive from ~360px phones to wide desktop, in a single sweep covering all routes and the chat workspace pane.

**Architecture:** Add a Tailwind-first `<AppShell>` layout primitive that owns the responsive shell (persistent sidebar `≥ md`, off-canvas Sheet drawer `< md`). Wrap every non-bare route in `<AppShell>`. Apply per-route Tailwind reflow classes. Make SDK panels responsive with className sweeps plus one internal layout change in `<AgentChat>` to expose a mobile Chat/Workspace tab toggle. Use `h-dvh` + a single `useVisualViewport` hook for keyboard-aware composer.

**Tech Stack:** React 19, TypeScript, TanStack Router, Tailwind CSS 4, `radix-ui` Dialog (for Sheet), Zustand.

**Spec:** [docs/superpowers/specs/2026-05-19-web-responsive-design.md](../specs/2026-05-19-web-responsive-design.md)

---

## Progress

- [x] **Task 1** Branch + baseline verification
- [x] **Task 2** Sheet primitive (web)
- [x] **Task 3** useVisualViewport hook (web)
- [x] **Task 4** AppShell component (web)
- [x] **Task 5** Refactor SessionSidebar to parent-driven
- [x] **Task 6** Update `__root.tsx` to `h-dvh` + mount viewport hook
- [x] **Task 7** Wrap `/chat` route in AppShell
- [x] **Task 8** Wrap `/inbox` in AppShell with URL-driven list/detail
- [x] **Task 9** Wrap `/missions/$missionId` route in AppShell
- [x] **Task 10** Wrap `/agents` route in AppShell + reflow
- [x] **Task 11** Wrap `/skills` route in AppShell + reflow
- [x] **Task 12** Wrap `/settings` route in AppShell + tabs reflow
- [x] **Task 13** Fix bare routes (`/login`, `/link`) for phone widths
- [x] **Task 14** SDK — sidebar panels touch targets and overflow
- [x] **Task 15** SDK — `MissionDashboard` grid + tabstrip
- [x] **Task 16** SDK — `BrowserPane` and `WorkspacePanel`
- [x] **Task 17** SDK — `AgentChat` mobile Chat/Workspace toggle
- [ ] **Task 18** SDK — UI primitive touch-target sweep — _in progress_
- [ ] **Task 19** Composer keyboard awareness wiring
- [ ] **Task 20** Final verification — typecheck, biome, build, dev sanity

---

## File Structure

### New files (web)

| Path | Responsibility |
| --- | --- |
| `web/src/components/ui/sheet.tsx` | Sheet primitive on `radix-ui` Dialog: slide-in from left, scroll lock, focus trap, animated. |
| `web/src/components/app-shell.tsx` | Responsive shell: persistent aside on `md+`, Sheet on `< md`, mobile header with hamburger. |
| `web/src/hooks/use-visual-viewport.ts` | Writes effective viewport height to `--viewport-h` on `<html>` so chat composer rides the keyboard. |

### Modified files (web)

| Path | Change |
| --- | --- |
| `web/src/app/routes/__root.tsx` | `h-screen` → `h-dvh`; mount `useVisualViewport` once for non-bare routes. |
| `web/src/components/navbar.tsx` | Drop internal `collapsed` state; widths driven by parent (`w-14 lg:w-80`); `data-mode` attribute differentiates aside vs sheet rendering. |
| `web/src/features/chat/chat-page.tsx` | Replace inline shell with `<AppShell>`. |
| `web/src/features/inbox/inbox-page.tsx` | Replace inline shell with `<AppShell>`; URL-driven list/detail swap on `< md`. |
| `web/src/features/missions/mission-page.tsx` | Replace inline shell with `<AppShell>`. |
| `web/src/features/agents/agents-page.tsx` | Replace inline shell with `<AppShell>`; filter bar / table / row-action reflow. |
| `web/src/features/skills/skills-page.tsx` | Same pattern as agents. |
| `web/src/features/settings/settings-page.tsx` | Replace inline shell with `<AppShell>`; tabs orientation flip. |
| `web/src/features/auth/login-page.tsx` (or equivalent) | `max-w-md w-full px-4`; `h-dvh`. |
| `web/src/app/routes/link.tsx` | Same min-width fix. |

### Modified files (SDK)

| Path | Change |
| --- | --- |
| `sdk/agent-chat-react/src/agent-chat.tsx` | Add internal `mobileView` state + segmented toggle; replace inline `style={{ width: 440 }}` with class + CSS variable; `data-mobile-view` pane swap. |
| `sdk/agent-chat-react/src/components/missions/missions-panel.tsx` | Touch targets, `min-w-0`, truncation audit. |
| `sdk/agent-chat-react/src/components/missions/mission-dashboard.tsx` | Grid `grid-cols-1 lg:grid-cols-[1fr_320px]`; tabstrip `overflow-x-auto`. |
| `sdk/agent-chat-react/src/components/scheduled/scheduled-work-panel.tsx` | Touch targets, `min-w-0`. |
| `sdk/agent-chat-react/src/components/sessions/session-tree-panel.tsx` | Touch targets, `min-w-0`. |
| `sdk/agent-chat-react/src/components/browser/browser-pane.tsx` | Header stacking, toolbar wrap, `min-w-0`. |
| `sdk/agent-chat-react/src/components/workspace/workspace-panel.tsx` | Header stacking, `min-w-0`, `aspect-video md:aspect-auto md:h-full` on preview. |
| `sdk/agent-chat-react/src/components/ui/button.tsx`, `input.tsx`, `input-group.tsx`, `dialog.tsx`, `item.tsx` | Min-height ≥ 44px on `< md`. |

---

## Conventions

- All work happens on a feature branch (e.g. `web-responsive`). Create it before Task 1.
- Commit after every task. Commit messages: `feat(web): …`, `feat(sdk): …`, `refactor(web): …`, `chore: …`. Use Conventional Commits style.
- **No Co-Authored-By trailer.** (Project convention.)
- The web app's auto SDK rebuild: `@invergent/agent-chat-react` resolves via `file:../sdk/agent-chat-react`. SDK source changes are picked up by Vite HMR; no separate build step is needed in dev. For typecheck verification, run typecheck in both packages.

---

## Task 1: Branch + baseline verification

**Files:**
- None changed in this task; this is the safety-net step.

- [ ] **Step 1: Create the branch**

```bash
cd /work/surogates
git checkout -b web-responsive
```

- [ ] **Step 2: Verify the baseline web build is green**

Run, from `/work/surogates/web`:
```bash
npm run typecheck
npm run biome:check
npm run build
```
Expected: all three exit 0. If any fail, STOP and investigate before adding changes — we need a clean baseline to attribute future failures correctly.

- [ ] **Step 3: Verify the SDK package's typecheck is green**

Run, from `/work/surogates/sdk/agent-chat-react`:
```bash
npm run typecheck 2>&1 | tail -20
```
Expected: exit 0. If the SDK has no `typecheck` script, run `npx tsc --noEmit` instead. Same rule: STOP if not green.

- [ ] **Step 4: Commit the branch start point**

Nothing to commit yet — just record state.
```bash
git log --oneline -1
```

---

## Task 2: `Sheet` primitive (web)

**Files:**
- Create: `web/src/components/ui/sheet.tsx`

This wraps `radix-ui` Dialog (already used in `web/src/components/ui/dialog.tsx`) into a side-anchored Sheet. Side `left` is the only side we use; the implementation keeps `side` as a prop so future call sites can use `right` without re-implementation.

- [ ] **Step 1: Create the Sheet primitive**

Write `web/src/components/ui/sheet.tsx`:

```tsx
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
import * as React from "react";
import { Dialog as DialogPrimitive } from "radix-ui";
import { XIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

function Sheet({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Root>) {
  return <DialogPrimitive.Root data-slot="sheet" {...props} />;
}

function SheetTrigger({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Trigger>) {
  return <DialogPrimitive.Trigger data-slot="sheet-trigger" {...props} />;
}

function SheetClose({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Close>) {
  return <DialogPrimitive.Close data-slot="sheet-close" {...props} />;
}

function SheetPortal({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Portal>) {
  return <DialogPrimitive.Portal data-slot="sheet-portal" {...props} />;
}

function SheetOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      data-slot="sheet-overlay"
      className={cn(
        "fixed inset-0 z-50 bg-black/40 supports-backdrop-filter:backdrop-blur-sm data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0",
        className,
      )}
      {...props}
    />
  );
}

type SheetContentProps = React.ComponentProps<
  typeof DialogPrimitive.Content
> & {
  side?: "left" | "right";
  showCloseButton?: boolean;
};

function SheetContent({
  className,
  children,
  side = "left",
  showCloseButton = true,
  ...props
}: SheetContentProps) {
  const sideClasses =
    side === "left"
      ? "inset-y-0 left-0 border-r data-open:slide-in-from-left data-closed:slide-out-to-left"
      : "inset-y-0 right-0 border-l data-open:slide-in-from-right data-closed:slide-out-to-right";

  return (
    <SheetPortal>
      <SheetOverlay />
      <DialogPrimitive.Content
        data-slot="sheet-content"
        className={cn(
          "fixed z-50 flex w-80 max-w-[85vw] flex-col bg-card text-sm text-foreground shadow-xl ring-1 ring-foreground/10 outline-none duration-150 data-open:animate-in data-closed:animate-out",
          sideClasses,
          className,
        )}
        {...props}
      >
        {children}
        {showCloseButton && (
          <DialogPrimitive.Close data-slot="sheet-close" asChild>
            <Button
              variant="ghost"
              className="absolute top-3 right-3"
              size="icon-sm"
              aria-label="Close"
            >
              <XIcon />
            </Button>
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </SheetPortal>
  );
}

function SheetTitle({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      data-slot="sheet-title"
      className={cn("sr-only", className)}
      {...props}
    />
  );
}

function SheetDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      data-slot="sheet-description"
      className={cn("sr-only", className)}
      {...props}
    />
  );
}

export {
  Sheet,
  SheetTrigger,
  SheetClose,
  SheetContent,
  SheetOverlay,
  SheetPortal,
  SheetTitle,
  SheetDescription,
};
```

- [ ] **Step 2: Typecheck**

From `/work/surogates/web`:
```bash
npm run typecheck
```
Expected: exit 0. (No call sites yet — purely a primitive.)

- [ ] **Step 3: Biome check**

```bash
npm run biome:check
```
Expected: exit 0. If lint fails, run `npm run biome:fix` and re-check.

- [ ] **Step 4: Commit**

```bash
git add web/src/components/ui/sheet.tsx
git commit -m "feat(web): add Sheet primitive on radix Dialog"
```

---

## Task 3: `useVisualViewport` hook (web)

**Files:**
- Create: `web/src/hooks/use-visual-viewport.ts`

- [ ] **Step 1: Create the hook**

Write `web/src/hooks/use-visual-viewport.ts`:

```ts
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
import { useEffect } from "react";

// Writes the visible viewport height (accounting for the on-screen keyboard
// on iOS Safari / mobile Chrome) to a CSS custom property `--viewport-h` on
// the <html> element. The chat composer reads it to stay pinned above the
// keyboard. Idempotent: safe to mount multiple times (last writer wins, but
// values are identical).
export function useVisualViewport() {
  useEffect(() => {
    const root = document.documentElement;
    const vv = window.visualViewport;

    function update() {
      const h = vv?.height ?? window.innerHeight;
      root.style.setProperty("--viewport-h", `${h}px`);
    }

    update();
    if (!vv) return;
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
    };
  }, []);
}
```

- [ ] **Step 2: Typecheck**

```bash
npm run typecheck
```
Expected: exit 0.

- [ ] **Step 3: Commit**

```bash
git add web/src/hooks/use-visual-viewport.ts
git commit -m "feat(web): add useVisualViewport hook"
```

---

## Task 4: `AppShell` component (web)

**Files:**
- Create: `web/src/components/app-shell.tsx`

The shell is a self-contained layout. It owns the mobile sheet's open state. The same `sidebar` React node is rendered twice — once in the persistent `<aside>` with `data-mode="aside"`, once inside the Sheet content with `data-mode="sheet"`. The sidebar component (Task 5) reads `data-mode` from its closest matching ancestor via CSS to vary its rendering.

- [ ] **Step 1: Create the AppShell**

Write `web/src/components/app-shell.tsx`:

```tsx
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
import * as React from "react";
import { useEffect, useState } from "react";
import { useRouterState } from "@tanstack/react-router";
import { MenuIcon, SunIcon, MoonIcon } from "lucide-react";
import { useTheme } from "next-themes";

import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";

type AppShellProps = {
  sidebar: React.ReactNode;
  headerSlot?: React.ReactNode;
  children: React.ReactNode;
};

export function AppShell({ sidebar, headerSlot, children }: AppShellProps) {
  const [sheetOpen, setSheetOpen] = useState(false);
  const { theme, setTheme } = useTheme();

  // Close the sheet on route change so a sidebar navigation doesn't leave
  // the drawer open over the new page.
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  useEffect(() => {
    setSheetOpen(false);
  }, [pathname]);

  return (
    <div className="flex h-full w-full overflow-hidden">
      {/* Desktop / tablet: persistent aside. `group` + `data-mode` enables
          group-data-* variants in the sidebar component. */}
      <aside
        data-mode="aside"
        className="group hidden md:flex md:w-14 md:min-w-14 lg:w-80 lg:min-w-80 bg-card border-r border-line flex-col overflow-hidden z-10"
      >
        {sidebar}
      </aside>

      {/* Phone: sheet drawer */}
      <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
        <SheetContent
          side="left"
          className="md:hidden p-0"
          showCloseButton={false}
        >
          <SheetTitle>Navigation</SheetTitle>
          <div
            data-mode="sheet"
            className="group flex flex-col h-full overflow-hidden"
          >
            {sidebar}
          </div>
        </SheetContent>
      </Sheet>

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {/* Phone-only top header */}
        <header className="md:hidden flex h-12 shrink-0 items-center gap-2 border-b border-line px-2">
          <button
            type="button"
            onClick={() => setSheetOpen(true)}
            aria-label="Open navigation"
            className="inline-flex h-10 w-10 items-center justify-center rounded-md text-subtle hover:bg-input hover:text-foreground"
          >
            <MenuIcon className="h-5 w-5" />
          </button>
          <div className="min-w-0 flex-1">{headerSlot}</div>
          <button
            type="button"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            aria-label="Toggle theme"
            className="inline-flex h-10 w-10 items-center justify-center rounded-md text-subtle hover:bg-input hover:text-foreground"
          >
            {theme === "dark" ? (
              <SunIcon className="h-4 w-4" />
            ) : (
              <MoonIcon className="h-4 w-4" />
            )}
          </button>
        </header>

        <main className="flex min-h-0 flex-1 min-w-0 flex-col overflow-hidden">
          {children}
        </main>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

```bash
npm run typecheck
```
Expected: exit 0.

- [ ] **Step 3: Commit**

```bash
git add web/src/components/app-shell.tsx
git commit -m "feat(web): add AppShell responsive layout primitive"
```

---

## Task 5: Refactor `SessionSidebar` to be parent-driven

**Files:**
- Modify: `web/src/components/navbar.tsx`

We're removing the internal `collapsed` state. Width comes from the parent (`<aside>` has `md:w-14 lg:w-80`; the Sheet sets its own width). The visual "collapsed" mode keys off `lg:` breakpoints. The Sheet variant gets `data-mode="sheet"` from AppShell's wrapping div, and we use Tailwind's `[data-mode=sheet]:` attribute selector via a parent-class trick: since attribute selectors target the element itself, we put `data-mode` on a wrapping div and use `group/mode` so descendants can react.

For simplicity, we use a different pattern: tag the sidebar root with the `group/sidebar` class and let the parent's wrapping `data-mode` be queryable via Tailwind's `group-data-[mode=sheet]:` variant.

- [ ] **Step 1: Rewrite `navbar.tsx`**

Replace the entire file at `web/src/components/navbar.tsx`:

```tsx
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useNavigate } from "@tanstack/react-router";
import {
  MissionsPanel,
  ScheduledWorkPanel,
  SessionTreePanel,
  useInboxUnreadCount,
} from "@invergent/agent-chat-react";
import {
  PlusIcon,
  MessageSquareIcon,
  InboxIcon,
  LogOutIcon,
  SunIcon,
  MoonIcon,
  SettingsIcon,
  BookOpenIcon,
  UsersIcon,
} from "lucide-react";
import { useTheme } from "next-themes";
import { useAppStore } from "@/stores/app-store";
import { logout } from "@/api/auth";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { surogatesWebChatAdapter } from "@/features/chat";

// The "expanded" classes show full content; "compact" classes only show icons.
// On phones the sidebar lives inside a Sheet whose ancestor sets
// data-mode="sheet" — we use that to force expanded rendering regardless of
// breakpoint via the group-data variant.
//
// `lg:` breakpoint splits compact (md, sheet via override) vs expanded (lg+).
// On md tablets the desktop aside is `w-14`, so compact wins by default.
const expanded = "lg:flex group-data-[mode=sheet]:flex";
const expandedInline = "lg:inline group-data-[mode=sheet]:inline";
const expandedBlock = "lg:block group-data-[mode=sheet]:block";
const hideOnCompact = "hidden lg:flex group-data-[mode=sheet]:flex";

export function SessionSidebar() {
  const navigate = useNavigate();
  const activeSessionId = useAppStore((s) => s.activeSessionId);
  const setActiveSession = useAppStore((s) => s.setActiveSession);
  const fetchSessions = useAppStore((s) => s.fetchSessions);
  const removeSession = useAppStore((s) => s.removeSession);
  const sessions = useAppStore((s) => s.sessions);
  const sessionsLoading = useAppStore((s) => s.sessionsLoading);
  const user = useAppStore((s) => s.user);
  const { theme, setTheme } = useTheme();
  const { unreadCount } = useInboxUnreadCount(surogatesWebChatAdapter);

  function handleNewSession() {
    setActiveSession(null);
    void navigate({ to: "/chat" });
  }

  function handleSelectSession(sessionId: string) {
    setActiveSession(sessionId);
    void navigate({ to: "/chat/$sessionId", params: { sessionId } });
  }

  function handleSessionDeleted(sessionId: string) {
    if (sessionId === activeSessionId) {
      void navigate({ to: "/chat" });
    }
    removeSession(sessionId);
  }

  function handleScheduleChanged() {
    void fetchSessions();
  }

  function handleMissionSelect(missionId: string) {
    void navigate({ to: "/missions/$missionId", params: { missionId } });
  }

  function handleLogout() {
    logout();
    void navigate({ to: "/login" });
  }

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      {/* Header */}
      <div
        className={cn(
          "flex items-center border-b border-line min-h-14",
          // Compact: center icon. Expanded: icon + label on left.
          "justify-center py-4",
          "lg:justify-start lg:px-4 lg:py-4 lg:gap-2.5",
          "group-data-[mode=sheet]:justify-start group-data-[mode=sheet]:px-4 group-data-[mode=sheet]:py-4 group-data-[mode=sheet]:gap-2.5",
        )}
      >
        <div className="w-7 h-7 rounded-md bg-primary flex items-center justify-center shrink-0">
          <MessageSquareIcon className="w-4 h-4 text-primary-foreground" />
        </div>
        <div className={cn("hidden", expandedBlock)}>
          <div className="font-bold text-foreground tracking-tight">
            Surogate
          </div>
          <div className="text-xs text-muted-foreground tracking-wide uppercase">
            Agent Chat
          </div>
        </div>
      </div>

      {/* Nav buttons */}
      <div
        className={cn(
          "border-b border-line",
          "p-1.5 lg:p-3 group-data-[mode=sheet]:p-3",
        )}
      >
        <Button
          variant="outline"
          onClick={handleNewSession}
          className={cn(
            "w-full gap-2 min-h-11 lg:min-h-9 group-data-[mode=sheet]:min-h-11",
            "justify-center px-0 lg:justify-start lg:px-3",
            "group-data-[mode=sheet]:justify-start group-data-[mode=sheet]:px-3",
          )}
        >
          <PlusIcon className="w-4 h-4" />
          <span className={cn("hidden", expandedInline)}>New chat</span>
        </Button>
        <Button
          variant="ghost"
          onClick={() => void navigate({ to: "/skills" })}
          className={cn(
            "w-full gap-2 mt-1 min-h-11 lg:min-h-9 group-data-[mode=sheet]:min-h-11",
            "justify-center px-0 lg:justify-start lg:px-3",
            "group-data-[mode=sheet]:justify-start group-data-[mode=sheet]:px-3",
          )}
        >
          <BookOpenIcon className="w-4 h-4" />
          <span className={cn("hidden", expandedInline)}>Skills</span>
        </Button>
        <Button
          variant="ghost"
          onClick={() => void navigate({ to: "/agents" })}
          className={cn(
            "w-full gap-2 mt-1 min-h-11 lg:min-h-9 group-data-[mode=sheet]:min-h-11",
            "justify-center px-0 lg:justify-start lg:px-3",
            "group-data-[mode=sheet]:justify-start group-data-[mode=sheet]:px-3",
          )}
        >
          <UsersIcon className="w-4 h-4" />
          <span className={cn("hidden", expandedInline)}>Sub-agents</span>
        </Button>
        <Button
          variant="ghost"
          onClick={() => void navigate({ to: "/inbox" })}
          className={cn(
            "w-full gap-2 mt-1 min-h-11 lg:min-h-9 group-data-[mode=sheet]:min-h-11 relative",
            "justify-center px-0 lg:justify-start lg:px-3",
            "group-data-[mode=sheet]:justify-start group-data-[mode=sheet]:px-3",
          )}
        >
          <InboxIcon className="w-4 h-4" />
          <span className={cn("hidden", expandedInline)}>Inbox</span>
          {unreadCount > 0 && (
            <span
              className={cn(
                "inline-flex items-center justify-center bg-primary px-1 text-[0.625rem] font-semibold text-primary-foreground",
                // Compact: corner badge. Expanded: trailing badge.
                "absolute right-1 top-0 h-4 min-w-4 px-0.5 text-[0.55rem]",
                "lg:static lg:ml-auto lg:h-5 lg:min-w-5 lg:px-1 lg:text-[0.625rem]",
                "group-data-[mode=sheet]:static group-data-[mode=sheet]:ml-auto group-data-[mode=sheet]:h-5 group-data-[mode=sheet]:min-w-5 group-data-[mode=sheet]:px-1 group-data-[mode=sheet]:text-[0.625rem]",
              )}
            >
              {unreadCount > 99 ? "99+" : unreadCount}
            </span>
          )}
        </Button>
      </div>

      {/* Sessions list */}
      <div className="min-h-0 flex-1 flex flex-col">
        <div className="min-h-0 flex-1 overflow-y-auto py-1">
          {/* Compact (md only, not sheet): icon-list */}
          <div
            className={cn(
              "block lg:hidden group-data-[mode=sheet]:hidden",
            )}
          >
            {sessions.map((session) => {
              const isActive = session.id === activeSessionId;
              return (
                <button
                  key={session.id}
                  type="button"
                  onClick={() => handleSelectSession(session.id)}
                  aria-label={session.title ?? "New session"}
                  className={cn(
                    "flex items-center justify-center w-full py-2 transition-colors border-l-2",
                    isActive
                      ? "bg-line text-foreground border-l-primary"
                      : "bg-transparent text-subtle hover:bg-input hover:text-foreground border-l-transparent",
                  )}
                >
                  <MessageSquareIcon className="w-4 h-4 shrink-0" />
                </button>
              );
            })}
          </div>

          {/* Expanded (lg+ or sheet): SessionTreePanel */}
          <div className={cn("hidden", expandedBlock)}>
            <SessionTreePanel
              adapter={surogatesWebChatAdapter}
              loadList
              sessionId={activeSessionId ?? undefined}
              activeSessionId={activeSessionId ?? undefined}
              hideHeader
              onSessionSelect={handleSelectSession}
              onSessionDelete={handleSessionDeleted}
            />
            {!sessionsLoading && sessions.length === 0 && (
              <div className="px-4 py-8 text-center text-sm text-faint">
                No sessions yet
              </div>
            )}
          </div>
        </div>

        {/* Missions + Scheduled work — expanded only */}
        <div
          className={cn(
            "hidden shrink-0 max-h-[45%] overflow-y-auto",
            expandedBlock,
          )}
        >
          <MissionsPanel
            adapter={surogatesWebChatAdapter}
            onMissionSelect={handleMissionSelect}
          />
          <ScheduledWorkPanel
            adapter={surogatesWebChatAdapter}
            onSessionSelect={handleSelectSession}
            onScheduleCancel={handleScheduleChanged}
            onScheduleRunNow={handleScheduleChanged}
          />
        </div>
      </div>

      {/* Footer */}
      <div
        className={cn(
          "border-t border-line",
          "py-2 lg:p-3 group-data-[mode=sheet]:p-3",
        )}
      >
        <div className={cn("hidden", expanded, "flex-col gap-1")}>
          {user && (
            <button
              type="button"
              onClick={() => void navigate({ to: "/settings" })}
              className="flex items-center gap-2 px-1 py-1.5 mb-1.5 w-full rounded-md hover:bg-input transition-colors cursor-pointer min-h-11 lg:min-h-9 group-data-[mode=sheet]:min-h-11"
            >
              <div className="w-6 h-6 rounded-full bg-primary/20 flex items-center justify-center font-bold text-primary text-xs shrink-0">
                {(user.display_name ?? user.email)?.[0]?.toUpperCase() ?? "?"}
              </div>
              <div className="flex-1 min-w-0 text-left">
                <div className="text-subtle font-medium text-sm truncate">
                  {user.display_name ?? user.email}
                </div>
              </div>
              <SettingsIcon className="w-3.5 h-3.5 text-faint shrink-0" />
            </button>
          )}
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={handleLogout}
              className="flex items-center gap-2 flex-1 px-2.5 py-1.5 rounded-md text-sm text-subtle hover:bg-input hover:text-foreground transition-colors min-h-11 lg:min-h-9 group-data-[mode=sheet]:min-h-11"
            >
              <LogOutIcon className="w-4 h-4" />
              Sign out
            </button>
            <button
              type="button"
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
              className="p-1.5 rounded-md text-subtle hover:bg-input hover:text-foreground transition-colors min-h-11 lg:min-h-9 min-w-11 lg:min-w-0 group-data-[mode=sheet]:min-h-11 group-data-[mode=sheet]:min-w-11"
              aria-label="Toggle theme"
            >
              {theme === "dark" ? (
                <SunIcon className="w-4 h-4" />
              ) : (
                <MoonIcon className="w-4 h-4" />
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
```

Notes:
- The `data-mode="aside"` vs `data-mode="sheet"` attribute and the `group` class both live on the wrapper provided by `<AppShell>`. Tailwind v4's `group-data-[mode=sheet]:*` variant walks up the DOM to find an ancestor with both `group` and a matching `data-mode` — both must be co-located for the variant to apply.
- The collapse `◂`/`▸` toggle button is removed. Collapse is purely viewport-driven (md compact, lg expanded, sheet always expanded).
- All clickable items get `min-h-11` on compact/sheet (touch target) and revert to `min-h-9` on `lg` desktop.

- [ ] **Step 2: Typecheck**

```bash
npm run typecheck
```
Expected: exit 0. Existing imports of `SessionSidebar` from other routes still work because the export name is unchanged.

- [ ] **Step 3: Biome check**

```bash
npm run biome:check
```
Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add web/src/components/navbar.tsx
git commit -m "refactor(web): parent-drive SessionSidebar width and density via data-mode"
```

---

## Task 6: Update `__root.tsx` to `h-dvh` + mount viewport hook

**Files:**
- Modify: `web/src/app/routes/__root.tsx`

- [ ] **Step 1: Update the root layout**

Replace the contents of `web/src/app/routes/__root.tsx` with:

```tsx
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { Outlet, createRootRoute, useRouterState } from "@tanstack/react-router";
import { Suspense } from "react";
import { AppProvider } from "../provider";
import { useVisualViewport } from "@/hooks/use-visual-viewport";

const BARE_ROUTES = ["/login", "/link"];

function RootLayout() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const isBare = BARE_ROUTES.includes(pathname);
  useVisualViewport();

  return (
    <AppProvider>
      <div
        className={
          isBare
            ? "h-dvh bg-background text-foreground"
            : "flex h-dvh overflow-hidden bg-background text-foreground"
        }
      >
        <Suspense fallback={null}>
          <Outlet />
        </Suspense>
      </div>
    </AppProvider>
  );
}

export const Route = createRootRoute({
  component: RootLayout,
});
```

- [ ] **Step 2: Typecheck**

```bash
npm run typecheck
```
Expected: exit 0.

- [ ] **Step 3: Commit**

```bash
git add web/src/app/routes/__root.tsx
git commit -m "feat(web): switch root to h-dvh and mount visual viewport hook"
```

---

## Task 7: Wrap `/chat` route in AppShell

**Files:**
- Modify: `web/src/features/chat/chat-page.tsx`

The chat page's inline shell (`<div className="flex h-screen…"><SessionSidebar /><main>…</main></div>`) is replaced with `<AppShell sidebar={<SessionSidebar />}>…</AppShell>`. The content of `<main>` becomes AppShell's `children`.

- [ ] **Step 1: Modify chat-page.tsx render**

In `web/src/features/chat/chat-page.tsx`, find the `return (` block at line 166 and replace it down to the closing `);` with:

```tsx
  return (
    <AppShell sidebar={<SessionSidebar />}>
      <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
        {needsDisclosure && (
          <div className="absolute inset-x-0 top-0 z-30 p-4 flex justify-center">
            <TransparencyBanner
              sessionId={sessionId ?? undefined}
              level={transparencyConfig?.level ?? "basic"}
              onConfirmed={handleDisclosureConfirmed}
              onDeclined={handleDisclosureDeclined}
            />
          </div>
        )}

        {sessionDeclined ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center text-muted-foreground space-y-3 px-4 max-w-5xl">
              <p className="font-medium text-red-400">Session disabled</p>
              <p className="leading-relaxed italic">
                In accordance with the EU Artificial Intelligence Act
                (Regulation 2024/1689), Articles 13 and 50, users must
                acknowledge that they are interacting with an AI system
                before it can process requests.
              </p>
              <p>
                Without your acknowledgment, this session cannot continue
                and has been deactivated.
              </p>
            </div>
          </div>
        ) : (
          <div
            className={`flex min-h-0 flex-1 flex-col overflow-hidden${sessionId ? "" : " [&>section>:last-child]:hidden"}`}
          >
            <AgentChat
              sessionId={sessionId ?? null}
              adapter={chatAdapter}
              onSessionChange={handleSessionChange}
              disabled={sessionDeclined}
              onMessagesChange={setChatMessages}
            />
          </div>
        )}
      </div>
    </AppShell>
  );
```

At the top of the file, add the import:

```tsx
import { AppShell } from "@/components/app-shell";
```

- [ ] **Step 2: Typecheck**

```bash
npm run typecheck
```
Expected: exit 0.

- [ ] **Step 3: Commit**

```bash
git add web/src/features/chat/chat-page.tsx
git commit -m "feat(web): wrap chat page in AppShell"
```

---

## Task 8: Wrap `/inbox` in AppShell with URL-driven list/detail

**Files:**
- Modify: `web/src/features/inbox/inbox-page.tsx`
- Possibly modify: TanStack Router route definition for `/inbox` to declare `validateSearch` if not present. Inspect `web/src/app/routes/inbox.tsx` first.

The `<InboxPanel>` SDK component owns the list+detail layout. We don't want to fork its internals; we instead apply Tailwind classes around it to show/hide list vs detail on phone based on a URL search param `?item=<id>`. The SDK panel already has its own routing for which item is selected; we just provide a wrapper that's responsive.

- [ ] **Step 1: Inspect the existing InboxPanel API**

```bash
grep -n "selectedItemId\|onItemSelect\|export.*InboxPanel" /work/surogates/sdk/agent-chat-react/src/components/inbox/*.tsx | head -20
```
Read the output to confirm how an item is selected externally. The most likely props are `selectedItemId` and `onItemSelect`. If they exist, the web layer drives them from the URL search param. If they don't, skip the URL-driven swap for this iteration and fall back to a CSS-only swap: list is `hidden md:flex` once detail is open (driven by the panel's internal state — out of reach), or accept that on phone both stack with an internal scroll. **Document which fallback was used in the commit message.**

- [ ] **Step 2: Modify `web/src/features/inbox/inbox-page.tsx`**

If InboxPanel exposes external selection state, replace contents with:

```tsx
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import { useEffect } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { InboxPanel } from "@invergent/agent-chat-react";
import { SessionSidebar } from "@/components/navbar";
import { AppShell } from "@/components/app-shell";
import { useAppStore } from "@/stores/app-store";
import { surogatesWebChatAdapter } from "@/features/chat";

export function InboxPage() {
  const navigate = useNavigate();
  const fetchSessions = useAppStore((state) => state.fetchSessions);
  const fetchUser = useAppStore((state) => state.fetchUser);
  const setActiveSession = useAppStore((state) => state.setActiveSession);
  const search = useSearch({ strict: false }) as { item?: string };

  useEffect(() => {
    void fetchSessions();
    void fetchUser();
  }, [fetchSessions, fetchUser]);

  function handleSessionSelect(sessionId: string) {
    setActiveSession(sessionId);
    void navigate({ to: "/chat/$sessionId", params: { sessionId } });
  }

  function handleItemSelect(itemId: string | null) {
    void navigate({
      to: "/inbox",
      search: itemId ? { item: itemId } : {},
      replace: true,
    });
  }

  const itemOpen = !!search.item;

  return (
    <AppShell sidebar={<SessionSidebar />}>
      <div
        data-item-open={itemOpen ? "true" : "false"}
        className="min-w-0 flex-1 overflow-hidden"
      >
        <InboxPanel
          adapter={surogatesWebChatAdapter}
          onSessionSelect={handleSessionSelect}
          selectedItemId={search.item ?? null}
          onItemSelect={handleItemSelect}
        />
      </div>
    </AppShell>
  );
}
```

If `selectedItemId`/`onItemSelect` props are NOT exposed by `InboxPanel`, use the minimal version:

```tsx
return (
  <AppShell sidebar={<SessionSidebar />}>
    <div className="min-w-0 flex-1 overflow-hidden">
      <InboxPanel
        adapter={surogatesWebChatAdapter}
        onSessionSelect={handleSessionSelect}
      />
    </div>
  </AppShell>
);
```

…and add a note to the commit message that mobile list/detail swap is deferred until the SDK exposes external selection state.

- [ ] **Step 3: If route file needs `validateSearch`, update it**

Check `web/src/app/routes/inbox.tsx`. If the route doesn't declare a `validateSearch` for the `item` query param, add it:

```tsx
import { createFileRoute } from "@tanstack/react-router";
// existing imports …

export const Route = createFileRoute("/inbox")({
  validateSearch: (search: Record<string, unknown>) => ({
    item: typeof search.item === "string" ? search.item : undefined,
  }),
  component: InboxPage,
});
```

Only change this file if you used the URL-driven version in Step 2.

- [ ] **Step 4: Typecheck**

```bash
npm run typecheck
```
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/inbox/inbox-page.tsx web/src/app/routes/inbox.tsx
git commit -m "feat(web): wrap inbox in AppShell + URL-driven mobile detail (if supported)"
```

---

## Task 9: Wrap `/missions/$missionId` route in AppShell

**Files:**
- Modify: `web/src/features/missions/mission-page.tsx`

- [ ] **Step 1: Modify the file**

Replace contents of `web/src/features/missions/mission-page.tsx` with:

```tsx
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Mission page — host shell around the SDK's <MissionDashboard>.
import { useNavigate, useParams } from "@tanstack/react-router";
import { MissionDashboard } from "@invergent/agent-chat-react";

import { SessionSidebar } from "@/components/navbar";
import { AppShell } from "@/components/app-shell";
import { surogatesWebChatAdapter } from "@/features/chat/surogates-web-chat-adapter";

export function MissionPage() {
  const navigate = useNavigate();
  const { missionId } = useParams({ strict: false }) as {
    missionId: string | undefined;
  };

  return (
    <AppShell sidebar={<SessionSidebar />}>
      <div className="flex-1 overflow-y-auto">
        {missionId ? (
          <MissionDashboard
            adapter={surogatesWebChatAdapter}
            missionId={missionId}
            onNavigateBack={() => {
              void navigate({ to: "/chat" });
            }}
            onOpenTranscript={(workerSessionId) => {
              void navigate({
                to: "/chat/$sessionId",
                params: { sessionId: workerSessionId },
              });
            }}
          />
        ) : (
          <div className="p-6 text-sm">Missing mission id in URL.</div>
        )}
      </div>
    </AppShell>
  );
}
```

- [ ] **Step 2: Typecheck**

```bash
npm run typecheck
```
Expected: exit 0.

- [ ] **Step 3: Commit**

```bash
git add web/src/features/missions/mission-page.tsx
git commit -m "feat(web): wrap mission page in AppShell"
```

---

## Task 10: Wrap `/agents` route in AppShell + reflow

**Files:**
- Modify: `web/src/features/agents/agents-page.tsx`

This route has its own fixed `w-80 min-w-80` left list panel (separate from the sidebar). On `< md`, that inner list also needs to reflow.

- [ ] **Step 1: Find the existing render block**

Read lines 216–end of `web/src/features/agents/agents-page.tsx` to confirm current structure. The shell currently is `<><SessionSidebar /><main>…</main></>`.

- [ ] **Step 2: Make the changes**

In the `return (…)` block:

1. Wrap in `<AppShell sidebar={<SessionSidebar />}>` and remove the `<SessionSidebar />` line and the wrapping fragment.
2. Replace `<main className="flex-1 flex overflow-hidden">` with `<div className="flex-1 flex flex-col md:flex-row overflow-hidden">`.
3. On the inner list `<section>`: change `w-80 min-w-80` to `w-full md:w-72 md:min-w-72 lg:w-80 lg:min-w-80`.
4. On the inner detail panel (the second child of the main row, which is after the list `<section>`): apply `hidden md:flex md:flex-1 md:flex-col` (or whatever wrapper class it currently has, plus the `hidden md:flex` part), so on `< md` only one of list/detail shows.
5. Add a swap based on selection: when `selectedName` is set on `< md`, the list hides and the detail shows. Tailwind-only swap requires reading `selectedName` and adding/removing classes; use:
   - List `<section>`: add `cn("...", selectedName && "hidden md:flex")`.
   - Detail wrapper: add `cn("...", !selectedName && "hidden md:flex")`.
6. Inside the list header (line ~221), the filter bar `<header>` is OK as-is but ensure `InputGroup` doesn't overflow on phone — it already uses `w-full` by default.
7. Add `import { AppShell } from "@/components/app-shell";` to the top.

Example for the list section open tag, replace:

```tsx
<section className="flex flex-col w-80 min-w-80 border-r border-line">
```

with:

```tsx
<section
  className={cn(
    "flex flex-col border-r border-line",
    "w-full md:w-72 md:min-w-72 lg:w-80 lg:min-w-80",
    selectedName && "hidden md:flex",
  )}
>
```

- [ ] **Step 3: Find and update the detail wrapper**

Read further in the file to locate the detail-side wrapper. Apply:

```tsx
className={cn(
  "<existing classes>",
  !selectedName && "hidden md:flex",
)}
```

If the detail-side wrapper doesn't currently have one (e.g., the detail is rendered conditionally only when `selectedName` is set), it's already correct on phone: list shows when nothing selected, detail shows when something selected. In that case, only the list section needs the `selectedName && "hidden md:flex"` rule.

- [ ] **Step 4: Add a back button in the detail header on phone**

In the detail header, prepend a phone-only back button that clears selection:

```tsx
<button
  type="button"
  onClick={() => setSelectedName(null)}
  className="md:hidden inline-flex h-10 w-10 items-center justify-center rounded-md text-subtle hover:bg-input"
  aria-label="Back to list"
>
  <ChevronLeftIcon className="h-5 w-5" />
</button>
```

Add `ChevronLeftIcon` to the existing `lucide-react` import at the top of the file.

- [ ] **Step 5: Typecheck**

```bash
npm run typecheck
```
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add web/src/features/agents/agents-page.tsx
git commit -m "feat(web): wrap agents page in AppShell with mobile list/detail swap"
```

---

## Task 11: Wrap `/skills` route in AppShell + reflow

**Files:**
- Modify: `web/src/features/skills/skills-page.tsx`

The skills page mirrors agents structurally. Apply the same transformations as Task 10:

- [ ] **Step 1: Wrap in `<AppShell>`** and remove the inline fragment + `<SessionSidebar />`.

- [ ] **Step 2: Replace main flex direction**

```tsx
<div className="flex-1 flex flex-col md:flex-row overflow-hidden">
```

- [ ] **Step 3: List section width + visibility**

```tsx
<section
  className={cn(
    "flex flex-col border-r border-line",
    "w-full md:w-72 md:min-w-72 lg:w-80 lg:min-w-80",
    selectedName && "hidden md:flex",
  )}
>
```

- [ ] **Step 4: Detail wrapper visibility**

If detail is rendered conditionally on `selectedName`, no change needed. Otherwise add `!selectedName && "hidden md:flex"`.

- [ ] **Step 5: Phone back button** (same snippet as Task 10 Step 4)

- [ ] **Step 6: Add imports**

```tsx
import { AppShell } from "@/components/app-shell";
import { ChevronLeftIcon } from "lucide-react"; // add to existing import
```

- [ ] **Step 7: Typecheck**

```bash
npm run typecheck
```
Expected: exit 0.

- [ ] **Step 8: Commit**

```bash
git add web/src/features/skills/skills-page.tsx
git commit -m "feat(web): wrap skills page in AppShell with mobile list/detail swap"
```

---

## Task 12: Wrap `/settings` route in AppShell + tabs reflow

**Files:**
- Modify: `web/src/features/settings/settings-page.tsx`

Settings currently uses `TabsList variant="line"` from the local Tabs component. Most likely this is already a horizontal tab list, in which case the only change is wrapping in `<AppShell>`. If the tabs are vertical, we need to flip orientation.

- [ ] **Step 1: Inspect Tabs component**

```bash
grep -n "variant.*line\|orientation" /work/surogates/web/src/components/ui/tabs.tsx | head
```
If `variant="line"` already renders horizontally, no orientation flip is needed.

- [ ] **Step 2: Wrap render in AppShell**

In `web/src/features/settings/settings-page.tsx`, replace the outer fragment + `<SessionSidebar />` + `<main>` block with:

```tsx
return (
  <AppShell sidebar={<SessionSidebar />}>
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-2xl mx-auto px-4 sm:px-6 py-6 sm:py-10">
        {/* existing content */}
      </div>
    </div>
  </AppShell>
);
```

Add `import { AppShell } from "@/components/app-shell";` to the top.

- [ ] **Step 3: Make `TabsList` scrollable on phone**

Locate `<TabsList variant="line" className="mb-6">` and change to:

```tsx
<TabsList variant="line" className="mb-6 overflow-x-auto">
```

This makes the tab strip horizontally scrollable on phone if more tabs are added later. The current two tabs ("Profile", "Connected Channels") fit in 360px without scrolling but the rule is safe.

- [ ] **Step 4: Typecheck**

```bash
npm run typecheck
```
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/settings/settings-page.tsx
git commit -m "feat(web): wrap settings page in AppShell"
```

---

## Task 13: Fix bare routes (`/login`, `/link`) for phone widths

**Files:**
- Modify: `web/src/features/auth/login-page.tsx` (path may differ — check)
- Modify: `web/src/app/routes/link.tsx`

- [ ] **Step 1: Find the login page**

```bash
grep -rln "from \"@/features/auth" /work/surogates/web/src/ | head
grep -rln "function.*Login\|LoginPage" /work/surogates/web/src/features/auth/ | head
```

- [ ] **Step 2: Open the login page file and locate the centering wrapper**

Look for the outer div containing the card. It likely has classes like `flex items-center justify-center h-full`. Verify that:
- The outer wrapper uses `min-h-full` not a fixed pixel height
- The card has `w-full max-w-md` and the page has `px-4`

If any of these are missing, add them. Specifically: card-wrapper className → `w-full max-w-md px-4`.

If the outer wrapper has `h-screen`, change to `min-h-dvh`.

- [ ] **Step 3: Same for `link.tsx`**

Open `web/src/app/routes/link.tsx`. Apply the same audit.

- [ ] **Step 4: Typecheck**

```bash
npm run typecheck
```
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/auth web/src/app/routes/link.tsx
git commit -m "feat(web): make login and link pages fit phone widths"
```

---

## Task 14: SDK — sidebar panels touch targets and overflow

**Files:**
- Modify: `sdk/agent-chat-react/src/components/missions/missions-panel.tsx`
- Modify: `sdk/agent-chat-react/src/components/scheduled/scheduled-work-panel.tsx`
- Modify: `sdk/agent-chat-react/src/components/sessions/session-tree-panel.tsx`

For each of these three files, the audit is the same:

1. Find clickable rows (buttons, `<li>` with `onClick`, etc.). Add `min-h-11 md:min-h-0` to ensure ≥ 44px touch targets on phone.
2. Find any parent of a `truncate` text element that doesn't have `min-w-0`. Add `min-w-0`.
3. Find any fixed-width `w-{n}` on inner elements that isn't intentional iconography. Replace with `w-full min-w-0` where the element is a flex child meant to fill.

- [ ] **Step 1: Open `missions-panel.tsx` and apply audit**

```bash
cd /work/surogates/sdk/agent-chat-react
grep -n "onClick\|truncate\|w-[0-9]" src/components/missions/missions-panel.tsx | head -40
```

For each match: confirm the change is needed and apply. Document touched classes by hand — don't blanket-replace.

- [ ] **Step 2: Open `scheduled-work-panel.tsx` and apply audit**

```bash
grep -n "onClick\|truncate\|w-[0-9]" src/components/scheduled/scheduled-work-panel.tsx | head -40
```
Apply audit.

- [ ] **Step 3: Open `session-tree-panel.tsx` and apply audit**

```bash
grep -n "onClick\|truncate\|w-[0-9]" src/components/sessions/session-tree-panel.tsx | head -40
```
Apply audit.

- [ ] **Step 4: Typecheck the SDK**

```bash
cd /work/surogates/sdk/agent-chat-react
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/components/missions/missions-panel.tsx \
        sdk/agent-chat-react/src/components/scheduled/scheduled-work-panel.tsx \
        sdk/agent-chat-react/src/components/sessions/session-tree-panel.tsx
git commit -m "feat(sdk): responsive touch targets and min-w-0 on sidebar panels"
```

---

## Task 15: SDK — `MissionDashboard` grid + tabstrip

**Files:**
- Modify: `sdk/agent-chat-react/src/components/missions/mission-dashboard.tsx`

- [ ] **Step 1: Locate grid + tabs**

```bash
grep -n "grid-cols\|TabsList\|overflow" /work/surogates/sdk/agent-chat-react/src/components/missions/mission-dashboard.tsx | head -20
```

- [ ] **Step 2: Change the main two-column grid**

Find the `grid-cols-[1fr_320px]` (or similar) and replace with `grid-cols-1 lg:grid-cols-[1fr_320px]`. If the grid uses `grid-cols-3` or other, the equivalent is `grid-cols-1 lg:grid-cols-3`.

- [ ] **Step 3: Make the tab strip horizontally scrollable**

Find the `TabsList` for this dashboard's tabs (Tasks / Workers / Evidence / Controls). Add `overflow-x-auto` to its className. If each `TabsTrigger` text wraps, also add `whitespace-nowrap` to the trigger className.

- [ ] **Step 4: Wrap evidence/workers tables**

Find each `<table>` inside this component. Wrap each in `<div className="overflow-x-auto md:overflow-visible">…</div>`. If a clear identity column is the first column, add `className="sticky left-0 bg-card"` to its `<th>` and `<td>`.

- [ ] **Step 5: SDK typecheck**

```bash
cd /work/surogates/sdk/agent-chat-react
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/components/missions/mission-dashboard.tsx
git commit -m "feat(sdk): responsive MissionDashboard grid and tabstrip"
```

---

## Task 16: SDK — `BrowserPane` and `WorkspacePanel`

**Files:**
- Modify: `sdk/agent-chat-react/src/components/browser/browser-pane.tsx`
- Modify: `sdk/agent-chat-react/src/components/workspace/workspace-panel.tsx`

- [ ] **Step 1: BrowserPane audit**

```bash
grep -n "header\|toolbar\|flex" /work/surogates/sdk/agent-chat-react/src/components/browser/browser-pane.tsx | head -20
```

For each header/toolbar `<div>` that has a `flex` direction and holds multiple controls: change `flex` → `flex flex-col md:flex-row` (or `flex-wrap` if appropriate). Ensure inputs inside use `min-w-0 flex-1` so they don't overflow.

- [ ] **Step 2: WorkspacePanel audit**

```bash
grep -n "header\|toolbar\|aspect\|h-full" /work/surogates/sdk/agent-chat-react/src/components/workspace/workspace-panel.tsx | head -20
```

Same header/toolbar reflow. On the file-preview area (iframe or image), add:
- `min-w-0` for flex correctness
- `aspect-video md:aspect-auto md:h-full` so on phone the preview keeps a sane aspect ratio rather than collapsing or overflowing

- [ ] **Step 3: SDK typecheck**

```bash
cd /work/surogates/sdk/agent-chat-react
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/components/browser/browser-pane.tsx \
        sdk/agent-chat-react/src/components/workspace/workspace-panel.tsx
git commit -m "feat(sdk): responsive BrowserPane and WorkspacePanel headers"
```

---

## Task 17: SDK — `AgentChat` mobile Chat/Workspace toggle (the big one)

**Files:**
- Modify: `sdk/agent-chat-react/src/agent-chat.tsx`

This is the only SDK component that needs more than a className sweep. The desktop layout uses absolute positioning with inline `style={{ width: 440 }}` / `style={{ right: 440 }}`. On phone, we need a flex stack with a tab toggle.

- [ ] **Step 1: Read the current layout**

```bash
grep -n "hasBrowserPanel\|workspacePath\|right: 440\|width: 440\|setWorkspacePath" /work/surogates/sdk/agent-chat-react/src/agent-chat.tsx | head
```

Confirm the structure matches: a `<section>` containing chat panel (absolute when `hasBrowserPanel`, flex otherwise) and a right stack (absolute, width 440, holds BrowserPane on top and WorkspacePanel on bottom).

- [ ] **Step 2: Add internal `mobileView` state**

Near the top of the component, alongside the existing `useState<string | null>(null)` for `workspacePath`, add:

```tsx
const [mobileView, setMobileView] = useState<"chat" | "workspace">("chat");
```

When `handleFileSelect` is called (a user clicks a file in the chat), also switch to workspace view on phone:

```tsx
const handleFileSelect = useCallback(
  (path: string) => {
    setWorkspacePath(path);
    setMobileView("workspace");
    onFileSelect?.(path);
  },
  [onFileSelect],
);
```

- [ ] **Step 3: Restructure the section's className**

Replace the section's current className expression with one that uses flex on phone and the existing layout on `md+`:

```tsx
<section
  data-testid="agent-chat-layout"
  data-mobile-view={mobileView}
  className={cn(
    // Phone: flex column stack, tab swap by data-mobile-view
    "flex min-h-0 flex-1 flex-col overflow-hidden bg-background text-sm text-foreground",
    // md+: original layout (absolute when hasBrowserPanel, otherwise flex row)
    hasBrowserPanel
      ? "md:relative md:flex-row"
      : "md:flex-row",
  )}
  style={{ direction: "ltr" }}
>
```

Add a `cn` import if missing (from the SDK's local `lib/utils`).

- [ ] **Step 4: Add the mobile tab strip**

Just inside the section, before the chat-panel and right-stack, render the toggle (only on `< md` and only when there's something in the workspace):

```tsx
<div className="md:hidden flex shrink-0 border-b border-line bg-card">
  <button
    type="button"
    onClick={() => setMobileView("chat")}
    aria-pressed={mobileView === "chat"}
    className={cn(
      "flex-1 px-4 py-3 text-sm font-medium border-b-2 -mb-px",
      mobileView === "chat"
        ? "border-primary text-foreground"
        : "border-transparent text-subtle",
    )}
  >
    Chat
  </button>
  <button
    type="button"
    onClick={() => setMobileView("workspace")}
    aria-pressed={mobileView === "workspace"}
    className={cn(
      "flex-1 px-4 py-3 text-sm font-medium border-b-2 -mb-px",
      mobileView === "workspace"
        ? "border-primary text-foreground"
        : "border-transparent text-subtle",
    )}
  >
    Workspace
  </button>
</div>
```

- [ ] **Step 5: Restructure chat panel and right stack className+style**

For the chat panel `<div data-testid="chat-panel">`:

```tsx
<div
  data-testid="chat-panel"
  className={cn(
    // Phone: full width, visible only when mobileView === 'chat'
    "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden",
    "data-[mobile-view=workspace]:hidden md:!flex",
    // md+: original positioning
    hasBrowserPanel
      ? "md:absolute md:inset-y-0 md:left-0 md:right-[var(--right-stack-w)]"
      : "md:relative md:flex-1",
  )}
  // The `data-mobile-view` on this child mirrors the parent's so the
  // attribute selector works without nested groups.
  data-mobile-view={mobileView}
  style={
    hasBrowserPanel
      ? ({ ["--right-stack-w" as string]: "440px" } as React.CSSProperties)
      : undefined
  }
>
```

For the right stack:

```tsx
<div
  data-testid="right-stack"
  data-mobile-view={mobileView}
  className={cn(
    // Phone: full width, only visible when mobileView === 'workspace'
    "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden",
    "data-[mobile-view=chat]:hidden md:!flex",
    // md+: original right-stack positioning
    hasBrowserPanel
      ? "md:absolute md:inset-y-0 md:right-0 md:w-[var(--right-stack-w,440px)] md:flex-none"
      : "md:relative md:shrink-0",
  )}
  style={
    hasBrowserPanel
      ? ({ ["--right-stack-w" as string]: "440px" } as React.CSSProperties)
      : undefined
  }
>
```

Notes:
- `!flex` overrides the `data-[mobile-view=…]:hidden` rule on `md+`. The `!` is Tailwind's `!important` modifier.
- The CSS variable `--right-stack-w` is set inline to keep desktop pixel-exact at 440px while letting the variable be referenced by both halves of the desktop layout.

- [ ] **Step 6: SDK typecheck**

```bash
cd /work/surogates/sdk/agent-chat-react
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/agent-chat.tsx
git commit -m "feat(sdk): mobile Chat/Workspace toggle in AgentChat layout"
```

---

## Task 18: SDK — UI primitive touch-target sweep

**Files:**
- Modify: `sdk/agent-chat-react/src/components/ui/button.tsx`
- Modify: `sdk/agent-chat-react/src/components/ui/input.tsx`
- Modify: `sdk/agent-chat-react/src/components/ui/input-group.tsx`
- Modify: `sdk/agent-chat-react/src/components/ui/dialog.tsx`
- Modify: `sdk/agent-chat-react/src/components/ui/item.tsx`

For each file, find the existing `min-h-*` or `h-*` rule that controls touch height. On `< md`, ensure at least `min-h-11` (44px). On `md+`, preserve current compact density.

- [ ] **Step 1: Audit `button.tsx`**

```bash
grep -n "min-h-\|h-9\|h-10\|h-11\|size:" /work/surogates/sdk/agent-chat-react/src/components/ui/button.tsx
```

For each size variant (default, sm, lg, icon, etc.), examine its className. If the default size has `h-9` (36px), change to `h-11 md:h-9` so on phone it's 44px. Do the same for `icon` and `icon-sm` variants if they're below 44px on phone.

- [ ] **Step 2: Audit `input.tsx`**

```bash
grep -n "h-9\|h-10\|h-11\|min-h-" /work/surogates/sdk/agent-chat-react/src/components/ui/input.tsx
```

Bump default input height: `h-11 md:h-9`.

- [ ] **Step 3: Audit `input-group.tsx`**

Same: if the wrapper sets a height, change to `h-11 md:h-9`.

- [ ] **Step 4: Audit `dialog.tsx` (close button + footer button heights)**

The close button in Dialog uses `size="icon-sm"`. If that variant in `button.tsx` is now correctly `h-11 md:h-9`, no change needed in dialog.tsx. Otherwise tweak directly.

- [ ] **Step 5: Audit `item.tsx`**

```bash
grep -n "py-\|h-\|min-h-" /work/surogates/sdk/agent-chat-react/src/components/ui/item.tsx
```

Items are clickable list rows. Ensure `min-h-11 md:min-h-0`.

- [ ] **Step 6: SDK typecheck**

```bash
cd /work/surogates/sdk/agent-chat-react
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/components/ui/button.tsx \
        sdk/agent-chat-react/src/components/ui/input.tsx \
        sdk/agent-chat-react/src/components/ui/input-group.tsx \
        sdk/agent-chat-react/src/components/ui/dialog.tsx \
        sdk/agent-chat-react/src/components/ui/item.tsx
git commit -m "feat(sdk): touch-target sweep on UI primitives"
```

---

## Task 19: Composer keyboard awareness wiring

**Files:**
- Likely modify: `sdk/agent-chat-react/src/components/chat/composer.tsx` (path may differ — search for it)

The web layer already mounts `useVisualViewport` in `__root.tsx` (Task 6), which keeps `--viewport-h` on `<html>` up to date. The remaining work is making the chat composer use that variable on phone.

- [ ] **Step 1: Find the composer**

```bash
grep -rln "Composer\|composer\.tsx" /work/surogates/sdk/agent-chat-react/src/components/chat/ | head
```

- [ ] **Step 2: Apply the height constraint**

Find the outermost wrapper of the chat scroll + composer pair (often in `ChatThread` or `agent-chat.tsx`'s chat-panel div). Add:

```tsx
className={cn("...existing...", "md:h-full h-[var(--viewport-h,100dvh)]")}
```

This causes the chat panel to use the visual-viewport height on phone, dropping when the keyboard appears. On `md+` it falls back to `h-full` (filling AppShell).

- [ ] **Step 3: Verify composer sticks to bottom**

Find the `<div>` wrapping the composer textarea inside the scroll container. Confirm it's `sticky bottom-0` or `flex-end` in a flex container. If it isn't, add `sticky bottom-0 bg-background z-10` to it.

- [ ] **Step 4: SDK typecheck**

```bash
cd /work/surogates/sdk/agent-chat-react
npx tsc --noEmit
```
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/
git commit -m "feat(sdk): composer respects mobile visual viewport"
```

---

## Task 20: Final verification — typecheck, biome, build, dev sanity

- [ ] **Step 1: Web typecheck + lint + build**

```bash
cd /work/surogates/web
npm run typecheck
npm run biome:check
npm run build
```
All three: exit 0.

- [ ] **Step 2: SDK typecheck**

```bash
cd /work/surogates/sdk/agent-chat-react
npx tsc --noEmit
```
Exit 0.

- [ ] **Step 3: Existing test suite**

```bash
cd /work/surogates/web
npm run test:shared-sdk
```
Exit 0.

- [ ] **Step 4: Cross-repo SDK compat check**

The web also installs `@invergent/agent-chat-react` from a sibling package (`file:../sdk/agent-chat-react`). Verify `/work/surogate-ops/frontend` still builds against the updated SDK (it consumes the same package, possibly via different install method per memory note about npm transition):

```bash
cd /work/surogate-ops/frontend
npm run typecheck 2>&1 | tail -30
```
Expected: exit 0 OR failures unrelated to our SDK changes. If failures are in files that import from `@invergent/agent-chat-react` and are caused by our changes, fix them by adjusting props/exports — but since this plan changes only className strings and adds internal state, breakage is unlikely.

- [ ] **Step 5: Dev server sanity**

```bash
cd /work/surogates/web
npm run dev &
sleep 6
curl -sI http://localhost:5173/ | head -3
```
Expected: `HTTP/1.1 200 OK` or similar. Kill the server.

```bash
pkill -f "vite" || true
```

- [ ] **Step 6: Manual responsive matrix (browser, by hand)**

Open Chrome devtools device mode and walk through each viewport × route cell:

Viewports: 360×640, 414×896, 768×1024, 1024×768, 1440×900.

Routes: `/chat`, `/chat/$sessionId` (with workspace), `/inbox` (list, detail), `/missions/$missionId`, `/agents`, `/skills`, `/settings`, `/login`.

For each cell, confirm:
- No horizontal page scroll on body.
- Sidebar accessible (drawer on phone, persistent on tablet+).
- All buttons have ≥ 44px hit area on phone (use devtools "Show ruler on hover").
- No content hidden under safe-area on phone.
- On `/chat` on phone: tap Workspace tab → workspace pane shows; tap Chat → chat shows; focus the composer textarea → composer stays visible above the (simulated) keyboard.

Document any failures inline by creating a follow-up task in this plan with the issue and recheck after fixing.

- [ ] **Step 7: iOS Safari real-device smoke (or explicit note)**

If a real iOS device is available: open the dev server on the LAN, walk through `/chat` and confirm composer behavior with the actual keyboard.

If NOT available: do not claim this step passed. In the final commit message, explicitly note "iOS Safari smoke test not run."

- [ ] **Step 8: Final commit (if any fixes were made in Step 6)**

```bash
git add -A
git commit -m "fix(web): responsive polish from manual verification"
```

If no fixes were needed, skip this step.

---

## Self-Review

**Spec coverage:** Walked each section of the spec. Every architectural element (AppShell, Sheet, useVisualViewport, sidebar refactor) has a dedicated task; every route reflow is covered; every SDK change is covered; verification matrix is enforced in Task 20.

**Placeholder scan:** Tasks 8 (inbox), 10 and 11 (agents/skills file-internal reflow), and 13 (login page exact path) contain inspection steps before edits because the exact filenames or APIs need to be confirmed first. These are not "TBD" placeholders — they are honest inspection-then-edit steps with concrete fallback code shown for both branches of the conditional. Task 19 likewise instructs to locate the composer file because its exact path varies across recent SDK reorganizations.

**Type consistency:** `AppShell` props (`sidebar`, `headerSlot`, `children`) used consistently across Tasks 4, 7, 8, 9, 10, 11, 12. `useVisualViewport` signature (`(): void`, writes `--viewport-h`) consistent across Tasks 3, 6, 19. Mobile view state in AgentChat (Task 17) is internal; no cross-task type contract.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-19-web-responsive-design-plan.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
