// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import { useEffect } from "react";
import { CodingAgentsPanel } from "@invergent/agent-chat-react";
import { createRoute, useNavigate } from "@tanstack/react-router";

import { surogatesWebChatAdapter } from "@/features/chat";
import { useAppStore } from "@/stores/app-store";
import { slashCommandEnabled } from "@/stores/capabilities-slice";
import { requireAuth } from "../auth-guards";
import { Route as rootRoute } from "./__root";

function CodingAgentsRoute() {
  const navigate = useNavigate();
  const fetchCapabilities = useAppStore((s) => s.fetchCapabilities);
  const slashCommands = useAppStore((s) => s.slashCommands);
  // Redirect only once capabilities have resolved (null = unknown, stay
  // put and fail open) and coding agents are disabled for this agent.
  const shouldRedirect =
    slashCommands !== null && !slashCommandEnabled(slashCommands, "code");

  useEffect(() => {
    void fetchCapabilities();
  }, [fetchCapabilities]);

  useEffect(() => {
    if (shouldRedirect) void navigate({ to: "/chat", replace: true });
  }, [shouldRedirect, navigate]);

  if (shouldRedirect) return null;

  return (
    <CodingAgentsPanel
      adapter={surogatesWebChatAdapter}
      onBack={() => void navigate({ to: "/chat" })}
    />
  );
}

export const Route = createRoute({
  getParentRoute: () => rootRoute,
  path: "/coding-agents",
  beforeLoad: () => requireAuth(),
  component: CodingAgentsRoute,
});
