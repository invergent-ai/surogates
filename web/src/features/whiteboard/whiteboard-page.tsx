import { AppShell } from "@/components/app-shell";
import { useAppStore } from "@/stores/app-store";
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { AgentWhiteboard } from "@invergent/agent-chat-react";
import { useNavigate, useParams } from "@tanstack/react-router";
import { useCallback, useEffect } from "react";
import { whiteboardChatAdapter } from "./whiteboard-chat-adapter";

export function WhiteboardPage() {
  const navigate = useNavigate();
  const params = useParams({ strict: false }) as { sessionId?: string };
  const fetchUser = useAppStore((s) => s.fetchUser);
  const fetchCapabilities = useAppStore((s) => s.fetchCapabilities);

  useEffect(() => {
    fetchUser().catch(() => undefined);
    fetchCapabilities().catch(() => undefined);
  }, [fetchUser, fetchCapabilities]);

  // The board auto-creates its session on the first Ask, so the route
  // only learns the id afterwards. Replace rather than push: the empty
  // /whiteboard URL is not a place anyone wants to go Back to.
  const onSessionChange = useCallback(
    (sessionId: string) => {
      navigate({
        to: "/whiteboard/$sessionId",
        params: { sessionId },
        replace: true,
      }).catch(() => undefined);
    },
    [navigate],
  );

  // The session-less route; the board creates a new session on its
  // first Ask.
  const onNewBoard = useCallback(() => {
    navigate({ to: "/whiteboard" }).catch(() => undefined);
  }, [navigate]);

  return (
    <AppShell>
      <div className="h-full w-full">
        <AgentWhiteboard
          adapter={whiteboardChatAdapter}
          sessionId={params.sessionId ?? null}
          onSessionChange={onSessionChange}
          onNewBoard={onNewBoard}
        />
      </div>
    </AppShell>
  );
}
