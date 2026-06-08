// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import { IntegrationsPage } from "@invergent/agent-chat-react";
import { createRoute, useNavigate } from "@tanstack/react-router";

import { surogatesWebChatAdapter } from "@/features/chat";
import { requireAuth } from "../auth-guards";
import { Route as rootRoute } from "./__root";

function IntegrationsRoute() {
  const navigate = useNavigate();
  return (
    <IntegrationsPage
      adapter={surogatesWebChatAdapter}
      onBack={() => void navigate({ to: "/chat" })}
    />
  );
}

export const Route = createRoute({
  getParentRoute: () => rootRoute,
  path: "/integrations",
  beforeLoad: () => requireAuth(),
  component: IntegrationsRoute,
});
