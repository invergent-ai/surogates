// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useEffect } from "react";
import { useParams, useNavigate } from "@tanstack/react-router";
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type ThreadMessageLike,
  type ExternalStoreAdapter,
} from "@assistant-ui/react";
import { Thread } from "@/components/assistant-ui/thread";
import { SessionSidebar } from "@/components/navbar";
import { useAppStore } from "@/stores/app-store";
import { useSessionRuntime, type ChatMessage } from "@/hooks/use-session-runtime";
import * as sessionsApi from "@/api/sessions";

type TextPart = { type: "text"; text: string };
type ToolCallPart = {
  type: "tool-call";
  toolCallId: string;
  toolName: string;
  argsText: string;
  args: Record<string, unknown>;
  result?: string;
};
type MessagePart = TextPart | ToolCallPart;

function toThreadMessage(msg: ChatMessage): ThreadMessageLike {
  if (msg.role === "user") {
    return {
      role: "user",
      content: [{ type: "text" as const, text: msg.content }],
    };
  }

  const content: MessagePart[] = [];

  if (msg.reasoning) {
    content.push({ type: "text", text: msg.reasoning });
  }

  if (msg.toolCalls) {
    for (const tc of msg.toolCalls) {
      content.push({
        type: "tool-call",
        toolCallId: tc.id,
        toolName: tc.toolName,
        argsText: tc.args,
        args: {},
        result: tc.result,
      });
    }
  }

  content.push({ type: "text", text: msg.content });

  return {
    role: "assistant",
    content,
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

  const sessionId = params.sessionId ?? activeSessionId;
  const { messages, isRunning } = useSessionRuntime(sessionId);

  // Sync route param -> store
  useEffect(() => {
    if (params.sessionId && params.sessionId !== activeSessionId) {
      setActiveSession(params.sessionId);
    }
  }, [params.sessionId, activeSessionId, setActiveSession]);

  // Load initial data on mount
  useEffect(() => {
    void fetchSessions();
    void fetchUser();
  }, [fetchSessions, fetchUser]);

  const adapter: ExternalStoreAdapter<ThreadMessageLike> = {
    isRunning,
    messages: messages.map(toThreadMessage),
    onNew: async (message) => {
      const text = message.content
        .filter((p): p is TextPart => p.type === "text")
        .map((p) => p.text)
        .join("\n");

      if (!sessionId) {
        const session = await sessionsApi.createSession({});
        setActiveSession(session.id);
        void navigate({ to: "/chat/$sessionId", params: { sessionId: session.id } });
        await sessionsApi.sendMessage(session.id, text);
        void fetchSessions();
      } else {
        await sessionsApi.sendMessage(sessionId, text);
      }
    },
  };

  const runtime = useExternalStoreRuntime(adapter);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="flex h-screen w-full overflow-hidden">
        <SessionSidebar />
        <main className="flex-1 flex flex-col overflow-hidden">
          <Thread />
        </main>
      </div>
    </AssistantRuntimeProvider>
  );
}
