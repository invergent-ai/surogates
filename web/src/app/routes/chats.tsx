// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { createRoute } from "@tanstack/react-router";
import { lazy } from "react";
import { requireAuth } from "../auth-guards";
import { Route as rootRoute } from "./__root";

const ChatsPage = lazy(() =>
  import("@/features/chat/chats-page").then((m) => ({ default: m.ChatsPage })),
);

export const Route = createRoute({
  getParentRoute: () => rootRoute,
  path: "/chats",
  beforeLoad: () => requireAuth(),
  component: ChatsPage,
});
