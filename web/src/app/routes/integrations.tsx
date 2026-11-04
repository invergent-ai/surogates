// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import { createRoute, redirect } from "@tanstack/react-router";

import { requireAuth } from "../auth-guards";
import { Route as rootRoute } from "./__root";

// The integrations list lives in Settings › Connected Accounts; this address stays for old links
// and bookmarks.
export const Route = createRoute({
  getParentRoute: () => rootRoute,
  path: "/integrations",
  beforeLoad: async () => {
    await requireAuth();
    throw redirect({ to: "/settings", search: { tab: "accounts" } });
  },
  component: () => null,
});
