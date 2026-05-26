import { useCallback, useEffect, useRef, useState } from "react";
import type {
  AgentChatAdapter,
  AgentChatAttachment,
  AgentChatDisplayAttachment,
  AgentChatEventStream,
  AgentChatImageAttachment,
  AgentChatPendingAttachment,
  AgentChatRuntimeApi,
  AgentChatSession,
  AgentChatState,
} from "../types";
import { AGENT_CHAT_LISTENED_EVENTS } from "./events";
import {
  applyAgentChatEvent,
  createInitialAgentChatState,
} from "./reducer";

const VIEW_MODE_KEY = "@invergent/agent-chat-react:viewMode";
const VIEW_MODE_EVENT = "@invergent/agent-chat-react:viewMode:change";

function readPersistedViewMode(): "simple" | "expert" | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(VIEW_MODE_KEY);
    if (raw === "simple" || raw === "expert") return raw;
  } catch {
    // localStorage can throw in restricted contexts (Safari private
    // mode, sandboxed iframes). Treat as "no preference yet".
  }
  return null;
}

function writePersistedViewMode(mode: "simple" | "expert"): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(VIEW_MODE_KEY, mode);
  } catch {
    // Non-fatal: the adapter call still persists for next session.
  }
  // Same-tab broadcast: the browser's "storage" event only fires in
  // *other* tabs, so sibling components (e.g. the sidebar) need a
  // separate signal to re-read the value.
  try {
    window.dispatchEvent(
      new CustomEvent(VIEW_MODE_EVENT, { detail: mode }),
    );
  } catch {
    // CustomEvent constructor missing in very old runtimes; ignore.
  }
}

