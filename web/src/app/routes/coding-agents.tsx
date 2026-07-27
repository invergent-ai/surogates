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
  const enabled = slashCommandEnabled(slashCommands, "code");

  useEffect(() => {
    void fetchCapabilities();
  }, [fetchCapabilities]);

  // Deep links must not reach the panel when the agent has coding agents
  // disabled. Redirect once capabilities have resolved (null = unknown,
  // stay put and fail open).
  useEffect(() => {
    if (slashCommands !== null && !enabled) {
      void navigate({ to: "/chat", replace: true });
    }
  }, [slashCommands, enabled, navigate]);

  if (slashCommands !== null && !enabled) return null;

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
