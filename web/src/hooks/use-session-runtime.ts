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
  checkpointHash?: string;
}

export interface TokenUsage {
  inputTokens: number;
  outputTokens: number;
  reasoningTokens: number;
  cachedInputTokens: number;
  totalTokens: number;
  contextWindow: number;
  model: string;
}

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
  "context.compact",
  "policy.denied",
  "stream.timeout",
] as const;

const EMPTY_USAGE: TokenUsage = {
  inputTokens: 0,
  outputTokens: 0,
  reasoningTokens: 0,
  cachedInputTokens: 0,
  totalTokens: 0,
  contextWindow: 0,
  model: "",
};

export function useSessionRuntime(sessionId: string | null) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [tokenUsage, setTokenUsage] = useState<TokenUsage>(EMPTY_USAGE);
  const esRef = useRef<EventSource | null>(null);
  const lastEventIdRef = useRef<number>(0);
  const sessionDoneRef = useRef<boolean>(false);
  // Tracks whether llm.delta events have streamed content for the current
  // turn.  When true, llm.response content is redundant and ignored.
  const hadDeltasRef = useRef<boolean>(false);
  // Ref to the latest connect function so onerror can call it without
  // a circular declaration dependency.
  const connectRef = useRef<() => void>(() => {});

  const applyEvent = useCallback(
    (type: string, eventId: number, data: Record<string, unknown>) => {
      if (type === "session.done") {
        sessionDoneRef.current = true;
      }

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
            hadDeltasRef.current = true;
            const lastIdx = findLastAssistantIndex(next);
            const lastMsg = lastIdx >= 0 ? next[lastIdx] : null;
            // Append to the current message only if it's streaming,
            // doesn't already have tool calls, AND there's no user
            // message after it (which means a new turn started).
            const hasUserAfter = lastIdx >= 0 && next.slice(lastIdx + 1).some(
              (m) => m.role === "user",
            );
            const canAppend = !!(
              lastMsg &&
              lastMsg.status === "streaming" &&
              !(lastMsg.toolCalls && lastMsg.toolCalls.length > 0) &&
              !hasUserAfter
            );
            if (canAppend) {
              next[lastIdx] = {
                ...lastMsg!,
                content: lastMsg!.content + ((data.content as string) ?? ""),
              };
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
            const msg = data.message as Record<string, unknown> | undefined;
            const responseContent =
              (msg?.content as string) ?? (data.content as string) ?? "";
            const hasToolCalls = !!(
              msg?.tool_calls &&
              Array.isArray(msg.tool_calls) &&
              (msg.tool_calls as unknown[]).length > 0
            );

            // When llm.delta events already streamed the content, the
            // llm.response carries the same text — ignore it to avoid
            // duplication.  Only use responseContent when no deltas were
            // received (non-streaming providers).
            const useDeltaContent = hadDeltasRef.current;
            // Reset for next turn.
            hadDeltasRef.current = false;

            const prevAssistant = findLastAssistant(next);
            const prevHasTools = !!(
              prevAssistant?.toolCalls && prevAssistant.toolCalls.length > 0
            );
            const idx = findLastAssistantIndex(next);

            if (useDeltaContent && idx >= 0) {
              // Deltas already delivered content. Just update status
              // and move content to reasoning if tool calls follow.
              if (hasToolCalls) {
                next[idx] = {
                  ...next[idx],
                  reasoning:
                    (next[idx].reasoning ?? "") + next[idx].content,
                  content: "",
                  status: "streaming",
                };
              } else {
                next[idx] = {
                  ...next[idx],
                  status: "complete",
                };
              }
            } else if (prevHasTools || !prevAssistant) {
              // New turn (previous had tools or no previous message).
              next.push({
                id: `evt-${eventId}`,
                role: "assistant",
                content: hasToolCalls ? "" : responseContent,
                reasoning: hasToolCalls && responseContent
                  ? responseContent
                  : undefined,
                createdAt: new Date(),
                status: hasToolCalls ? "streaming" : "complete",
              });
            } else if (idx >= 0) {
              // Same turn, no deltas — use response content.
              if (hasToolCalls && responseContent) {
                next[idx] = {
                  ...next[idx],
                  reasoning:
                    (next[idx].reasoning ?? "") + responseContent,
                  status: "streaming",
                };
              } else {
                next[idx] = {
                  ...next[idx],
                  content: responseContent || next[idx].content,
                  status: hasToolCalls ? "streaming" : "complete",
                };
              }
            }

            if (!hasToolCalls) {
              setIsRunning(false);
            }

            // Update token usage from the LLM response — these are the
            // authoritative numbers from the worker's context compressor.
            const inputTk = (data.input_tokens as number) ?? 0;
            const outputTk = (data.output_tokens as number) ?? 0;
            const reasoningTk = (data.reasoning_tokens as number) ?? 0;
            const cacheTk = (data.cache_read_tokens as number) ?? 0;
            setTokenUsage({
              inputTokens: inputTk,
              outputTokens: outputTk,
              reasoningTokens: reasoningTk,
              cachedInputTokens: cacheTk,
              totalTokens: inputTk + outputTk,
              contextWindow: (data.context_window as number) ?? 0,
              model: (data.model as string) ?? "",
            });
            break;
          }

          case "llm.thinking": {
            const thinkIdx = findLastAssistantIndex(next);
            if (thinkIdx >= 0) {
              const prev = next[thinkIdx];
              next[thinkIdx] = {
                ...prev,
                reasoning:
                  (prev.reasoning ?? "") +
                  ((data.reasoning as string) ??
                    (data.content as string) ??
                    ""),
              };
            }
            setIsRunning(true);
            break;
          }

          case "tool.call": {
            let assistant = findLastAssistant(next);
            const assistantIdx = findLastAssistantIndex(next);
            const userAfterAssistant = assistantIdx >= 0 && next.slice(assistantIdx + 1).some(
              (m) => m.role === "user",
            );
            if (!assistant || assistant.status === "complete" || userAfterAssistant) {
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
            // Deduplicate — skip if this tool_call_id already exists.
            const tcId = (data.tool_call_id as string) ?? `tc-${eventId}`;
            if (!assistant.toolCalls.some((t) => t.id === tcId)) {
              const entry: ToolCallInfo = {
                id: tcId,
                toolName:
                  (data.name as string) ??
                  (data.tool_name as string) ??
                  "unknown",
                args:
                  typeof data.arguments === "string"
                    ? data.arguments
                    : JSON.stringify(data.arguments ?? {}),
                status: "running",
              };
              if (data.checkpoint_hash) {
                entry.checkpointHash = data.checkpoint_hash as string;
              }
              assistant.toolCalls.push(entry);
            }
            setIsRunning(true);
            break;
          }

          case "tool.result": {
            const assistant = findLastAssistant(next);
            const tc = assistant?.toolCalls?.find(
              (t) => t.id === (data.tool_call_id as string),
            );
            if (tc) {
              const resultContent =
                (data.content as string) ?? (data.result as string);
              tc.result =
                typeof resultContent === "string"
                  ? resultContent
                  : JSON.stringify(resultContent ?? null);
              tc.status = "complete";
            }
            break;
          }

          case "harness.wake":
          case "llm.request": {
            setIsRunning(true);
            break;
          }

          case "session.pause":
          case "session.complete":
          case "session.fail":
          case "session.done":
          case "harness.crash": {
            const doneIdx = findLastAssistantIndex(next);
            if (doneIdx >= 0 && next[doneIdx].status === "streaming") {
              // Mark any running tool calls as complete/error.
              const finalTools = next[doneIdx].toolCalls?.map((tc) =>
                tc.status === "running"
                  ? {
                      ...tc,
                      status: (type === "session.pause" ? "complete" : "error") as ToolCallInfo["status"],
                      result: tc.result ?? (type === "session.pause" ? "[interrupted]" : "[failed]"),
                    }
                  : tc,
              );
              next[doneIdx] = {
                ...next[doneIdx],
                toolCalls: finalTools,
                status:
                  type === "session.fail" || type === "harness.crash"
                    ? "error"
                    : "complete",
              };
            }
            setIsRunning(false);
            break;
          }

          case "stream.timeout":
            break;

          case "context.compact": {
            // Only clear the UI for explicit /clear commands.
            // /compress keeps the conversation visible — the compacted
            // messages are server-side only and the UI stays as-is.
            if (data.strategy === "clear") {
              next.length = 0;
              setTokenUsage(EMPTY_USAGE);
            }
            break;
          }

          case "policy.denied": {
            const assistant = findLastAssistant(next);
            if (assistant) {
              assistant.content += `\n\n**Policy denied**: ${(data.reason as string) ?? "Action blocked by governance policy."}`;
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
    esRef.current = es;

    for (const eventType of LISTENED_EVENTS) {
      es.addEventListener(eventType, (e: MessageEvent) => {
        // Ignore events from a stale EventSource (React StrictMode
        // cleanup may close the ES while queued events are still firing).
        if (esRef.current !== es) return;
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
      esRef.current = null;
      if (!sessionDoneRef.current) {
        setTimeout(() => {
          if (esRef.current === null) connectRef.current();
        }, 3000);
      }
    };
  }, [sessionId, applyEvent]);

  // Keep ref in sync with latest connect callback.
  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  /* eslint-disable react-hooks/set-state-in-effect -- intentional:
     we must reset messages/running state synchronously when sessionId
     changes; this is the standard pattern for external-store hooks. */
  useEffect(() => {
    if (!sessionId) {
      setMessages([]);
      setIsRunning(false);
      setTokenUsage(EMPTY_USAGE);
      return;
    }

    lastEventIdRef.current = 0;
    sessionDoneRef.current = false;
    hadDeltasRef.current = false;
    setMessages([]);
    setIsRunning(false);
    setTokenUsage(EMPTY_USAGE);
    connect();

    return () => {
      const es = esRef.current;
      esRef.current = null;
      es?.close();
    };
  }, [sessionId, connect]);
  /* eslint-enable react-hooks/set-state-in-effect */

  return { messages, isRunning, tokenUsage };
}

function findLastAssistant(msgs: ChatMessage[]): ChatMessage | undefined {
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === "assistant") return msgs[i];
  }
  return undefined;
}

function findLastAssistantIndex(msgs: ChatMessage[]): number {
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === "assistant") return i;
  }
  return -1;
}
