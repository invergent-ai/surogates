// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import { type AgentChatInboxItem, InboxPanel } from "@invergent/agent-chat-react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect } from "react";

import { AppShell } from "@/components/app-shell";
import { SessionSidebar } from "@/components/navbar";
import { surogatesWebChatAdapter } from "@/features/chat";
import { useAppStore } from "@/stores/app-store";

// Build the owning agent's hosted-page URL from THIS app's host: prod serves
// each agent at <slug>.<domain> (e.g. agent.cloud.surogate.ai), so swap the
// current first label for the owner's slug. Returns null when the host has no
// derivable domain (e.g. localhost:5174 in dev) — caller falls back to in-place.
function crossAgentChatUrl(agentSlug: string, sessionId: string): string | null {
  const { protocol, host } = window.location;
  const dot = host.indexOf(".");
  if (dot <= 0) return null; // localhost / single-label host → not derivable
  const domain = host.slice(dot + 1);
  return `${protocol}//${agentSlug}.${domain}/chat/${sessionId}`;
}

export function InboxPage() {
  const navigate = useNavigate();
  const fetchSessions = useAppStore((state) => state.fetchSessions);
  const fetchUser = useAppStore((state) => state.fetchUser);
  const fetchCapabilities = useAppStore((state) => state.fetchCapabilities);
  const setActiveSession = useAppStore((state) => state.setActiveSession);
  const currentAgentId = useAppStore((s) => s.agentId);
  const search = useSearch({ strict: false }) as { item?: number };

  useEffect(() => {
    void fetchSessions();
    void fetchUser();
    void fetchCapabilities();
  }, [fetchSessions, fetchUser, fetchCapabilities]);

  function handleSessionSelect(sessionId: string, item?: AgentChatInboxItem) {
    if (
      item?.agentId &&
      currentAgentId &&
      item.agentId !== currentAgentId &&
      item.agentSlug
    ) {
      const url = crossAgentChatUrl(item.agentSlug, sessionId);
      if (url) {
        window.open(url, "_blank", "noopener");
        return;
      }
    }
    setActiveSession(sessionId);
    void navigate({ to: "/chat/$sessionId", params: { sessionId } });
  }

  function handleSelectedIdChange(itemId: number | null) {
    void navigate({
      to: "/inbox",
      search: itemId === null ? {} : { item: itemId },
      replace: true,
    });
  }

  return (
    <AppShell sidebar={<SessionSidebar />}>
      <div className="min-w-0 flex-1 overflow-hidden">
        <InboxPanel
          adapter={surogatesWebChatAdapter}
          onSessionSelect={handleSessionSelect}
          selectedId={search.item ?? null}
          onSelectedIdChange={handleSelectedIdChange}
        />
      </div>
    </AppShell>
  );
}
