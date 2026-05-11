// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import { useEffect } from "react";
import { useNavigate } from "@tanstack/react-router";
import { InboxPanel } from "@invergent/agent-chat-react";
import { SessionSidebar } from "@/components/navbar";
import { useAppStore } from "@/stores/app-store";
import { surogatesWebChatAdapter } from "@/features/chat";

export function InboxPage() {
  const navigate = useNavigate();
  const fetchSessions = useAppStore((state) => state.fetchSessions);
  const fetchUser = useAppStore((state) => state.fetchUser);
  const setActiveSession = useAppStore((state) => state.setActiveSession);

  useEffect(() => {
    void fetchSessions();
    void fetchUser();
  }, [fetchSessions, fetchUser]);

  function handleSessionSelect(sessionId: string) {
    setActiveSession(sessionId);
    void navigate({ to: "/chat/$sessionId", params: { sessionId } });
  }

  return (
    <div className="flex h-screen w-full overflow-hidden">
      <SessionSidebar />
      <main className="min-w-0 flex-1 overflow-hidden">
        <InboxPanel
          adapter={surogatesWebChatAdapter}
          onSessionSelect={handleSessionSelect}
        />
      </main>
    </div>
  );
}
