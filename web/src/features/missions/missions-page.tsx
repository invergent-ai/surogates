// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The missions as a page: where Surogate Desktop, whose web client draws no sidebar, lists them,
// as /chats lists the chats. A browser has them in its sidebar too.

import { MissionsPanel } from "@invergent/agent-chat-react";
import { useNavigate } from "@tanstack/react-router";
import { useEffect } from "react";

import { AppShell } from "@/components/app-shell";
import { SessionSidebar } from "@/components/navbar";
import { surogatesWebChatAdapter } from "@/features/chat";
import { useAppStore } from "@/stores/app-store";
import { slashCommandEnabled } from "@/stores/capabilities-slice";

export function MissionsPage() {
  const navigate = useNavigate();
  const slashCommands = useAppStore((s) => s.slashCommands);
  const fetchCapabilities = useAppStore((s) => s.fetchCapabilities);

  useEffect(() => {
    fetchCapabilities();
  }, [fetchCapabilities]);

  function handleSelect(missionId: string) {
    navigate({ to: "/missions/$missionId", params: { missionId } });
  }

  return (
    <AppShell sidebar={<SessionSidebar />}>
      <div className="mx-auto flex min-h-0 w-full max-w-3xl flex-1 flex-col overflow-hidden px-4 py-6">
        <h1 className="mb-4 text-xl font-semibold text-foreground">Missions</h1>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {slashCommandEnabled(slashCommands, "mission") ? (
            <MissionsPanel
              adapter={surogatesWebChatAdapter}
              hideHeader={true}
              onMissionSelect={handleSelect}
            />
          ) : (
            <p className="px-4 py-8 text-center text-sm text-faint">
              No missions
            </p>
          )}
        </div>
      </div>
    </AppShell>
  );
}
