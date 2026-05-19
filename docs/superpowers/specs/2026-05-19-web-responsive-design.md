# Surogates Web Responsive Design

**Date:** 2026-05-19
**Scope:** `/work/surogates/web` and `/work/surogates/sdk/agent-chat-react` (the panels the web app embeds)

## Goal

Make the surogates web app usable from ~360px phone widths up to wide desktop, in a single sweep covering all routes (chat, missions, inbox, agents, skills, settings, login) and the SDK panels they embed (`MissionsPanel`, `ScheduledWorkPanel`, `SessionTreePanel`, `MissionDashboard`, `BrowserPane`, `WorkspacePanel`).

Today the app uses fixed widths (`w-80` sidebar, `h-screen` viewport) and inlines the `<SessionSidebar />` in 7 route pages. There is a `useIsMobile` hook but it is unused. Only ~9 responsive-prefixed Tailwind classes exist across the whole codebase.

## Non-goals

- Visual regression snapshot infrastructure (Percy, Chromatic).
- Playwright touch-emulation E2E tests.
- Accessibility audit beyond touch targets and basic semantics.
- Per-row card transformations of dense tables (missions/agents/skills) — phone keeps horizontal-scroll for tables.
- Changes to SDK component APIs/props or behavior — visual-only sweep.

## Decisions

| Topic | Decision |
| --- | --- |
| Target | Phones + tablets + desktop (full responsive) |
| Mobile sidebar | Off-canvas drawer + hamburger |
| Content reflow | Single column, full-width on phone |
| Composer | Sticky bottom + viewport-aware (`dvh` + visualViewport API) |
| SDK panels | Made responsive too (className sweep only) |
| Workspace pane | Chat/Workspace toggle in chat header on mobile |
| Scope | Full sweep, single spec/plan |
| Layout primitive | New `<AppShell>` component (Tailwind-first; JS only where required) |
| Tables on phone | Horizontal scroll, not card lists |

## Architecture

### Breakpoints (Tailwind defaults)

- `< md` (< 768px) — phone: drawer sidebar, single column, top header with hamburger.
- `md` to `lg` (768–1023px) — tablet: persistent sidebar (collapsed, `w-14`), content reflows but stays multi-column where it fits.
- `≥ lg` (≥ 1024px) — desktop: persistent sidebar expandable (`w-80` / `w-14`), current layout.

The historic manual sidebar collapse button (`◂`/`▸`) is removed; collapse is purely viewport-driven.

### New components (web layer)

**`web/src/components/ui/sheet.tsx`** — Sheet primitive built on `radix-ui` Dialog (already available via the umbrella package; see `web/src/components/ui/dialog.tsx`). Slide-in from left, fixed positioning, focus trap, scroll lock, animated via `data-state` attribute selectors. Mirrors shadcn's Sheet API.

**`web/src/components/app-shell.tsx`** — The shell. Props:

```ts
type AppShellProps = {
  sidebar: ReactNode;
  headerSlot?: ReactNode;
  children: ReactNode;
};
```

Renders three regions:

1. Persistent `<aside class="hidden md:flex">` containing `sidebar`. Width: `w-14 lg:w-80`.
2. A `<Sheet>` mounted unconditionally, content gated by `className="md:hidden"`, containing the same `sidebar`. Open state lives in `AppShell` via `useState`. Sheet panel width: `w-80`.
3. A mobile-only `<header class="md:hidden h-12 border-b ...">` with hamburger button (opens the sheet), `headerSlot` (route-specific bits, e.g. Chat/Workspace toggle), and theme toggle.
4. `<main class="flex-1 min-w-0 flex flex-col overflow-hidden">{children}</main>`.

Root wrapper: `flex h-dvh w-full overflow-hidden`. `web/src/app/routes/__root.tsx` switches the existing `h-screen` to `h-dvh`.

The sidebar mounts twice (once in `<aside>`, once in `<Sheet>`) but both subscribe to the same Zustand store, so there is no data duplication — only two render trees. This is the explicit tradeoff for keeping the swap Tailwind-driven rather than JS-driven.

**`web/src/hooks/use-visual-viewport.ts`** — Single-mount hook that listens to `window.visualViewport`'s `resize` and `scroll` events and writes the effective height to a CSS custom property `--viewport-h` on `document.documentElement`. Used by the chat composer wrapper so it stays above the on-screen keyboard on mobile Safari/Chrome. Idempotent; cleans up its listeners on unmount.

### Sidebar component changes (`web/src/components/navbar.tsx`)

