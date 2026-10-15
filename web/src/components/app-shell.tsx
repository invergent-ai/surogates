// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
import { useRouterState } from "@tanstack/react-router";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  MenuIcon,
  MoonIcon,
  SunIcon,
  XIcon,
} from "lucide-react";
import { useTheme } from "next-themes";
import type * as React from "react";
import { useState } from "react";

import { getAppRouteTitle } from "@/components/app-route-title";
import { ComputerAddedBanner } from "@/components/computer-added-banner";
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetTitle,
} from "@/components/ui/sheet";
import { getDesktop } from "@/lib/desktop-bridge";
import { useAppStore } from "@/stores/app-store";

type AppShellProps = {
  sidebar: React.ReactNode;
  /**
   * Contents of the phone header's title area. Defaults to the current route's
   * name — no page was passing this, so the header rendered blank on every
   * route while the page below spelled its own title out. Pass something only
   * where a page has a better title than its route does.
   */
  headerSlot?: React.ReactNode;
  children: React.ReactNode;
};

export function AppShell({ sidebar, headerSlot, children }: AppShellProps) {
  const [sheetOpen, setSheetOpen] = useState(false);
  const { theme, setTheme } = useTheme();
  // Pinned, the sidebar stays open; unpinned, it is an icon rail that opens
  // while the pointer is over it — the ops console's rail.
  const pinned = useAppStore((s) => s.sidebarPinned);
  const setPinned = useAppStore((s) => s.setSidebarPinned);
  const [hoverOpen, setHoverOpen] = useState(false);
  const railOpen = pinned || hoverOpen;

  // Close the sheet on route change so a sidebar navigation doesn't leave
  // the drawer open over the new page. React's documented "store info from
  // previous renders" pattern — guarded setState during render.
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const [prevPathname, setPrevPathname] = useState(pathname);
  if (prevPathname !== pathname) {
    setPrevPathname(pathname);
    if (sheetOpen) {
      setSheetOpen(false);
    }
  }

  // In Surogate Desktop the web client is the conversation alone: the desktop's own sidebar
  // lists the projects, the chats (on /chats) and the pages, and its user menu reaches Settings.
  if (getDesktop()) {
    return (
      <main className="flex h-full min-h-0 w-full min-w-0 flex-col overflow-hidden">
        <ComputerAddedBanner />
        {children}
      </main>
    );
  }

  return (
    <div className="flex h-full w-full overflow-hidden">
      {/* Desktop / tablet: persistent aside.
          `group` + `data-mode` enables group-data-* variants
          in the sidebar component. */}
      <aside
        data-mode="aside"
        data-collapsed={railOpen ? undefined : ""}
        onMouseEnter={() => setHoverOpen(true)}
        onMouseLeave={() => setHoverOpen(false)}
        className="group hidden md:flex md:w-14 md:min-w-14 lg:w-80 lg:min-w-80 lg:data-collapsed:w-14 lg:data-collapsed:min-w-14 bg-card border-r border-line flex-col overflow-hidden z-10 transition-[width,min-width] duration-200"
      >
        <div className="min-h-0 flex-1">{sidebar}</div>
        {/* Only where there is a full sidebar to keep: below lg it is the
            icon rail regardless. */}
        <button
          type="button"
          onClick={() => {
            if (pinned) setHoverOpen(false);
            setPinned(!pinned);
          }}
          aria-label={pinned ? "Collapse sidebar" : "Expand sidebar"}
          className="hidden shrink-0 items-center justify-center border-t border-line py-2.5 text-faint transition-colors hover:text-foreground lg:flex"
        >
          {pinned ? (
            <ChevronLeftIcon className="size-4" strokeWidth={1.75} />
          ) : (
            <ChevronRightIcon className="size-4" strokeWidth={1.75} />
          )}
        </button>
      </aside>

      {/* Phone: sheet drawer. It owns the device insets — the footer used to
          sit under the home indicator — and keeps a visible way to shut
          itself: swiping and tapping the scrim both work, but neither is
          visible, and a drawer with no button to close reads as stuck. */}
      <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
        <SheetContent
          side="left"
          // The built-in close is 32px and sits at a fixed `top-3`, which on a
          // notched phone lands inside the notch; this one clears the inset
          // and meets the 44px touch minimum.
          showCloseButton={false}
          className="p-0 pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] ps-[env(safe-area-inset-left)] md:hidden"
        >
          {/* The sidebar below carries its own visible branding, so this is
              the accessible name only — Radix Dialog requires one. */}
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <SheetClose
            aria-label="Close navigation"
            className="absolute end-2 top-[calc(0.75rem+env(safe-area-inset-top))] z-10 inline-flex size-11 items-center justify-center rounded-xl text-subtle transition-colors active:bg-input"
          >
            <XIcon className="size-5" />
          </SheetClose>
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
        {/* Pinned to the top edge, so it carries the notch inset itself. */}
        <header className="shrink-0 border-b border-line pt-[env(safe-area-inset-top)] md:hidden">
          <div className="flex h-14 items-center gap-2 px-2">
            <button
              type="button"
              onClick={() => setSheetOpen(true)}
              aria-label="Open navigation"
              className="inline-flex size-11 shrink-0 items-center justify-center rounded-lg text-subtle active:bg-input"
            >
              <MenuIcon className="h-5 w-5" />
            </button>
            <div className="min-w-0 flex-1 truncate text-[15px] font-semibold">
              {headerSlot ?? getAppRouteTitle(pathname)}
            </div>
            <button
              type="button"
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
              aria-label="Toggle theme"
              className="inline-flex size-11 shrink-0 items-center justify-center rounded-lg text-subtle active:bg-input"
            >
              {theme === "dark" ? (
                <SunIcon className="h-4 w-4" />
              ) : (
                <MoonIcon className="h-4 w-4" />
              )}
            </button>
          </div>
        </header>

        <main className="flex min-h-0 flex-1 min-w-0 flex-col overflow-hidden">
          <ComputerAddedBanner />
          {children}
        </main>
      </div>
    </div>
  );
}
