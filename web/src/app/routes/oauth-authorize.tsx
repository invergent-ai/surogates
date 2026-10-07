// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { createRoute } from "@tanstack/react-router";
import { lazy } from "react";
import { Route as rootRoute } from "./__root";

const AuthorizePage = lazy(() =>
  import("@/features/auth/authorize-page").then((m) => ({ default: m.AuthorizePage })),
);

// Surogate Desktop's sign-in in the system browser. No guard: the page signs in itself, and keeps its query.
export const Route = createRoute({
  getParentRoute: () => rootRoute,
  path: "/oauth/authorize",
  component: AuthorizePage,
});
