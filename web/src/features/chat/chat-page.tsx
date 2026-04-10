// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useCallback, useEffect, useState } from "react";
import { useParams, useNavigate } from "@tanstack/react-router";
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import { FolderOpenIcon } from "lucide-react";
import { Thread } from "@/components/assistant-ui/thread";
import { SessionSidebar } from "@/components/navbar";
import { WorkspacePanel } from "@/components/workspace-panel";
import { FileViewer } from "@/components/file-viewer";
import { TransparencyBanner } from "@/components/transparency-banner";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { useAppStore } from "@/stores/app-store";
import { useSessionRuntime, type ChatMessage } from "@/hooks/use-session-runtime";
import * as sessionsApi from "@/api/sessions";
import {
  getTransparencyConfig,
  type TransparencyConfig,
} from "@/api/transparency";

function toThreadMessage(msg: ChatMessage): ThreadMessageLike {
  if (msg.role === "user") {
    return {
      role: "user",
      content: [{ type: "text" as const, text: msg.content }],
    };
  }

  // Build assistant content as a plain array, then spread into the return.
  const parts: Array<
    | { type: "text"; text: string }
    | { type: "tool-call"; toolCallId: string; toolName: string; argsText: string; result?: string }
  > = [];

  if (msg.reasoning) {
    parts.push({ type: "text", text: msg.reasoning });
  }

  if (msg.toolCalls) {
    for (const tc of msg.toolCalls) {
      parts.push({
        type: "tool-call",
        toolCallId: tc.id,
        toolName: tc.toolName,
        argsText: tc.args,
        result: tc.result,
      });
    }
  }

  if (msg.content) {
    parts.push({ type: "text", text: msg.content });
  }

  return {
    role: "assistant",
    content: parts,
    status:
      msg.status === "streaming"
        ? { type: "running" }
        : { type: "complete", reason: "stop" },
  };
}

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

  // Single effect to sync URL ↔ store ↔ session list.
  useEffect(() => {
    if (sessionsLoading) return;

    const urlId = params.sessionId;

    // URL has a session ID — validate it exists.
    if (urlId) {
      const exists = sessions.some((s) => s.id === urlId);
      if (exists) {
        // Valid — sync to store if needed.
        if (urlId !== activeSessionId) setActiveSession(urlId);
      } else {
        // Deleted/archived — clear and go to base /chat.
        setActiveSession(null);
        void navigate({ to: "/chat", replace: true });
      }
      return;
    }

    // No URL session ID — auto-select from store or first in list.
    if (activeSessionId) {
      void navigate({ to: "/chat/$sessionId", params: { sessionId: activeSessionId }, replace: true });
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
  // "accepted" = confirmed, "declined" = user refused, undefined = pending.
  const [disclosureState, setDisclosureState] = useState<
    Record<string, "accepted" | "declined">
  >({});

  const sessionId = params.sessionId ?? activeSessionId;
  const { messages, isRunning } = useSessionRuntime(sessionId);

  // Show disclosure banner for new sessions (no messages yet, not resolved,
  // and transparency is enabled in server config).
  const sessionDisclosure = sessionId ? disclosureState[sessionId] : undefined;
  const needsDisclosure = !!(
    transparencyConfig?.enabled &&
    sessionId &&
    messages.length === 0 &&
    !sessionDisclosure
  );
  const sessionDeclined = sessionDisclosure === "declined";

  // Sync checkpoint hashes from tool calls into the workspace store
  // so the ToolFallback component can offer per-call rollback.
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

  const threadMessages = messages.map(toThreadMessage);

  const adapter = {
    isRunning,
    messages: threadMessages,
    convertMessage: (m: ThreadMessageLike) => m,
    onNew: async (message: { content: ReadonlyArray<{ type: string; text?: string }> }) => {
      // Block input if disclosure was declined.
      if (sessionDeclined) return;

      const text = message.content
        .filter((p): p is { type: "text"; text: string } => p.type === "text")
        .map((p) => p.text)
        .join("\n");

      try {
        if (!sessionId) {
          // Create session, send message, then navigate + refresh sidebar.
          const session = await sessionsApi.createSession({});
          await sessionsApi.sendMessage(session.id, text);
          setActiveSession(session.id);
          void fetchSessions();
          void navigate({ to: "/chat/$sessionId", params: { sessionId: session.id } });
        } else {
          await sessionsApi.sendMessage(sessionId, text);
        }
      } catch (err) {
        console.error("Failed to send message:", err);
      }
    },
  };

  // Disclosure handlers — called from the TransparencyBanner.
  const handleDisclosureConfirmed = useCallback(() => {
    if (!sessionId) return;
    setDisclosureState((prev) => ({ ...prev, [sessionId]: "accepted" }));
  }, [sessionId]);

  const handleDisclosureDeclined = useCallback(() => {
    // Decline = dismiss the banner, block input.
    // Tool execution will also be blocked server-side until confirmed.
    if (!sessionId) return;
    setDisclosureState((prev) => ({ ...prev, [sessionId]: "declined" }));
  }, [sessionId]);

  const runtime = useExternalStoreRuntime(adapter);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
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
                <p>Without your acknowledgment, this session cannot continue and has been deactivated.</p>
              </div>
            </div>
          ) : (
            <Thread />
          )}
        </main>
        <WorkspacePanel sessionId={sessionId ?? null} />
        <FileViewer />
      </div>
    </AssistantRuntimeProvider>
  );
}
