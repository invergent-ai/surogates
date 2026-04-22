// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useNavigate } from "@tanstack/react-router";
import { ChatThread } from "@/components/chat/chat-thread";
import { SessionSidebar } from "@/components/navbar";
import { WorkspacePanel } from "@/components/workspace-panel";
import { TransparencyBanner } from "@/components/transparency-banner";
import { useAppStore } from "@/stores/app-store";
import { useSessionRuntime } from "@/hooks/use-session-runtime";
import * as sessionsApi from "@/api/sessions";
import {
  getTransparencyConfig,
  type TransparencyConfig,
} from "@/api/transparency";

const PRE_SESSION_KEY = "__pre_session__";

export function ChatPage() {
  const navigate = useNavigate();
  const params = useParams({ strict: false }) as { sessionId?: string };
  const activeSessionId = useAppStore((s) => s.activeSessionId);
  const setActiveSession = useAppStore((s) => s.setActiveSession);
  const fetchSessions = useAppStore((s) => s.fetchSessions);
  const fetchUser = useAppStore((s) => s.fetchUser);
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
  // Ref tracks pre-session acceptance so handleSend can read it without
  // closing over the full disclosureState record.
  const preSessionAccepted = useRef(false);

  const sessionId = params.sessionId ?? activeSessionId;
  const {
    messages,
    isRunning,
    tokenUsage,
    forceStop,
    markSending,
    markSendError,
  } = useSessionRuntime(sessionId);

  // Show disclosure banner when transparency is enabled and the user has not
  // yet accepted.  This covers two states:
  //   1. No session yet (landing screen) — keyed by PRE_SESSION_KEY
  //   2. New session with zero messages — keyed by session id
  const disclosureKey = sessionId ?? PRE_SESSION_KEY;
  const sessionDisclosure = disclosureState[disclosureKey];
  const needsDisclosure = !!(
    transparencyConfig?.enabled &&
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
      // First message in a fresh session can't use the optimistic echo — the
      // runtime hook isn't tracking any session yet.  Create → send →
      // navigate; the SSE stream picks up once the new route mounts.
      if (!sessionId) {
        try {
          const session = await sessionsApi.createSession({});
          if (preSessionAccepted.current) {
            try {
              await sessionsApi.confirmDisclosure(session.id);
            } catch (err) {
              console.error("Failed to confirm disclosure:", err);
            }
          }
          await sessionsApi.sendMessage(session.id, text);
          setActiveSession(session.id);
          void fetchSessions();
          void navigate({
            to: "/chat/$sessionId",
            params: { sessionId: session.id },
          });
        } catch (err) {
          console.error("Failed to send message:", err);
        }
        return;
      }

      markSending(text);
      try {
        await sessionsApi.sendMessage(sessionId, text);
      } catch (err) {
        console.error("Failed to send message:", err);
        markSendError(err instanceof Error ? err.message : "send failed");
      }
    },
    [
      sessionId,
      sessionDeclined,
      setActiveSession,
      fetchSessions,
      navigate,
      markSending,
      markSendError,
    ],
  );

  const handleStop = useCallback(async () => {
    if (sessionId) {
      forceStop();
      try {
        await sessionsApi.pauseSession(sessionId);
      } catch (err) {
        console.error("Failed to stop session:", err);
      }
    }
  }, [sessionId, forceStop]);

  const fetchWorkspaceFile = useAppStore((s) => s.fetchWorkspaceFile);
  const handleFileSelect = useCallback(
    (path: string) => {
      if (!sessionId) return;
      void fetchWorkspaceFile(sessionId, path);
    },
    [sessionId, fetchWorkspaceFile],
  );

  const handleDisclosureConfirmed = useCallback(() => {
    if (disclosureKey === PRE_SESSION_KEY) preSessionAccepted.current = true;
    setDisclosureState((prev) => ({ ...prev, [disclosureKey]: "accepted" }));
  }, [disclosureKey]);

  const handleDisclosureDeclined = useCallback(() => {
    setDisclosureState((prev) => ({ ...prev, [disclosureKey]: "declined" }));
  }, [disclosureKey]);

  // ── Render ────────────────────────────────────────────────────────

  return (
    <div className="flex h-screen w-full overflow-hidden">
      <SessionSidebar />
      <main className="flex-1 flex flex-col overflow-hidden relative">
        {needsDisclosure && (
          <div className="absolute inset-x-0 top-0 z-30 p-4 flex justify-center">
            <TransparencyBanner
              sessionId={sessionId ?? undefined}
              level={transparencyConfig?.level ?? "basic"}
              onConfirmed={handleDisclosureConfirmed}
              onDeclined={handleDisclosureDeclined}
            />
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
            onFileSelect={handleFileSelect}
            disabled={sessionDeclined}
            tokenUsage={tokenUsage}
          />
        )}
      </main>
      <WorkspacePanel sessionId={sessionId ?? null} />
    </div>
  );
}
