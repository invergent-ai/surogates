// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { createRouter } from "@tanstack/react-router";
import { Route as rootRoute } from "./routes/__root";
import { Route as indexRoute } from "./routes/index";
import { Route as loginRoute } from "./routes/login";
import { chatRoute, chatSessionRoute } from "./routes/chat";
import { Route as linkRoute } from "./routes/link";
import { Route as settingsRoute } from "./routes/settings";

const routeTree = rootRoute.addChildren([
  indexRoute,
  loginRoute,
  linkRoute,
  settingsRoute,
  chatRoute.addChildren([chatSessionRoute]),
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
