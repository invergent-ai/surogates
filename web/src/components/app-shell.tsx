// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
import { useRouterState } from "@tanstack/react-router";
import { MenuIcon, MoonIcon, SunIcon } from "lucide-react";
import { useTheme } from "next-themes";
import type * as React from "react";
import { useState } from "react";

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

  return (
    <div className="flex h-full w-full overflow-hidden">
      {/* Desktop / tablet: persistent aside.
          `group` + `data-mode` enables group-data-* variants
          in the sidebar component. */}
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
