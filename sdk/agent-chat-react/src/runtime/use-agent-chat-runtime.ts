import { useCallback, useEffect, useRef, useState } from "react";
import type {
  AgentChatAdapter,
  AgentChatEventStream,
  AgentChatRuntimeApi,
  AgentChatState,
} from "../types";
import { AGENT_CHAT_LISTENED_EVENTS } from "./events";
import {
  applyAgentChatEvent,
  createInitialAgentChatState,
} from "./reducer";

export interface UseAgentChatRuntimeInput {
  adapter: AgentChatAdapter;
  agentId?: string;
  sessionId: string | null;
  onSessionChange?: (sessionId: string) => void;
}

export function useAgentChatRuntime({
  adapter,
  agentId,
  sessionId,
  onSessionChange,
}: UseAgentChatRuntimeInput): AgentChatRuntimeApi {
  const [state, setState] = useState<AgentChatState>(() =>
    createInitialAgentChatState({ isLoadingHistory: Boolean(sessionId) }),
  );
  const stateRef = useRef(state);
  const streamRef = useRef<AgentChatEventStream | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const previousSessionIdRef = useRef<string | null>(sessionId);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const closeStream = useCallback(() => {
    const stream = streamRef.current;
    streamRef.current = null;
    stream?.close();
  }, []);

  useEffect(() => {
    const previousSessionId = previousSessionIdRef.current;
    previousSessionIdRef.current = sessionId;
    clearReconnectTimer();
    closeStream();

    if (!sessionId) {
      setState(createInitialAgentChatState());
      return;
    }

    let cancelled = false;

    const connect = (after?: number) => {
      if (cancelled) return;
      const stream = adapter.openEventStream({
        sessionId,
        after: after ?? stateRef.current.lastEventId,
      });
      streamRef.current = stream;

      for (const eventType of AGENT_CHAT_LISTENED_EVENTS) {
        stream.addEventListener(eventType, (messageEvent) => {
          if (streamRef.current !== stream) return;
          const data = parseEventData(messageEvent.data);
          const eventId = messageEvent.lastEventId
            ? Number(messageEvent.lastEventId)
            : 0;
          setState((prev) =>
            applyAgentChatEvent(prev, {
              type: eventType,
              eventId,
              data,
            }),
          );
        });
      }

      stream.onerror = () => {
        stream.close();
        if (streamRef.current === stream) {
          streamRef.current = null;
        }
        if (!stateRef.current.sessionDone && !cancelled) {
          reconnectTimerRef.current = setTimeout(() => connect(), 3000);
        }
      };
    };

    const currentState = stateRef.current;
    const preservePendingFirstMessage =
      previousSessionId === null &&
      currentState.isRunning &&
      currentState.messages.some(
        (message) =>
          message.role === "user" && message.id.startsWith("local-"),
      );
    const initialState = preservePendingFirstMessage
      ? {
          ...createInitialAgentChatState({ isLoadingHistory: false }),
          messages: currentState.messages,
          isRunning: true,
        }
      : createInitialAgentChatState({
          isLoadingHistory: true,
        });
    stateRef.current = initialState;
    setState(initialState);
    connect(0);

    adapter
      .getSession({ sessionId })
      .then((session) => {
        if (cancelled) return;
        if (session.messageCount === 0) {
          setState((prev) => ({
            ...prev,
            isLoadingHistory: false,
          }));
        }
        if (isTerminalStatus(session.status)) {
          setState((prev) => ({
            ...prev,
            terminal: true,
            isRunning: false,
          }));
        }
      })
      .catch(() => undefined);

    return () => {
      cancelled = true;
      clearReconnectTimer();
      closeStream();
    };
  }, [adapter, clearReconnectTimer, closeStream, sessionId]);

  const markSending = useCallback((content: string) => {
    setState((prev) => ({
      ...prev,
      terminal: false,
      isRunning: true,
      messages: [
        ...prev.messages,
        {
          id: `local-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          role: "user",
          content,
          createdAt: new Date(),
          status: "complete",
        },
      ],
    }));
  }, []);

  const markSendError = useCallback((errorText: string) => {
    setState((prev) => {
      for (let i = prev.messages.length - 1; i >= 0; i--) {
        const message = prev.messages[i];
        if (message?.role === "user" && message.id.startsWith("local-")) {
          const messages = [...prev.messages];
          messages[i] = {
            ...message,
            status: "error",
            content: `${message.content}\n\n*Failed to send: ${errorText}*`,
          };
          return {
            ...prev,
            isRunning: false,
            messages,
          };
        }
      }
      return { ...prev, isRunning: false };
    });
  }, []);

  const forceStop = useCallback(() => {
    setState((prev) => {
      const messages = [...prev.messages];
      const idx = findLastAssistantIndex(messages);
      if (idx >= 0 && messages[idx]?.status === "streaming") {
        const message = messages[idx]!;
        messages[idx] = {
          ...message,
          status: "complete",
          toolCalls: message.toolCalls?.map((toolCall) =>
            toolCall.status === "running"
              ? {
                  ...toolCall,
                  status: "complete",
                  result: toolCall.result ?? "[interrupted]",
                }
              : toolCall,
          ),
        };
      }
      return {
        ...prev,
        terminal: true,
        isRunning: false,
        messages,
      };
    });
  }, []);

  const send = useCallback(
    async (content: string) => {
      markSending(content);

      if (!sessionId) {
        try {
          const session = await adapter.createSession({ agentId });
          onSessionChange?.(session.id);
          await adapter.sendMessage({ sessionId: session.id, content });
        } catch (error) {
          markSendError(error instanceof Error ? error.message : "send failed");
          throw error;
        }
        return;
      }

      try {
        await adapter.sendMessage({ sessionId, content });
      } catch (error) {
        markSendError(error instanceof Error ? error.message : "send failed");
        throw error;
      }
    },
    [adapter, agentId, markSendError, markSending, onSessionChange, sessionId],
  );

  const stop = useCallback(async () => {
    if (!sessionId) return;
    forceStop();
    await adapter.pauseSession({ sessionId });
  }, [adapter, forceStop, sessionId]);

  const retry = useCallback(async () => {
    if (!sessionId) return;
    setState((prev) => ({
      ...prev,
      terminal: false,
      retryIndicator: null,
      isRunning: true,
    }));
    try {
      await adapter.retrySession({ sessionId });
    } catch (error) {
      setState((prev) => ({
        ...prev,
        terminal: true,
        isRunning: false,
      }));
      throw error;
    }
  }, [adapter, sessionId]);

  return {
    messages: state.messages,
    isRunning: state.isRunning,
    isLoadingHistory: state.isLoadingHistory,
    tokenUsage: state.tokenUsage,
    retryIndicator: state.retryIndicator,
    send,
    stop,
    retry,
    markSending,
    markSendError,
  };
}

function parseEventData(data: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(data) as unknown;
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function isTerminalStatus(status: string): boolean {
  return status === "paused" || status === "completed" || status === "failed";
}

function findLastAssistantIndex(messages: AgentChatState["messages"]): number {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i]?.role === "assistant") return i;
  }
  return -1;
}
