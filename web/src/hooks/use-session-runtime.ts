// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Bridges the Surogates SSE event stream (/v1/sessions/{id}/events)
// into ChatMessage objects consumed by the assistant-ui runtime adapter.

import { useEffect, useRef, useState, useCallback } from "react";
import { getAuthToken } from "@/features/auth";
import { getSession } from "@/api/sessions";

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
  // Populated for consult_expert tool calls when the expert.result
  // event lands.  The id references the expert.result event and is
  // used to submit thumbs-up/thumbs-down feedback.
  expertResultEventId?: number;
  // Current user feedback on this expert's output.  Set when an
  // EXPERT_ENDORSE or EXPERT_OVERRIDE event arrives (either live or
  // on session replay).
  expertFeedback?: { rating: "up" | "down"; reason?: string };
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
  "expert.result",
  "expert.endorse",
  "expert.override",
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
  // When true, the session is in a terminal state (paused/completed/failed)
  // per the DB.  Prevents replayed SSE events from flipping isRunning back.
  const terminalRef = useRef<boolean>(false);

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
            const deltaContent = (data.content as string) ?? "";
            const deltaReasoning = (data.reasoning as string) ?? "";
            if (deltaContent) hadDeltasRef.current = true;

            const lastIdx = findLastAssistantIndex(next);
            const lastMsg = lastIdx >= 0 ? next[lastIdx] : null;
            const hasUserAfter = lastIdx >= 0 && hasUserAfterIndex(next, lastIdx);
            const allToolsDoneDelta = lastMsg?.toolCalls?.length &&
              lastMsg.toolCalls.every((tc) => tc.status !== "running");
            // Append to the current message only if it's streaming,
            // doesn't already have completed tool calls, AND there's no
            // user message after it (which means a new turn started).
            const canAppend = !!(
              lastMsg &&
              lastMsg.status === "streaming" &&
              !allToolsDoneDelta &&
              !hasUserAfter
            );
            if (canAppend) {
              next[lastIdx] = {
                ...lastMsg!,
                content: deltaContent
                  ? lastMsg!.content + deltaContent
                  : lastMsg!.content,
                reasoning: deltaReasoning
                  ? (lastMsg!.reasoning ?? "") + deltaReasoning
                  : lastMsg!.reasoning,
              };
            } else {
              next.push({
                id: `evt-${eventId}`,
                role: "assistant",
                content: deltaContent,
                reasoning: deltaReasoning || undefined,
                createdAt: new Date(),
                status: "streaming",
              });
            }
            if (!terminalRef.current) setIsRunning(true);
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

            const idx = findLastAssistantIndex(next);
            const prevAssistant = idx >= 0 ? next[idx] : undefined;
            const prevHasTools = !!(
              prevAssistant?.toolCalls && prevAssistant.toolCalls.length > 0
            );

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
            const reasoningText =
              (data.reasoning as string) ?? (data.content as string) ?? "";
            const thinkIdx = findLastAssistantIndex(next);
            const prev = thinkIdx >= 0 ? next[thinkIdx] : null;

            // If the last assistant message already has completed tool
            // calls, this thinking belongs to a NEW LLM iteration (the
            // model is reasoning about tool results).  Start a fresh
            // assistant message so the reasoning appears after the tools,
            // not inside the previous message.
            const allToolsDone = prev?.toolCalls?.length &&
              prev.toolCalls.every((tc) => tc.status !== "running");
            const hasUserAfter = thinkIdx >= 0 && hasUserAfterIndex(next, thinkIdx);

            if (!prev || allToolsDone || hasUserAfter) {
              next.push({
                id: `evt-${eventId}`,
                role: "assistant",
                content: "",
                reasoning: reasoningText,
                createdAt: new Date(),
                status: "streaming",
              });
            } else {
              next[thinkIdx] = {
                ...prev,
                reasoning: (prev.reasoning ?? "") + reasoningText,
              };
            }
            if (!terminalRef.current) setIsRunning(true);
            break;
          }

          case "tool.call": {
            const tcIdx = findLastAssistantIndex(next);
            let tcAssistant = tcIdx >= 0 ? next[tcIdx] : null;
            const userAfterAssistant = tcIdx >= 0 && hasUserAfterIndex(next, tcIdx);
            if (!tcAssistant || tcAssistant.status === "complete" || userAfterAssistant) {
              tcAssistant = {
                id: `evt-${eventId}-tc`,
                role: "assistant",
                content: "",
                createdAt: new Date(),
                status: "streaming",
              };
              next.push(tcAssistant);
            } else {
              // Create a new reference so React detects the change.
              tcAssistant = { ...tcAssistant };
              next[tcIdx] = tcAssistant;
            }
            const existingCalls = tcAssistant.toolCalls ?? [];
            // Deduplicate — skip if this tool_call_id already exists.
            const tcId = (data.tool_call_id as string) ?? `tc-${eventId}`;
            if (!existingCalls.some((t) => t.id === tcId)) {
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
              tcAssistant.toolCalls = [...existingCalls, entry];
            }
            if (!terminalRef.current) setIsRunning(true);
            break;
          }

          case "tool.result": {
            const targetId = data.tool_call_id as string;
            // Search backwards — the tool call may not be on the last
            // assistant message (streaming tool executor emits tool.call
            // before llm.response, which can create a new message).
            let matchIdx = -1;
            for (let i = next.length - 1; i >= 0; i--) {
              if (
                next[i].role === "assistant" &&
                next[i].toolCalls?.some((t) => t.id === targetId)
              ) {
                matchIdx = i;
                break;
              }
            }
            if (matchIdx >= 0) {
              const matchMsg = next[matchIdx];
              const resultContent =
                (data.content as string) ?? (data.result as string);
              const formattedResult =
                typeof resultContent === "string"
                  ? resultContent
                  : JSON.stringify(resultContent ?? null);
              next[matchIdx] = {
                ...matchMsg,
                toolCalls: matchMsg.toolCalls!.map((t) =>
                  t.id === targetId
                    ? { ...t, result: formattedResult, status: "complete" as const }
                    : t,
                ),
              };
            }
            break;
          }

          case "harness.wake":
          case "llm.request": {
            if (!terminalRef.current) {
              setIsRunning(true);
            }
            break;
          }

          case "harness.crash": {
            // Crash is NOT terminal — the orchestrator retries (up to 3x).
            // Keep isRunning=true so the shimmer stays visible during retries.
            // Only session.fail (after retries exhausted) is the terminal signal.
            break;
          }

          case "session.resume": {
            terminalRef.current = false;
            setIsRunning(true);
            break;
          }

          case "session.pause":
          case "session.complete":
          case "session.fail":
          case "session.done": {
            terminalRef.current = true;
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
                status: type === "session.fail" ? "error" : "complete",
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

          case "expert.result": {
            // Attach this expert.result event id to the most recent
            // consult_expert tool call so the UI can later submit
            // feedback keyed to this specific event.
            const tcMatch = findLatestConsultExpertCall(next);
            if (tcMatch) {
              const { msgIdx, toolId } = tcMatch;
              const msg = next[msgIdx];
              next[msgIdx] = {
                ...msg,
                toolCalls: msg.toolCalls?.map((t) =>
                  t.id === toolId
                    ? { ...t, expertResultEventId: eventId }
                    : t,
                ),
              };
            }
            break;
          }

          case "expert.endorse":
          case "expert.override": {
            // Apply prior feedback when replaying a session, or reflect
            // the caller's own click once the server has persisted it.
            const resultEventId = data.expert_result_event_id as
              | number
              | undefined;
            if (resultEventId == null) break;
            const rating: "up" | "down" = type === "expert.endorse" ? "up" : "down";
            const reason = (data.reason as string | undefined) ?? undefined;
            for (let i = next.length - 1; i >= 0; i--) {
              const msg = next[i];
              if (!msg.toolCalls) continue;
              const hit = msg.toolCalls.find(
                (t) => t.expertResultEventId === resultEventId,
              );
              if (!hit) continue;
              if (
                hit.expertFeedback?.rating === rating &&
                hit.expertFeedback?.reason === reason
              ) break;
              next[i] = {
                ...msg,
                toolCalls: msg.toolCalls.map((t) =>
                  t.expertResultEventId === resultEventId
                    ? { ...t, expertFeedback: { rating, reason } }
                    : t,
                ),
              };
              break;
            }
            break;
          }

          case "policy.denied": {
            const policyIdx = findLastAssistantIndex(next);
            if (policyIdx >= 0) {
              const policyMsg = next[policyIdx];
              next[policyIdx] = {
                ...policyMsg,
                content: policyMsg.content + `\n\n**Policy denied**: ${(data.reason as string) ?? "Action blocked by governance policy."}`,
                status: "error",
              };
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
    terminalRef.current = false;
    setMessages([]);
    setIsRunning(false);
    setTokenUsage(EMPTY_USAGE);
    connect();

    // Fetch session status to set terminalRef.  If the session is
    // already paused/completed/failed, this prevents replayed SSE
    // events from flipping isRunning back to true.
    let cancelled = false;
    getSession(sessionId)
      .then((session) => {
        if (cancelled) return;
        const status = session.status as string;
        if (status === "paused" || status === "completed" || status === "failed") {
          terminalRef.current = true;
          setIsRunning(false);
        }
      })
      .catch(() => {});

    return () => {
      cancelled = true;
      const es = esRef.current;
      esRef.current = null;
      es?.close();
    };
  }, [sessionId, connect]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const forceStop = useCallback(() => {
    terminalRef.current = true;
    setIsRunning(false);
    // Mark the last streaming assistant message as complete so the UI
    // doesn't show a dangling shimmer.
    setMessages((prev) => {
      const idx = findLastAssistantIndex(prev);
      if (idx < 0 || prev[idx].status !== "streaming") return prev;
      const next = [...prev];
      const msg = prev[idx];
      next[idx] = {
        ...msg,
        status: "complete",
        toolCalls: msg.toolCalls?.map((tc) =>
          tc.status === "running"
            ? { ...tc, status: "complete" as const, result: tc.result ?? "[interrupted]" }
            : tc,
        ),
      };
      return next;
    });
  }, []);

  return { messages, isRunning, tokenUsage, forceStop };
}

function findLastAssistantIndex(msgs: ChatMessage[]): number {
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === "assistant") return i;
  }
  return -1;
}

function hasUserAfterIndex(msgs: ChatMessage[], idx: number): boolean {
  for (let i = idx + 1; i < msgs.length; i++) {
    if (msgs[i].role === "user") return true;
  }
  return false;
}

// Locate the most recent consult_expert tool call across all assistant
// messages.  Used to attach an expert.result event id to the tool call
// that triggered it (expert.result is emitted inside the expert's
// mini-loop, before the consult_expert tool.result arrives).
function findLatestConsultExpertCall(
  msgs: ChatMessage[],
): { msgIdx: number; toolId: string } | null {
  for (let i = msgs.length - 1; i >= 0; i--) {
    const msg = msgs[i];
    if (!msg.toolCalls) continue;
    for (let j = msg.toolCalls.length - 1; j >= 0; j--) {
      const tc = msg.toolCalls[j];
      if (
        tc.toolName === "consult_expert" &&
        tc.expertResultEventId === undefined
      ) {
        return { msgIdx: i, toolId: tc.id };
      }
    }
  }
  return null;
}
