// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { createRoute } from "@tanstack/react-router";
import { lazy } from "react";
import { requireAuth } from "../auth-guards";
import { Route as rootRoute } from "./__root";

const SkillsPage = lazy(() =>
  import("@/features/skills/skills-page").then((m) => ({
    default: m.SkillsPage,
  })),
);

export const Route = createRoute({
  getParentRoute: () => rootRoute,
  path: "/skills",
  beforeLoad: () => requireAuth(),
  component: SkillsPage,
});
