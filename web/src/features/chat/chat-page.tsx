// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useCallback, useEffect, useState } from "react";
import { useParams, useNavigate } from "@tanstack/react-router";
import { FolderOpenIcon } from "lucide-react";
import { ChatThread } from "@/components/chat/chat-thread";
import { SessionSidebar } from "@/components/navbar";
import { WorkspacePanel } from "@/components/workspace-panel";
import { FileViewer } from "@/components/file-viewer";
import { TransparencyBanner } from "@/components/transparency-banner";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { useAppStore } from "@/stores/app-store";
import { useSessionRuntime } from "@/hooks/use-session-runtime";
import * as sessionsApi from "@/api/sessions";
import {
  getTransparencyConfig,
  type TransparencyConfig,
} from "@/api/transparency";

export function ChatPage() {
  const navigate = useNavigate();
  const params = useParams({ strict: false }) as { sessionId?: string };
  const activeSessionId = useAppStore((s) => s.activeSessionId);
  const setActiveSession = useAppStore((s) => s.setActiveSession);
  const fetchSessions = useAppStore((s) => s.fetchSessions);
  const fetchUser = useAppStore((s) => s.fetchUser);
  const workspacePanelOpen = useAppStore((s) => s.workspacePanelOpen);
  const setWorkspacePanelOpen = useAppStore((s) => s.setWorkspacePanelOpen);

  const sessions = useAppStore((s) => s.sessions);
  const sessionsLoading = useAppStore((s) => s.sessionsLoading);

  // Load initial data on mount
  useEffect(() => {
    void fetchSessions();
    void fetchUser();
  }, [fetchSessions, fetchUser]);

  // Single effect to sync URL <-> store <-> session list.
  useEffect(() => {
    if (sessionsLoading) return;

    const urlId = params.sessionId;

    if (urlId) {
      const exists = sessions.some((s) => s.id === urlId);
      if (exists) {
        if (urlId !== activeSessionId) setActiveSession(urlId);
      } else {
        setActiveSession(null);
        void navigate({ to: "/chat", replace: true });
      }
      return;
    }

    if (activeSessionId) {
      void navigate({
        to: "/chat/$sessionId",
        params: { sessionId: activeSessionId },
        replace: true,
      });
    }
  }, [sessionsLoading, sessions, params.sessionId, activeSessionId, setActiveSession, navigate]);

  const setToolCheckpoint = useAppStore((s) => s.setToolCheckpoint);

  // EU AI Act transparency — fetch config once on mount.
  const [transparencyConfig, setTransparencyConfig] =
    useState<TransparencyConfig | null>(null);
  useEffect(() => {
    void getTransparencyConfig().then(setTransparencyConfig);
  }, []);

  // Per-session disclosure state.
  const [disclosureState, setDisclosureState] = useState<
    Record<string, "accepted" | "declined">
  >({});

  const sessionId = params.sessionId ?? activeSessionId;
  const { messages, isRunning } = useSessionRuntime(sessionId);

  // Show disclosure banner for new sessions.
  const sessionDisclosure = sessionId ? disclosureState[sessionId] : undefined;
  const needsDisclosure = !!(
    transparencyConfig?.enabled &&
    sessionId &&
    messages.length === 0 &&
    !sessionDisclosure
  );
  const sessionDeclined = sessionDisclosure === "declined";

  // Sync checkpoint hashes from tool calls into the workspace store.
  useEffect(() => {
    for (const msg of messages) {
      if (msg.toolCalls) {
        for (const tc of msg.toolCalls) {
          if (tc.checkpointHash) {
            setToolCheckpoint(tc.id, tc.checkpointHash);
          }
        }
      }
    }
  }, [messages, setToolCheckpoint]);

  // ── Handlers ──────────────────────────────────────────────────────

  const handleSend = useCallback(
    async (text: string) => {
      if (sessionDeclined) return;
      try {
        if (!sessionId) {
          const session = await sessionsApi.createSession({});
          await sessionsApi.sendMessage(session.id, text);
          setActiveSession(session.id);
          void fetchSessions();
          void navigate({
            to: "/chat/$sessionId",
            params: { sessionId: session.id },
          });
        } else {
          await sessionsApi.sendMessage(sessionId, text);
        }
      } catch (err) {
        console.error("Failed to send message:", err);
      }
    },
    [sessionId, sessionDeclined, setActiveSession, fetchSessions, navigate],
  );

  const handleStop = useCallback(async () => {
    if (sessionId) {
      try {
        await sessionsApi.pauseSession(sessionId);
      } catch (err) {
        console.error("Failed to stop session:", err);
      }
    }
  }, [sessionId]);

  const handleDisclosureConfirmed = useCallback(() => {
    if (!sessionId) return;
    setDisclosureState((prev) => ({ ...prev, [sessionId]: "accepted" }));
  }, [sessionId]);

  const handleDisclosureDeclined = useCallback(() => {
    if (!sessionId) return;
    setDisclosureState((prev) => ({ ...prev, [sessionId]: "declined" }));
  }, [sessionId]);

  // ── Render ────────────────────────────────────────────────────────

  return (
    <div className="flex h-screen w-full overflow-hidden">
      <SessionSidebar />
      <main className="flex-1 flex flex-col overflow-hidden relative">
        {needsDisclosure && sessionId && (
          <div className="absolute inset-x-0 top-0 z-30 p-4 flex justify-center">
            <TransparencyBanner
              sessionId={sessionId}
              level={transparencyConfig?.level ?? "basic"}
              onConfirmed={handleDisclosureConfirmed}
              onDeclined={handleDisclosureDeclined}
            />
          </div>
        )}

        {!workspacePanelOpen && sessionId && (
          <div className="absolute top-3 right-3 z-20">
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => setWorkspacePanelOpen(true)}
                >
                  <FolderOpenIcon className="w-4 h-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="left">Workspace files</TooltipContent>
            </Tooltip>
          </div>
        )}

        {sessionDeclined ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center text-muted-foreground space-y-3 px-4 max-w-5xl">
              <p className="font-medium text-red-400">Session disabled</p>
              <p className="leading-relaxed italic">
                In accordance with the EU Artificial Intelligence Act
                (Regulation 2024/1689), Articles 13 and 50, users must
                acknowledge that they are interacting with an AI system
                before it can process requests.
              </p>
              <p>
                Without your acknowledgment, this session cannot continue
                and has been deactivated.
              </p>
            </div>
          </div>
        ) : (
          <ChatThread
            messages={messages}
            isRunning={isRunning}
            onSend={handleSend}
            onStop={handleStop}
            disabled={sessionDeclined}
          />
        )}
      </main>
      <WorkspacePanel sessionId={sessionId ?? null} />
      <FileViewer />
    </div>
  );
}
