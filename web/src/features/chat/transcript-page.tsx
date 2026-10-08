// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A chat's transcript and nothing else: what Surogate Desktop shows of a thread in its Overview
// pane, beside the project's conversation. It is read, not written: the composer is off. Its
// look is the desktop's (lib/appearance.ts, main.tsx).

import { AgentChat } from "@invergent/agent-chat-react";
import { useParams } from "@tanstack/react-router";

import { surogatesWebChatAdapter } from "./surogates-web-chat-adapter";

export function TranscriptPage() {
  const { sessionId } = useParams({ from: "/transcript/$sessionId" });
  return (
    <div className="flex h-full min-h-0 w-full flex-col overflow-hidden">
      <AgentChat
        sessionId={sessionId}
        adapter={surogatesWebChatAdapter}
        disabled={true}
      />
    </div>
  );
}
