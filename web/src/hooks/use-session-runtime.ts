// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Bridges the Surogates SSE event stream (/v1/sessions/{id}/events)
// into ChatMessage objects consumed by the assistant-ui runtime adapter.

import { useEffect, useRef, useState, useCallback } from "react";
import { getAuthToken } from "@/features/auth";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: Date;
  status: "complete" | "streaming" | "error";
  toolCalls?: ToolCallInfo[];
  reasoning?: string;
}

export interface ToolCallInfo {
  id: string;
  toolName: string;
  args: string;
  result?: string;
  status: "running" | "complete" | "error";
}

// All event types the backend can emit
const LISTENED_EVENTS = [
  "user.message",
  "llm.request",
  "llm.response",
  "llm.thinking",
  "llm.delta",
  "tool.call",
  "tool.result",
  "session.start",
  "session.pause",
  "session.resume",
  "session.complete",
  "session.fail",
  "session.done",
  "harness.wake",
  "harness.crash",
  "policy.denied",
  "stream.timeout",
] as const;

export function useSessionRuntime(sessionId: string | null) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const eventSourceRef = useRef<EventSource | null>(null);
  const lastEventIdRef = useRef<number>(0);

  const connect = useCallback(() => {
    if (!sessionId) return;

    const token = getAuthToken();
    const url = new URL(
      `/api/v1/sessions/${sessionId}/events`,
      window.location.origin,
    );
    url.searchParams.set("after", String(lastEventIdRef.current));
    if (token) url.searchParams.set("token", token);

    const es = new EventSource(url.toString());
    eventSourceRef.current = es;

    // The backend sends named events (event: user.message, event: llm.delta, etc.)
    // Register a listener for each event type.
    for (const eventType of LISTENED_EVENTS) {
      es.addEventListener(eventType, (e: MessageEvent) => {
        const data = JSON.parse(e.data) as Record<string, unknown>;
        const eventId = e.lastEventId ? Number(e.lastEventId) : 0;
        if (eventId > lastEventIdRef.current) {
          lastEventIdRef.current = eventId;
        }
        applyEvent(eventType, eventId, data);
      });
    }

    es.onerror = () => {
      es.close();
      eventSourceRef.current = null;
      // Reconnect after a short delay (unless unmounted)
      setTimeout(() => {
        if (eventSourceRef.current === null) connect();
      }, 3000);
    };
  }, [sessionId]);

  const applyEvent = useCallback(
    (
      type: string,
      eventId: number,
      data: Record<string, unknown>,
    ) => {
      setMessages((prev) => {
        const next = [...prev];

        switch (type) {
          case "user.message": {
            next.push({
              id: `evt-${eventId}`,
              role: "user",
              content: (data.content as string) ?? "",
              createdAt: new Date(),
              status: "complete",
            });
            break;
          }

          case "llm.delta": {
            const streaming = findLastAssistant(next);
            if (streaming && streaming.status === "streaming") {
              streaming.content += (data.content as string) ?? "";
            } else {
              next.push({
                id: `evt-${eventId}`,
                role: "assistant",
                content: (data.content as string) ?? "",
                createdAt: new Date(),
                status: "streaming",
              });
            }
            setIsRunning(true);
            break;
          }

          case "llm.response": {
            const lastAssistant = findLastAssistant(next);
            if (lastAssistant) {
              lastAssistant.content =
                (data.content as string) ?? lastAssistant.content;
              lastAssistant.status = "complete";
            } else {
              next.push({
                id: `evt-${eventId}`,
                role: "assistant",
                content: (data.content as string) ?? "",
                createdAt: new Date(),
                status: "complete",
              });
            }
            setIsRunning(false);
            break;
          }

          case "llm.thinking": {
            const assistant = findLastAssistant(next);
            if (assistant) {
              assistant.reasoning =
                (assistant.reasoning ?? "") +
                ((data.content as string) ?? "");
            }
            setIsRunning(true);
            break;
          }

          case "tool.call": {
            let assistant = findLastAssistant(next);
            if (!assistant || assistant.status === "complete") {
              assistant = {
                id: `evt-${eventId}-tc`,
                role: "assistant",
                content: "",
                createdAt: new Date(),
                status: "streaming",
              };
              next.push(assistant);
            }
            assistant.toolCalls = assistant.toolCalls ?? [];
            assistant.toolCalls.push({
              id:
                (data.tool_call_id as string) ?? `tc-${eventId}`,
              toolName: (data.tool_name as string) ?? "unknown",
              args:
                typeof data.arguments === "string"
                  ? data.arguments
                  : JSON.stringify(data.arguments ?? {}),
              status: "running",
            });
            setIsRunning(true);
            break;
          }

          case "tool.result": {
            const assistant = findLastAssistant(next);
            const tc = assistant?.toolCalls?.find(
              (t) => t.id === (data.tool_call_id as string),
            );
            if (tc) {
              tc.result =
                typeof data.result === "string"
                  ? data.result
                  : JSON.stringify(data.result ?? null);
              tc.status = "complete";
            }
            break;
          }

          case "harness.wake":
          case "llm.request": {
            setIsRunning(true);
            break;
          }

          case "session.complete":
          case "session.fail":
          case "session.done":
          case "harness.crash": {
            // Mark any streaming assistant message as complete
            const last = findLastAssistant(next);
            if (last && last.status === "streaming") {
              last.status =
                type === "session.fail" || type === "harness.crash"
                  ? "error"
                  : "complete";
            }
            setIsRunning(false);
            break;
          }

          case "stream.timeout": {
            // Server closed the SSE stream due to max duration.
            // The onerror handler will reconnect automatically.
            break;
          }

          case "policy.denied": {
            const assistant = findLastAssistant(next);
            if (assistant) {
              assistant.content +=
                `\n\n**Policy denied**: ${(data.reason as string) ?? "Action blocked by governance policy."}`;
              assistant.status = "error";
            }
            setIsRunning(false);
            break;
          }
        }

        return next;
      });
    },
    [],
  );

  // Connect/disconnect on sessionId change
  useEffect(() => {
    if (!sessionId) {
      setMessages([]);
      setIsRunning(false);
      return;
    }

    lastEventIdRef.current = 0;
    setMessages([]);
    connect();

    return () => {
      const es = eventSourceRef.current;
      eventSourceRef.current = null; // prevent reconnect in onerror
      es?.close();
    };
  }, [sessionId, connect]);

  return { messages, isRunning };
}

function findLastAssistant(
  msgs: ChatMessage[],
): ChatMessage | undefined {
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === "assistant") return msgs[i];
  }
  return undefined;
}