export function useChatViewMode(): "simple" | "expert" {
  const [mode, setMode] = useState<"simple" | "expert">(
    () => readPersistedViewMode() ?? "simple",
  );
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onSameTab = (event: Event) => {
      const detail = (event as CustomEvent<unknown>).detail;
      if (detail === "simple" || detail === "expert") setMode(detail);
    };
    const onCrossTab = (event: StorageEvent) => {
      if (event.key !== VIEW_MODE_KEY) return;
      const next = event.newValue;
      if (next === "simple" || next === "expert") setMode(next);
    };
    window.addEventListener(VIEW_MODE_EVENT, onSameTab);
    window.addEventListener("storage", onCrossTab);
    return () => {
      window.removeEventListener(VIEW_MODE_EVENT, onSameTab);
      window.removeEventListener("storage", onCrossTab);
    };
  }, []);
  return mode;
}

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
    createInitialAgentChatState({
      isLoadingHistory: Boolean(sessionId),
      // Seed from localStorage so the first paint matches the user's
      // last choice without flashing the default. The async adapter
      // load can still upgrade this later.
      viewMode: readPersistedViewMode() ?? "simple",
    }),
  );
  const [session, setSession] = useState<AgentChatSession | null>(null);
  const stateRef = useRef(state);
  const streamRef = useRef<AgentChatEventStream | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const previousSessionIdRef = useRef<string | null>(sessionId);
  const sessionIdRef = useRef<string | null>(sessionId);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

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

  const openStream = useCallback(
    (targetSessionId: string, after?: number) => {
      const stream = adapter.openEventStream({
        sessionId: targetSessionId,
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
        if (
          !stateRef.current.sessionDone &&
          sessionIdRef.current === targetSessionId
        ) {
          reconnectTimerRef.current = setTimeout(
            () => openStream(targetSessionId),
            3000,
          );
        }
      };
    },
    [adapter],
  );

  const ensureStreamForNewTurn = useCallback(
    (targetSessionId: string) => {
      if (streamRef.current && !stateRef.current.sessionDone) return;

      const after = stateRef.current.lastEventId;
      clearReconnectTimer();
      closeStream();
      stateRef.current = {
        ...stateRef.current,
        sessionDone: false,
        terminal: false,
      };
      openStream(targetSessionId, after);
    },
    [clearReconnectTimer, closeStream, openStream],
  );

  useEffect(() => {
    const previousSessionId = previousSessionIdRef.current;
    previousSessionIdRef.current = sessionId;
    clearReconnectTimer();
    closeStream();

    if (!sessionId) {
      setSession(null);
      setState(createInitialAgentChatState());
      return;
    }

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
    setSession(null);
    setState(initialState);
    openStream(sessionId, 0);

    adapter
      .getSession({ sessionId })
      .then((loadedSession) => {
        if (sessionIdRef.current !== sessionId) return;
        setSession(loadedSession);
        if (loadedSession.messageCount === 0) {
          setState((prev) => ({
            ...prev,
            isLoadingHistory: false,
          }));
        }
        if (isTerminalStatus(loadedSession.status)) {
          setState((prev) => ({
            ...prev,
            terminal: true,
            isRunning: false,
          }));
        }
      })
      .catch(() => undefined);

    // Replayed events alone aren't enough to know whether the browser is
    // currently alive. The server emits `browser.provisioned` but doesn't
    // always emit a matching `destroyed` when the browser is reaped, so
    // session history can show a "live" browser that no longer exists.
    // Ask the live API for the actual current state when the session opens
    // and use that as the source of truth (a 404 collapses to null and
    // hides the pane).
    void adapter
      .getBrowserState(sessionId)
      .then((browserState) => {
        if (sessionIdRef.current !== sessionId) return;
        setState((prev) => ({
          ...prev,
          browser: browserState
            ? {
                status: browserState.status,
                controlOwner: browserState.controlOwner ?? null,
              }
            : null,
        }));
      })
      .catch(() => undefined);

    return () => {
      clearReconnectTimer();
      closeStream();
    };
  }, [adapter, clearReconnectTimer, closeStream, openStream, sessionId]);

  const markSending = useCallback(
    (
      content: string,
      images?: AgentChatImageAttachment[],
      attachments?: AgentChatDisplayAttachment[],
    ) => {
      setState((prev) => ({
        ...prev,
        terminal: false,
        sessionDone: false,
        isRunning: true,
        messages: [
          ...prev.messages,
          {
            id: `local-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
            role: "user",
            content,
            createdAt: new Date(),
            status: "complete",
            images: images?.length ? images : undefined,
            attachments: attachments?.length ? attachments : undefined,
          },
        ],
      }));
    },
    [],
  );

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
    async (
      content: string,
      images?: AgentChatImageAttachment[],
      pendingAttachments?: AgentChatPendingAttachment[],
    ) => {
      // Display-only mirror of pendingAttachments for the optimistic
      // local user-message: same filename + size, no path (chip renders
      // disabled).  The reducer reconciles these with the persisted
      // refs when the user.message event arrives.
      const displayAttachments: AgentChatDisplayAttachment[] | undefined =
        pendingAttachments?.length
          ? pendingAttachments.map((a) => ({
              filename: a.filename,
              mimeType: a.mimeType,
              size: a.size ?? a.file.size,
              // path absent until upload completes.
            }))
          : undefined;

      markSending(content, images, displayAttachments);
      // Slash-command parsing is suppressed when the user is sending
      // image vision OR file attachments — neither shape participates
      // in /goal control flow.
      const goal =
        images?.length || pendingAttachments?.length
          ? null
          : parseGoalDefinition(content);

      const runUploadsAndSend = async (sid: string): Promise<void> => {
        let refs: AgentChatAttachment[] | undefined;
        if (pendingAttachments?.length) {
          // Same epoch timestamp shared across this batch so all files
          // from this turn cluster together in the workspace listing.
          const stamp = Date.now();
          refs = await Promise.all(
            pendingAttachments.map(async (a, index) => {
              const safeName = sanitizeUploadFilename(a.file.name);
              const renamed = new File(
                [a.file],
                `${stamp}-${index}-${safeName}`,
                { type: a.file.type },
              );
              const up = await adapter.uploadWorkspaceFile({
                sessionId: sid,
                file: renamed,
                directory: "uploads",
              });
              return {
                path: up.path,
                filename: a.filename,
                mimeType: a.mimeType ?? a.file.type ?? undefined,
                size: up.size,
              } satisfies AgentChatAttachment;
            }),
          );
        }
        await sendTurn(adapter, sid, content, images, refs, goal);
      };

      if (!sessionId) {
        try {
          const session = await adapter.createSession({ agentId });
          onSessionChange?.(session.id);
          await runUploadsAndSend(session.id);
        } catch (error) {
          markSendError(error instanceof Error ? error.message : "send failed");
          throw error;
        }
        return;
      }

      try {
        ensureStreamForNewTurn(sessionId);
        await runUploadsAndSend(sessionId);
      } catch (error) {
        markSendError(error instanceof Error ? error.message : "send failed");
        throw error;
      }
    },
    [
      adapter,
      agentId,
      ensureStreamForNewTurn,
      markSendError,
      markSending,
      onSessionChange,
      sessionId,
    ],
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

  // viewMode (Simple/Expert) persistence: prefer the adapter's
  // getChatViewMode / setChatViewMode when implemented, fall back to
  // localStorage otherwise. The initial hydration runs once per
  // adapter; setViewMode writes through to both stores so a later
  // reload picks up the change even if the adapter call is in flight.
  useEffect(() => {
    if (!adapter.getChatViewMode) {
      const cached = readPersistedViewMode();
      if (cached !== null) {
        setState((prev) =>
          prev.viewMode === cached ? prev : { ...prev, viewMode: cached },
        );
      }
      return;
    }
    let cancelled = false;
    void adapter
      .getChatViewMode()
      .then((persisted) => {
        if (cancelled) return;
        if (persisted === "simple" || persisted === "expert") {
          setState((prev) =>
            prev.viewMode === persisted
              ? prev
              : { ...prev, viewMode: persisted },
          );
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [adapter]);

  const setViewMode = useCallback(
    (mode: "simple" | "expert") => {
      setState((prev) =>
        prev.viewMode === mode ? prev : { ...prev, viewMode: mode },
      );
      writePersistedViewMode(mode);
      if (adapter.setChatViewMode) {
        void adapter.setChatViewMode(mode).catch(() => undefined);
      }
    },
    [adapter],
  );

  return {
    state,
    session,
    messages: state.messages,
    isRunning: state.isRunning,
    terminal: state.terminal,
    isLoadingHistory: state.isLoadingHistory,
    tokenUsage: state.tokenUsage,
    retryIndicator: state.retryIndicator,
    workspaceRefreshKey: state.workspaceRefreshKey,
    send,
    stop,
    retry,
    markSending,
    markSendError,
    viewMode: state.viewMode,
    setViewMode,
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

type ParsedGoalDefinition = {
  description: string;
  rubric?: string;
};

const GOAL_RUBRIC_RE = /\n\s*(?:rubric|criteria)\s*:\s*\n/i;
const GOAL_CONTROL_COMMANDS = new Set(["", "status", "pause", "resume", "clear"]);

async function sendTurn(
  adapter: AgentChatAdapter,
  sessionId: string,
  content: string,
  images: AgentChatImageAttachment[] | undefined,
  attachments: AgentChatAttachment[] | undefined,
  goal: ParsedGoalDefinition | null,
): Promise<void> {
  if (goal && adapter.defineOutcome) {
    await adapter.defineOutcome({
      sessionId,
      description: goal.description,
      rubric: goal.rubric,
    });
    return;
  }
  await adapter.sendMessage({ sessionId, content, images, attachments });
}

/**
 * Strip characters the harness or storage backend will reject from an
 * upload filename: path separators, NUL.  When the resulting string is
 * empty (e.g. the original name was made up entirely of stripped
 * characters), fall back to a generic placeholder so the workspace key
 * stays well-formed.  The harness re-validates the path on its end.
 */
function sanitizeUploadFilename(name: string): string {
  const stripped = name.replace(/[\/\\\0]/g, "").trim();
  return stripped.length > 0 ? stripped : "attachment";
}

function parseGoalDefinition(content: string): ParsedGoalDefinition | null {
  const text = content.trim();
  if (text !== "/goal" && !text.startsWith("/goal ")) return null;

  const args = text.slice("/goal".length).trim();
  if (GOAL_CONTROL_COMMANDS.has(args.toLowerCase())) return null;

  const match = GOAL_RUBRIC_RE.exec(args);
  const description = (match ? args.slice(0, match.index) : args).trim();
  if (!description) return null;

  const rubric = match ? args.slice(match.index + match[0].length).trim() : "";
  return rubric ? { description, rubric } : { description };
}
