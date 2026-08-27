// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { createRoute } from "@tanstack/react-router";
import { lazy } from "react";
import { requireAuth } from "../auth-guards";
import { Route as rootRoute } from "./__root";

// The canvas pulls in MathJax on first formula; keep it out of the
// initial bundle for everyone who never opens a board.
const WhiteboardPage = lazy(() =>
  import("@/features/whiteboard/whiteboard-page").then((m) => ({
    default: m.WhiteboardPage,
  })),
);

export const whiteboardRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/whiteboard",
  beforeLoad: () => requireAuth(),
  component: WhiteboardPage,
});

export const whiteboardSessionRoute = createRoute({
  getParentRoute: () => whiteboardRoute,
  path: "/$sessionId",
  component: WhiteboardPage,
});
