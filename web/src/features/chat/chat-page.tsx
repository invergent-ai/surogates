// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useNavigate } from "@tanstack/react-router";
import {
  AgentChat,
  type AgentChatAdapter,
  type AgentChatMessage,
} from "@invergent/agent-chat-react";
import { SessionSidebar } from "@/components/navbar";
import { TransparencyBanner } from "@/components/transparency-banner";
import { useAppStore } from "@/stores/app-store";
import * as sessionsApi from "@/api/sessions";
import {
  getTransparencyConfig,
  type TransparencyConfig,
} from "@/api/transparency";
import { surogatesWebChatAdapter } from "./surogates-web-chat-adapter";

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
  }, [
    sessionsLoading,
    sessions,
    params.sessionId,
    activeSessionId,
    setActiveSession,
    navigate,
  ]);

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
  const [chatMessages, setChatMessages] = useState<AgentChatMessage[]>([]);

  // Show disclosure banner when transparency is enabled and the user has not
  // yet accepted.  This covers two states:
  //   1. No session yet (landing screen) — keyed by PRE_SESSION_KEY
  //   2. New session with zero messages — keyed by session id
  const disclosureKey = sessionId ?? PRE_SESSION_KEY;
  const sessionDisclosure = disclosureState[disclosureKey];
  const needsDisclosure = !!(
    transparencyConfig?.enabled &&
    chatMessages.length === 0 &&
    !sessionDisclosure
  );
  const sessionDeclined = sessionDisclosure === "declined";

  // Sync checkpoint hashes from tool calls into the workspace store.
  useEffect(() => {
    for (const msg of chatMessages) {
      if (msg.toolCalls) {
        for (const tc of msg.toolCalls) {
          if (tc.checkpointHash) {
            setToolCheckpoint(tc.id, tc.checkpointHash);
          }
        }
      }
    }
  }, [chatMessages, setToolCheckpoint]);

  // ── Handlers ──────────────────────────────────────────────────────

  const chatAdapter = useMemo<AgentChatAdapter>(
    () => ({
      ...surogatesWebChatAdapter,
      async createSession(input) {
        const session = await surogatesWebChatAdapter.createSession(input);
        if (preSessionAccepted.current) {
          try {
            await sessionsApi.confirmDisclosure(session.id);
            setDisclosureState((prev) => ({
              ...prev,
              [session.id]: "accepted",
            }));
          } catch (err) {
            console.error("Failed to confirm disclosure:", err);
          }
        }
        return session;
      },
    }),
    [],
  );

  const handleSessionChange = useCallback(
    (nextSessionId: string) => {
      setActiveSession(nextSessionId);
      void fetchSessions();
      void navigate({
        to: "/chat/$sessionId",
        params: { sessionId: nextSessionId },
      });
    },
    [fetchSessions, navigate, setActiveSession],
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
          <AgentChat
            sessionId={sessionId ?? null}
            adapter={chatAdapter}
            onSessionChange={handleSessionChange}
            disabled={sessionDeclined}
            onMessagesChange={setChatMessages}
          />
        )}
      </main>
    </div>
  );
}
