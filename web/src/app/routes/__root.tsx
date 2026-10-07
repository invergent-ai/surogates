// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { Outlet, createRootRoute, useRouterState } from "@tanstack/react-router";
import { Suspense, useEffect } from "react";

import { authFetch, fetchCurrentUser } from "@/api/auth";
import { listSessions } from "@/api/sessions";
import { hasAuthToken } from "@/features/auth";
import { useVisualViewport } from "@/hooks/use-visual-viewport";
import { getDesktop, joinDesktop, leaveDesktop } from "@/lib/desktop-bridge";

import { AppProvider } from "../provider";

const BARE_ROUTES = ["/login", "/link", "/oauth/authorize"];

function RootLayout() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const isBare = BARE_ROUTES.includes(pathname);
  useVisualViewport();
  useDesktop(!isBare);

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

// In Surogate Desktop, once signed in: tell the desktop who is signed in, serve it their
// projects for its sidebar and Overview pane, and register this computer. A page with no
// sign-in, such as the sign-in page an expired session lands on, tells it nobody is.
function useDesktop(signedInRoute: boolean): void {
  useEffect(() => {
    const desktop = getDesktop();
    if (!desktop) return;
    if (!hasAuthToken()) {
      leaveDesktop(desktop);
      return;
    }
    if (!signedInRoute) return;
    return joinDesktop(desktop, {
      register: async (name) => {
        const response = await authFetch("/api/v1/devices", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        if (!response.ok) throw new Error(`Registering this computer failed (HTTP ${response.status})`);
        return (await response.json()) as { token: string };
      },
      account: async () => {
        const me = await fetchCurrentUser();
        return { name: me.display_name ?? me.email, email: me.email, userId: me.id, orgId: me.org_id };
      },
      sessions: async () =>
        (await listSessions({ includeDescendants: true, limit: 200 })).sessions.map((session) => ({
          id: session.id,
          parentId: session.parent_id,
          channel: session.channel,
          title: session.title,
          status: session.status,
          createdAt: session.created_at,
          updatedAt: session.updated_at,
        })),
    });
  }, [signedInRoute]);
}

export const Route = createRootRoute({
  component: RootLayout,
});
