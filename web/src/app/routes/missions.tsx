// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { createRoute, Outlet } from "@tanstack/react-router";
import { lazy } from "react";
import { requireAuth } from "../auth-guards";
import { Route as rootRoute } from "./__root";

const MissionPage = lazy(() =>
  import("@/features/missions/mission-page").then((m) => ({
    default: m.MissionPage,
  })),
);

const MissionsPage = lazy(() =>
  import("@/features/missions/missions-page").then((m) => ({
    default: m.MissionsPage,
  })),
);

export const missionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/missions",
  beforeLoad: () => requireAuth(),
  component: Outlet,
});

export const missionsIndexRoute = createRoute({
  getParentRoute: () => missionsRoute,
  path: "/",
  component: MissionsPage,
});

export const missionDetailRoute = createRoute({
  getParentRoute: () => missionsRoute,
  path: "/$missionId",
  component: MissionPage,
});