- Drop the internal `collapsed` state and `◂`/`▸` toggle button.
- Width is now driven by the parent: the persistent `<aside>` applies `w-14 lg:w-80`; the Sheet provides its own panel width.
- All current `if (collapsed) … else …` branches become Tailwind class pairs: e.g. footer user button is `hidden lg:flex` (in the desktop aside), but inside the Sheet it should always show. This is handled by tagging the sidebar root with `data-mode="aside"` (in `<aside>`) or `data-mode="sheet"` (inside Sheet); class rules then read `data-[mode=sheet]:flex` to override the `lg:` rule.
- The existing `useInboxUnreadCount` badge logic stays; only its positional class changes.

### `__root.tsx`

Replace `className="flex h-screen overflow-hidden ..."` with `className="flex h-dvh overflow-hidden ..."` for the non-bare layout. Bare routes (`/login`, `/link`) stay as-is but switch `h-screen` to `h-dvh` for keyboard correctness on phones.

## Route-by-route reflow

All non-bare routes are wrapped in `<AppShell>`. Inside `<main>`, each route handles its own internal reflow with Tailwind classes only.

### `/chat` and `/chat/$sessionId`

- `headerSlot` on mobile = segmented control with two options: "Chat" and "Workspace". State lives in `chat-page.tsx`.
- The two panes (messages, workspace) are wrapped so that on `< md`: messages visible iff toggle = Chat, workspace visible iff toggle = Workspace; on `md+` both always visible side-by-side and the toggle itself is `md:hidden`.
- Composer wrapper: `sticky bottom-0` inside the scroll container; container is `h-[var(--viewport-h,100dvh)]`. The `use-visual-viewport` hook keeps `--viewport-h` accurate when the on-screen keyboard appears.

### `/inbox`

- Currently a 2-column list+detail layout. On `< md`, becomes a router-driven list/detail swap: a search param (`?item=<id>`) selects the open item. List has `hidden md:block` when an item is present; detail is full-width on phone.
- A mobile-only back button in the detail view clears the search param. Browser back button works naturally because state lives in the URL.
- On `md+`: both panes visible side-by-side as today.

### `/missions` and `/missions/$missionId`

- Mission dashboard already has internal sections. On `< md`, the side stats column stacks below the main timeline via `grid grid-cols-1 lg:grid-cols-[1fr_320px]`.
- Evidence/workers tabs contain dense tables. Each table is wrapped in `<div class="overflow-x-auto md:overflow-visible">`; the first column gets `sticky left-0 bg-card` so identity stays visible while horizontal-scrolling on phone.

### `/agents`, `/skills`

- Filter/search bar stacks via `flex-col gap-2 md:flex-row md:items-center`.
- Tables: same treatment as missions — `overflow-x-auto md:overflow-visible` wrapper, sticky first column on phone.
- Row-action menus switch from hover-reveal to always-visible icon buttons on `< md` via `opacity-100 md:opacity-0 md:group-hover:opacity-100`.

### `/settings`

- Currently vertical tabs on the left + content on the right. Becomes a top horizontal scrollable tab strip on `< md`: `flex overflow-x-auto md:flex-col md:overflow-visible`, with content below. Pure Tailwind.

### `/login`, `/link` (bare routes — no AppShell)

- Add `px-4` and ensure the card has `max-w-md w-full` so it fits widths down to ~360px.
- Switch root `h-screen` to `h-dvh` (mirrors the non-bare change in `__root.tsx`).

## SDK panel changes

Pure className sweeps in `/work/surogates/sdk/agent-chat-react/src/components/`. No prop/API changes, no behavior changes.

### `MissionsPanel`, `ScheduledWorkPanel`, `SessionTreePanel`

- Touch targets: every clickable row gets `min-h-11` on `< md` and keeps current density on `md+` via `md:min-h-0`. Icon-only buttons that are < 44px get `p-2 md:p-1.5`.
- Text truncation: rows already use `truncate`. Audit parents and add `min-w-0` where missing (common Flexbox truncation pitfall).
- No horizontal overflow: audit each panel for fixed-width children and replace with `w-full min-w-0`.

### `MissionDashboard` (used by `/missions` detail and embedded in `/chat` workspace)

- Convert the desktop two-column grid `grid-cols-[1fr_320px]` to `grid-cols-1 lg:grid-cols-[1fr_320px]` so the stats sidebar drops below content on phone/tablet.
- Tab strip becomes `flex overflow-x-auto` on `< md` with `whitespace-nowrap` tabs.

### `BrowserPane`, `WorkspacePanel`

