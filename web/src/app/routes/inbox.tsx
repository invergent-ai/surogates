// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import { createRoute } from "@tanstack/react-router";
import { lazy } from "react";
import { requireAuth } from "../auth-guards";
import { Route as rootRoute } from "./__root";

const InboxPage = lazy(() =>
  import("@/features/inbox/inbox-page").then((module) => ({
    default: module.InboxPage,
  })),
);

export const Route = createRoute({
  getParentRoute: () => rootRoute,
  path: "/inbox",
  beforeLoad: () => requireAuth(),
  component: InboxPage,
});
