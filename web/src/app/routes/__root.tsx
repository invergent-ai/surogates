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
      {/* Fixed to the visual viewport, not just sized to it: opening the
          keyboard also scrolls the layout viewport out from under a shell
          that is merely the right height. See use-visual-viewport. */}
      <div
        className={
          isBare
            ? "fixed inset-x-0 top-(--viewport-top,0px) h-(--viewport-h,100dvh) overflow-y-auto bg-background text-foreground"
            : "fixed inset-x-0 top-(--viewport-top,0px) flex h-(--viewport-h,100dvh) overflow-hidden bg-background text-foreground"
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