- Headers stack: `flex-col gap-2 md:flex-row`. Toolbars wrap: `flex-wrap`.
- The iframe / preview area gets `min-w-0` and `aspect-video md:aspect-auto md:h-full` so it does not overflow on phone.

### SDK component primitives (`components/ui/`)

- `button.tsx`, `input.tsx`, `input-group.tsx`, `dialog.tsx`, `item.tsx`: touch-target sweep — ensure min-heights ≥ 44px on `< md`. The grep showed they already use breakpoint classes, so this is a sweep, not a redesign.

## Testing & verification

### Automated

- `npm run typecheck` and `npm run biome:check` must pass on both `/work/surogates/web` and `/work/surogates/sdk/agent-chat-react`.
- `npm run build` on web — catches dead imports and broken aliases that a refactor of this size can introduce.
- Existing `npm run test:shared-sdk` in web must stay green.

No new layout test suite is added (high maintenance, low signal for className-only changes).

### Manual matrix

Viewports × routes the implementer must walk through before claiming done.

**Viewports:** 360×640, 414×896, 768×1024, 1024×768, 1440×900.

**Routes:** `/chat`, `/chat/$sessionId` (with workspace open), `/inbox` (list and detail), `/missions`, `/missions/$missionId`, `/agents`, `/skills`, `/settings`, `/login`.

**Per cell, confirm:**

- No horizontal page scroll on the body.
- Sidebar accessible (drawer on phone, persistent on tablet+).
- All buttons reachable with ≥ 44px hit area on phone.
- No content hidden under iOS safe-area insets.
- On phone chat: composer does not jump when keyboard opens.

### Browser sanity

- Chrome desktop devtools device mode for the matrix.
- One real-device smoke test on iOS Safari (where `dvh` and visualViewport bugs actually live). If unavailable, the implementer must explicitly call this out as not verified rather than claim a clean run.

## File-level change inventory

**New files:**

- `web/src/components/ui/sheet.tsx`
- `web/src/components/app-shell.tsx`
- `web/src/hooks/use-visual-viewport.ts`

**Modified files (web):**

- `web/src/app/routes/__root.tsx` — `h-screen` → `h-dvh`.
- `web/src/components/navbar.tsx` — drop internal collapse state, switch to Tailwind-driven widths and `data-mode` attribute.
- `web/src/features/chat/chat-page.tsx` — wrap in `<AppShell>`, add Chat/Workspace toggle in `headerSlot`, apply mobile pane-swap classes, wire composer to `--viewport-h`.
- `web/src/features/inbox/inbox-page.tsx` — wrap in `<AppShell>`, list/detail URL-driven swap.
- `web/src/features/missions/mission-page.tsx` — wrap in `<AppShell>`, grid breakpoint change, table wrappers.
- `web/src/features/agents/agents-page.tsx` — wrap in `<AppShell>`, filter bar stacking, table wrapper, row-action visibility.
- `web/src/features/skills/skills-page.tsx` — same pattern as agents.
- `web/src/features/settings/settings-page.tsx` — wrap in `<AppShell>`, tab strip orientation flip.
- `web/src/features/auth/*` (login route content) — `max-w-md w-full px-4`.
- `web/src/app/routes/link.tsx` — same min-width fix.

**Modified files (SDK):**

- `sdk/agent-chat-react/src/components/missions/missions-panel.tsx` and `mission-dashboard.tsx`
- `sdk/agent-chat-react/src/components/scheduled/scheduled-work-panel.tsx`
- `sdk/agent-chat-react/src/components/sessions/session-tree-panel.tsx`
- `sdk/agent-chat-react/src/components/browser/browser-pane.tsx`
- `sdk/agent-chat-react/src/components/workspace/workspace-panel.tsx`
- `sdk/agent-chat-react/src/components/ui/button.tsx`, `input.tsx`, `input-group.tsx`, `dialog.tsx`, `item.tsx`

Exact file paths inside the SDK will be verified during implementation; the inventory above lists the components in scope, not necessarily the precise filenames.

## Risks & open questions

- **Dual-mount sidebar cost:** Two Zustand subscribers for the same store. Negligible in practice but called out so future readers don't think it's a bug.
- **iOS Safari `dvh` quirks:** Some iOS versions report `dvh` slightly off when the URL bar animates. The `use-visual-viewport` hook covers the keyboard case; the URL-bar case is acceptable jitter and not worth additional JS.
- **SDK consumers outside this repo:** The SDK package is also consumed by `/work/surogate-ops/frontend` (per package metadata patterns). Pure-className changes should be backward-compatible, but the implementer should confirm the surogate-ops frontend still builds against the updated SDK.
