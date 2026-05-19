// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Mission page — host shell around the SDK's <MissionDashboard>.
//
// The SDK component owns the data layer (polling) and the rendering
// (header / tasks / workers / cancel dialog). This file is just the
// surogates-web framing: the AppShell + main scrollable region +
// route param plumbing.
import { MissionDashboard } from "@invergent/agent-chat-react";
import { useNavigate, useParams } from "@tanstack/react-router";

import { AppShell } from "@/components/app-shell";
import { SessionSidebar } from "@/components/navbar";
import { surogatesWebChatAdapter } from "@/features/chat/surogates-web-chat-adapter";


export function MissionPage() {
  const navigate = useNavigate();
  const { missionId } = useParams({ strict: false }) as {
    missionId: string | undefined;
  };

  return (
    <AppShell sidebar={<SessionSidebar />}>
      <div className="flex-1 overflow-y-auto">
        {missionId ? (
          <MissionDashboard
            adapter={surogatesWebChatAdapter}
            missionId={missionId}
            onNavigateBack={() => {
              void navigate({ to: "/chat" });
            }}
            onOpenTranscript={(workerSessionId) => {
              void navigate({
                to: "/chat/$sessionId",
                params: { sessionId: workerSessionId },
              });
            }}
          />
        ) : (
          <div className="p-6 text-sm">Missing mission id in URL.</div>
        )}
      </div>
    </AppShell>
  );
}
