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
import { AppShell } from "@/components/app-shell";
import { SessionSidebar } from "@/components/navbar";
import { TransparencyBanner } from "@/components/transparency-banner";
import { useAppStore } from "@/stores/app-store";
import { slashCommandEnabled } from "@/stores/capabilities-slice";
import * as sessionsApi from "@/api/sessions";
import {
  getTransparencyConfig,
  type TransparencyConfig,
} from "@/api/transparency";
import {
  surogatesWebChatAdapter,
  toAgentChatSession,
} from "./surogates-web-chat-adapter";
import { getChatRouteState } from "./chat-route-state";

const PRE_SESSION_KEY = "__pre_session__";

export function ChatPage() {
  const navigate = useNavigate();
  const params = useParams({ strict: false }) as { sessionId?: string };
  const activeSessionId = useAppStore((s) => s.activeSessionId);
  const setActiveSession = useAppStore((s) => s.setActiveSession);
  const fetchSessions = useAppStore((s) => s.fetchSessions);
  const fetchUser = useAppStore((s) => s.fetchUser);
  const sessionsLoading = useAppStore((s) => s.sessionsLoading);
  const upsertSession = useAppStore((s) => s.upsertSession);
  const fetchCapabilities = useAppStore((s) => s.fetchCapabilities);
  const slashCommands = useAppStore((s) => s.slashCommands);

  // Load initial data on mount
  useEffect(() => {
    void fetchSessions();
    void fetchUser();
    void fetchCapabilities();
  }, [fetchSessions, fetchUser, fetchCapabilities]);

  const chatRouteState = getChatRouteState({
    activeSessionId,
    sessionsLoading,
    urlSessionId: params.sessionId,
  });

  // Single effect to sync URL <-> store <-> session list.
  useEffect(() => {
    if (sessionsLoading) return;

    if (chatRouteState.nextActiveSessionId !== activeSessionId) {
      setActiveSession(chatRouteState.nextActiveSessionId);
    }

    if (chatRouteState.redirectTo === "/chat") {
      void navigate({ to: "/chat", replace: true });
    }
  }, [
    sessionsLoading,
    chatRouteState.nextActiveSessionId,
    chatRouteState.redirectTo,
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

  const sessionId = chatRouteState.sessionId;
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

  const chatAdapter = useMemo<AgentChatAdapter>(
    () => ({
      ...surogatesWebChatAdapter,
      async createSession(input) {
        const rawSession = await sessionsApi.createSession({
          system: input.system,
        });
        upsertSession(rawSession);
        handleSessionChange(rawSession.id);
        if (preSessionAccepted.current) {
          try {
            await sessionsApi.confirmDisclosure(rawSession.id);
            setDisclosureState((prev) => ({
              ...prev,
              [rawSession.id]: "accepted",
            }));
          } catch (err) {
            console.error("Failed to confirm disclosure:", err);
          }
        }
        return toAgentChatSession(rawSession);
      },
    }),
    [handleSessionChange, upsertSession],
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
    <AppShell sidebar={<SessionSidebar />}>
      <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
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
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
            <AgentChat
              sessionId={sessionId ?? null}
              adapter={chatAdapter}
              onSessionChange={handleSessionChange}
              disabled={sessionDeclined}
              onMessagesChange={setChatMessages}
              onOpenIntegrations={() => void navigate({ to: "/integrations" })}
              // Hide built-in slash commands the agent has disabled. Unknown
              // (capabilities not yet loaded) fails open via the helper.
              compressEnabled={slashCommandEnabled(slashCommands, "compress")}
              loopsEnabled={slashCommandEnabled(slashCommands, "loop")}
              missionsEnabled={slashCommandEnabled(slashCommands, "mission")}
              goalsEnabled={slashCommandEnabled(slashCommands, "goal")}
              codeAgentsEnabled={slashCommandEnabled(slashCommands, "code")}
              deepResearchEnabled={slashCommandEnabled(
                slashCommands,
                "deep-research",
              )}
              researchEnabled={slashCommandEnabled(
                slashCommands,
                "auto-research",
              )}
            />
          </div>
        )}
      </div>
    </AppShell>
  );
}
