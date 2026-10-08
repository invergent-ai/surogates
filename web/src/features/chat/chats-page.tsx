// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The user's chats as a page: where Surogate Desktop, whose own sidebar lists the projects, lists
// the plain chats, each on a folder of a computer marked with its laptop. A browser has them in
// its sidebar too.

import { SessionTreePanel } from "@invergent/agent-chat-react";
import { Navigate, useNavigate } from "@tanstack/react-router";
import { useEffect } from "react";

import { AppShell } from "@/components/app-shell";
import { SessionSidebar } from "@/components/navbar";
import { useAppStore } from "@/stores/app-store";

import { surogatesWebChatAdapter } from "./surogates-web-chat-adapter";

export function ChatsPage() {
  const navigate = useNavigate();
  const fetchSessions = useAppStore((s) => s.fetchSessions);
  const fetchUser = useAppStore((s) => s.fetchUser);
  const fetchCapabilities = useAppStore((s) => s.fetchCapabilities);
  const setActiveSession = useAppStore((s) => s.setActiveSession);
  const removeSession = useAppStore((s) => s.removeSession);
  const sessions = useAppStore((s) => s.sessions);
  const sessionsLoading = useAppStore((s) => s.sessionsLoading);
  // One conversation per user: no list, as the sidebar keeps none, only the conversation.
  const singleSession = useAppStore((s) => s.multiSession) === false;

  useEffect(() => {
    fetchSessions();
    fetchUser();
    fetchCapabilities();
  }, [fetchSessions, fetchUser, fetchCapabilities]);

  if (singleSession) {
    return <Navigate to="/chat" replace={true} />;
  }

  function handleSelect(sessionId: string) {
    setActiveSession(sessionId);
    navigate({ to: "/chat/$sessionId", params: { sessionId } });
  }

  return (
    <AppShell sidebar={<SessionSidebar />}>
      <div className="mx-auto flex min-h-0 w-full max-w-3xl flex-1 flex-col overflow-hidden px-4 py-6">
        <h1 className="mb-4 text-xl font-semibold text-foreground">Chats</h1>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <SessionTreePanel
            adapter={surogatesWebChatAdapter}
            loadList={true}
            hideHeader={true}
            onSessionSelect={handleSelect}
            onSessionDelete={removeSession}
          />
          {!sessionsLoading && sessions.length === 0 && (
            <p className="px-4 py-8 text-center text-sm text-faint">
              No chats yet
            </p>
          )}
        </div>
      </div>
    </AppShell>
  );
}
