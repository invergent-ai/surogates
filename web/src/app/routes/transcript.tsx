// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { createRoute } from "@tanstack/react-router";
import { lazy } from "react";
import { requireAuth } from "../auth-guards";
import { Route as rootRoute } from "./__root";

const TranscriptPage = lazy(() =>
  import("@/features/chat/transcript-page").then((m) => ({
    default: m.TranscriptPage,
  })),
);

// A chat's transcript alone, as Surogate Desktop's Overview pane reads a thread.
export const Route = createRoute({
  getParentRoute: () => rootRoute,
  path: "/transcript/$sessionId",
  beforeLoad: () => requireAuth(),
  component: TranscriptPage,
});
