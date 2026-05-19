// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { Outlet, createRootRoute, useRouterState } from "@tanstack/react-router";
import { Suspense } from "react";

import { useVisualViewport } from "@/hooks/use-visual-viewport";

import { AppProvider } from "../provider";

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
            ? "h-(--viewport-h,100dvh) bg-background text-foreground"
            : "flex h-(--viewport-h,100dvh) overflow-hidden bg-background text-foreground"
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
