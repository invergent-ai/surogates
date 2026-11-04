// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { createRoute } from "@tanstack/react-router";
import { lazy } from "react";
import { requireAuth } from "../auth-guards";
import { Route as rootRoute } from "./__root";

const SettingsPage = lazy(() =>
  import("@/features/settings/settings-page").then((m) => ({
    default: m.SettingsPage,
  })),
);

export const Route = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  beforeLoad: () => requireAuth(),
  // The section to open: Devices, from the "computer added" notice; Connected accounts, from the
  // chat's "Connect your accounts" band.
  validateSearch: (search: Record<string, unknown>): { tab?: "devices" | "accounts" } =>
    search.tab === "devices" || search.tab === "accounts" ? { tab: search.tab } : {},
  component: SettingsPage,
});
