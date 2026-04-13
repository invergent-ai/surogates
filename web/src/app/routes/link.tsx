// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { createRoute } from "@tanstack/react-router";
import { lazy } from "react";
import { requireAuth } from "../auth-guards";
import { Route as rootRoute } from "./__root";

const LinkChannelPage = lazy(() =>
  import("@/features/auth/link-channel-page").then((m) => ({
    default: m.LinkChannelPage,
  })),
);

export const Route = createRoute({
  getParentRoute: () => rootRoute,
  path: "/link",
  beforeLoad: () => requireAuth(),
  component: LinkChannelPage,
});
