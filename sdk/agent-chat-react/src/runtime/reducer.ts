import type {
  AgentChatBrowserState,
  AgentChatDisplayAttachment,
  AgentChatErrorInfo,
  AgentChatImageAttachment,
  AgentChatMessage,
  AgentChatRuntimeEvent,
  AgentChatState,
  AgentChatTokenUsage,
  AgentChatToolCallInfo,
  AgentChatTurnArtifactRef,
  AgentChatViewMode,
} from "../types";
import { WORKSPACE_MUTATING_TOOLS } from "./events";

export const EMPTY_TOKEN_USAGE: AgentChatTokenUsage = {
  inputTokens: 0,
  outputTokens: 0,
  reasoningTokens: 0,
  cachedInputTokens: 0,
  totalTokens: 0,
  contextWindow: 0,
  model: "",
};

export function createInitialAgentChatState(
  options: {
    isLoadingHistory?: boolean;
    viewMode?: AgentChatViewMode;
  } = {},
): AgentChatState {
  return {
    messages: [],
    isRunning: false,
    isLoadingHistory: options.isLoadingHistory ?? false,
    tokenUsage: EMPTY_TOKEN_USAGE,
    retryIndicator: null,
    lastEventId: 0,
    sessionDone: false,
    hadDeltas: false,
    terminal: false,
    workspaceRefreshKey: 0,
    browser: null,
    viewMode: options.viewMode ?? "simple",
    researchSources: [],
  };
}

export function applyAgentChatEvent(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  let nextState: AgentChatState = {
    ...state,
    isLoadingHistory: false,
    lastEventId: Math.max(state.lastEventId, event.eventId),
    sessionDone: state.sessionDone || event.type === "session.done",
  };

  nextState = applyRetryIndicator(nextState, event);

  if (state.terminal && isPostTerminalLiveEvent(event.type)) {
    return nextState;
  }

  switch (event.type) {
    case "user.message":
      // A new user message means the session is about to run a new turn.
      // Clear ``terminal`` so any out-of-order ``session.pause`` that
      // landed between the ``/messages`` route's RESUME and USER_MESSAGE
      // emits (e.g. the harness's redundant abort-cleanup pause) doesn't
      // suppress the running indicator for the new turn's deltas.
      return withMessages(
        { ...nextState, terminal: false, isRunning: true },
        applyUserMessage(nextState.messages, event.eventId, event.data),
      );

    case "skill.invoked":
      return withMessages(nextState, [
        ...nextState.messages,
        {
          id: `evt-${event.eventId}`,
          role: "system",
          content: stringValue(event.data.skill),
          createdAt: new Date(),
          status: "complete",
          systemKind: "skill_invoked",
          systemMeta: {
            skill: event.data.skill,
            staged_at: event.data.staged_at,
          },
        },
      ]);

    case "artifact.created":
    case "artifact.updated":
      return withMessages(nextState, [
        ...nextState.messages,
        {
          id: `evt-${event.eventId}`,
          role: "system",
          content: stringValue(event.data.name),
          createdAt: new Date(),
          status: "complete",
          systemKind: "artifact",
          systemMeta: {
            artifact_id: event.data.artifact_id,
            name: event.data.name,
            kind: event.data.kind,
            version: event.data.version,
            size: event.data.size,
          },
        },
      ]);

    case "browser.provisioned":
      return applyBrowserEvent(nextState, event, {
        status: "live",
        controlOwner: null,
      });

    case "browser.control_granted":
      return applyBrowserEvent(nextState, event, {
        status: "user-control",
        controlOwner: stringValue(event.data.owner_user_id) || null,
      });

    case "browser.control_returned":
      return applyBrowserEvent(nextState, event, {
        status: "live",
        controlOwner: null,
      });

    case "browser.destroyed":
      return applyBrowserEvent(nextState, event, null);

    case "llm.delta":
      return applyLlmDelta(nextState, event);

    case "llm.response":
      return applyLlmResponse(nextState, event);

    case "llm.thinking":
      return applyLlmThinking(nextState, event);

    case "tool.call":
      return applyToolCall(nextState, event);

    case "tool.result": {
      const toolCallId = stringValue(event.data.tool_call_id);
      const toolName = findToolNameById(nextState.messages, toolCallId);
      const messages = applyToolResult(nextState.messages, event.data);
      const mutatesWorkspace =
        toolName !== null && WORKSPACE_MUTATING_TOOLS.has(toolName);
      const withResult: AgentChatState = {
        ...nextState,
        messages,
        workspaceRefreshKey: mutatesWorkspace
          ? nextState.workspaceRefreshKey + 1
          : nextState.workspaceRefreshKey,
      };
      // research_memory(add) is the only event that surfaces a new
      // source for the citations/sources panel.  Other research tool
      // calls (retrieve, list, set, get) pass through unchanged.
      return collectResearchSource(withResult, toolName, event.data);
    }

    case "harness.wake":
    case "llm.request":
      // Sign-of-life events: clear ``terminal`` too. A late
      // ``session.pause`` from the harness's abort cleanup can land
      // after the ``/messages`` route's RESUME + USER_MESSAGE; without
      // re-clearing ``terminal`` here every subsequent gated event
      // (deltas, thinking, tool calls) would leave ``isRunning`` false.
      return { ...nextState, terminal: false, isRunning: true };

    case "harness.crash":
    case "stream.timeout":
      return nextState;

    case "session.resume":
      return {
        ...nextState,
        terminal: false,
        isRunning: true,
      };

    case "session.pause":
    case "session.complete":
    case "session.fail":
    case "session.done":
      return applyTerminalEvent(nextState, event);

    case "context.compact":
      if (event.data.strategy === "clear") {
        return {
          ...nextState,
          messages: [],
          tokenUsage: EMPTY_TOKEN_USAGE,
        };
      }
      return nextState;

    case "expert.delegation":
      return applyExpertDelegation(nextState, event);

    case "expert.result":
      return withMessages(
        nextState,
        applyExpertResult(nextState.messages, event.eventId, event.data),
      );

    case "expert.failure":
      return withMessages(
        nextState,
        applyExpertFailure(nextState.messages, event.eventId, event.data),
      );

    case "expert.endorse":
    case "expert.override":
      return withMessages(
        nextState,
        applyExpertFeedback(nextState.messages, event.type, event.data),
      );

    case "user.feedback":
      return withMessages(
        nextState,
        applyUserFeedback(nextState.messages, event.data),
      );

    case "ask_user_question.response":
      return withMessages(
        nextState,
        applyAskUserQuestionResponse(nextState.messages, event.data),
      );

    case "policy.denied":
      return applyPolicyDenied(nextState, event.data);

    case "session.start":
      return nextState;

    case "iteration.summary":
      return applyIterationSummary(nextState, event);

    case "turn.summary":
      return applyTurnSummary(nextState, event);
  }
}

function isPostTerminalLiveEvent(type: AgentChatRuntimeEvent["type"]): boolean {
  return (
    type === "harness.wake" ||
    type === "llm.request" ||
    type === "session.resume" ||
    type === "llm.delta" ||
    type === "llm.response" ||
    type === "llm.thinking" ||
    type === "tool.call"
  );
}

function applyRetryIndicator(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  switch (event.type) {
    case "harness.wake":
    case "llm.request":
    case "llm.response":
    case "session.resume":
    case "session.pause":
    case "session.complete":
    case "session.fail":
    case "session.done":
      return { ...state, retryIndicator: null };
    case "harness.crash": {
      const title = stringValue(event.data.error_title) ||
        "A transient error occurred, retrying...";
      const detail = stringValue(event.data.error_detail) ||
        stringValue(event.data.error);
      return {
        ...state,
        retryIndicator: {
          title,
          detail,
          attempt: (state.retryIndicator?.attempt ?? 0) + 1,
        },
      };
    }
    default:
      return state;
  }
}

function applyUserMessage(
  messages: AgentChatMessage[],
  eventId: number,
  data: Record<string, unknown>,
): AgentChatMessage[] {
  const content = stringValue(data.content);
  const images = parseUserMessageImages(data.images);
  const attachments = parseUserMessageAttachments(data.attachments);
  const next = [...messages];
  const localIdx = next.findIndex(
    (m) =>
      m.role === "user" &&
      m.id.startsWith("local-") &&
      m.content === content,
  );
  if (localIdx >= 0) {
    const local = next[localIdx]!;
    next[localIdx] = {
      ...local,
      id: `evt-${eventId}`,
      // Server-confirmed lists replace any optimistic display-only
      // entries: chips that were rendered while the upload was in
      // flight become clickable because the persisted refs include a
      // workspace path.
      images: images ?? local.images,
      attachments: attachments ?? local.attachments,
    };
    return next;
  }
  next.push({
    id: `evt-${eventId}`,
    role: "user",
    content,
    createdAt: new Date(),
    status: "complete",
    images,
    attachments,
  });
  return next;
}

function parseUserMessageImages(
  raw: unknown,
): AgentChatImageAttachment[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const out: AgentChatImageAttachment[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const r = item as Record<string, unknown>;
    const data = typeof r.data === "string" ? r.data : undefined;
    if (!data) continue;
    const mimeType =
      typeof r.mime_type === "string"
        ? r.mime_type
        : typeof r.mimeType === "string"
          ? r.mimeType
          : undefined;
    out.push({ data, mimeType });
  }
  return out.length > 0 ? out : undefined;
}

function parseUserMessageAttachments(
  raw: unknown,
): AgentChatDisplayAttachment[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const out: AgentChatDisplayAttachment[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const r = item as Record<string, unknown>;
    const filename = typeof r.filename === "string" ? r.filename : undefined;
    if (!filename) continue;
    const path = typeof r.path === "string" ? r.path : undefined;
    if (!path) continue;
    const mimeType =
      typeof r.mime_type === "string"
        ? r.mime_type
        : typeof r.mimeType === "string"
          ? r.mimeType
          : undefined;
    const size = typeof r.size === "number" ? r.size : undefined;
    out.push({ path, filename, mimeType, size });
  }
  return out.length > 0 ? out : undefined;
}

function applyBrowserEvent(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
  browser: AgentChatBrowserState | null,
): AgentChatState {
  return withMessages(
    { ...state, browser },
    [...state.messages, browserMarker(event)],
  );
}

function browserMarker(event: AgentChatRuntimeEvent): AgentChatMessage {
  const labels: Record<string, { content: string; warning: boolean }> = {
    "browser.provisioned": {
      content: "Browser ready.",
      warning: false,
    },
    "browser.control_granted": {
      content: "A user took control of the browser.",
      warning: true,
    },
    "browser.control_returned": {
      content: "Browser control returned to the agent.",
      warning: false,
    },
    "browser.destroyed": {
      content: "Browser closed.",
      warning: false,
    },
  };
  const label = labels[event.type] ?? {
    content: "Browser updated.",
    warning: false,
  };
  return {
    id: `browser-marker-${event.eventId}`,
    role: "system",
    content: label.content,
    createdAt: new Date(),
    status: "complete",
    systemKind: label.warning ? "browser_marker_warning" : "browser_marker",
  };
}

function applyLlmDelta(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  const deltaContent = stringValue(event.data.content);
  const deltaReasoning = stringValue(event.data.reasoning);
  const { turnId, iterationIndex } = readTurnMeta(event.data);
  const messages = [...state.messages];
  const lastIdx = findLastAssistantIndex(messages);
  const lastMsg = lastIdx >= 0 ? messages[lastIdx] : null;
  const hasUserAfter = lastIdx >= 0 && hasUserAfterIndex(messages, lastIdx);
  const allToolsDone = Boolean(
    lastMsg?.toolCalls?.length &&
      lastMsg.toolCalls.every((tc) => tc.status !== "running"),
  );
  const canAppend = Boolean(
    lastMsg &&
      lastMsg.status === "streaming" &&
      !allToolsDone &&
      !hasUserAfter,
  );

  if (canAppend && lastMsg) {
    messages[lastIdx] = {
      ...lastMsg,
      content: deltaContent ? lastMsg.content + deltaContent : lastMsg.content,
      reasoning: deltaReasoning
        ? (lastMsg.reasoning ?? "") + deltaReasoning
        : lastMsg.reasoning,
      turnId: turnId ?? lastMsg.turnId,
      iterationIndex: iterationIndex ?? lastMsg.iterationIndex,
    };
  } else {
    messages.push({
      id: `evt-${event.eventId}`,
      role: "assistant",
      content: deltaContent,
      reasoning: deltaReasoning || undefined,
      createdAt: new Date(),
      status: "streaming",
      turnId,
      iterationIndex,
    });
  }

  return {
    ...state,
    messages,
    hadDeltas: state.hadDeltas || Boolean(deltaContent),
    terminal: false,
    isRunning: true,
  };
}

function applyLlmResponse(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  const messages = [...state.messages];
  const message = objectValue(event.data.message);
  const responseContent = stringValue(message?.content) ||
    stringValue(event.data.content);
  const toolCalls = Array.isArray(message?.tool_calls)
    ? message?.tool_calls
    : [];
  const hasToolCalls = toolCalls.length > 0;
  const responseToolCallIds = toolCallIdsFromResponse(toolCalls);
  const { turnId, iterationIndex } = readTurnMeta(event.data);
  const idx = findLastAssistantIndex(messages);
  const prevAssistant = idx >= 0 ? messages[idx] : undefined;
  const prevHasTools = Boolean(prevAssistant?.toolCalls?.length);
  const hasUserAfter = idx >= 0 && hasUserAfterIndex(messages, idx);
  const matchesExistingToolTurn = Boolean(
    hasToolCalls &&
      prevAssistant?.toolCalls?.length &&
      responseToolCallIds.length > 0 &&
      !hasUserAfter &&
      responseToolCallIds.every((id) =>
        prevAssistant.toolCalls?.some((tc) => tc.id === id)
      ),
  );

  // NOTE: on tool-call iterations we now KEEP ``content`` separate
  // from ``reasoning`` instead of folding them.  ``messageToEntries``
  // surfaces ``content`` as a dedicated narration entry rendered as
  // the iteration's italic prose line ("I'll fetch the weather…");
  // ``reasoning`` stays inside the collapsible CoT viewer.  Older
  // sessions that pre-date this change won't have the split signal
  // but the new rendering handles ``content === ""`` gracefully.
  if (state.hadDeltas && idx >= 0 && !hasUserAfter) {
    const current = messages[idx]!;
    messages[idx] = {
      ...current,
      status: hasToolCalls ? "streaming" : "complete",
      llmResponseEventId: event.eventId,
      turnId: turnId ?? current.turnId,
      iterationIndex: iterationIndex ?? current.iterationIndex,
    };
  } else if (matchesExistingToolTurn && idx >= 0) {
    const current = messages[idx]!;
    messages[idx] = {
      ...current,
      content: responseContent
        ? appendText(current.content, responseContent)
        : current.content,
      status: "streaming",
      llmResponseEventId: event.eventId,
      turnId: turnId ?? current.turnId,
      iterationIndex: iterationIndex ?? current.iterationIndex,
    };
  } else if (prevHasTools || !prevAssistant || hasUserAfter) {
    messages.push({
      id: `evt-${event.eventId}`,
      role: "assistant",
      content: responseContent,
      reasoning: undefined,
      createdAt: new Date(),
      status: hasToolCalls ? "streaming" : "complete",
      llmResponseEventId: event.eventId,
      turnId,
      iterationIndex,
    });
  } else {
    const current = messages[idx]!;
    messages[idx] = {
      ...current,
      content: responseContent || current.content,
      status: hasToolCalls ? "streaming" : "complete",
      llmResponseEventId: event.eventId,
      turnId: turnId ?? current.turnId,
      iterationIndex: iterationIndex ?? current.iterationIndex,
    };
  }

  const inputTokens = numberValue(event.data.input_tokens);
  const outputTokens = numberValue(event.data.output_tokens);
  return {
    ...state,
    messages,
    hadDeltas: false,
    isRunning: hasToolCalls,
    tokenUsage: {
      inputTokens,
      outputTokens,
      reasoningTokens: numberValue(event.data.reasoning_tokens),
      cachedInputTokens: numberValue(event.data.cache_read_tokens),
      totalTokens: inputTokens + outputTokens,
      contextWindow: numberValue(event.data.context_window),
      model: stringValue(event.data.model),
    },
  };
}

function toolCallIdsFromResponse(toolCalls: unknown[]): string[] {
  return toolCalls
    .map((toolCall) => stringValue(objectValue(toolCall)?.id))
    .filter((id) => id.length > 0);
}

function appendText(existing: string | undefined, addition: string): string {
  if (!existing) return addition;
  if (existing.includes(addition)) return existing;
  return `${existing}\n${addition}`;
}

function applyLlmThinking(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  const reasoningText = stringValue(event.data.reasoning) ||
    stringValue(event.data.content);
  const { turnId, iterationIndex } = readTurnMeta(event.data);
  const messages = [...state.messages];
  const idx = findLastAssistantIndex(messages);
  const prev = idx >= 0 ? messages[idx] : null;
  const allToolsDone = Boolean(
    prev?.toolCalls?.length &&
      prev.toolCalls.every((tc) => tc.status !== "running"),
  );
  const hasUserAfter = idx >= 0 && hasUserAfterIndex(messages, idx);

  if (!prev || allToolsDone || hasUserAfter) {
    messages.push({
      id: `evt-${event.eventId}`,
      role: "assistant",
      content: "",
      reasoning: reasoningText,
      createdAt: new Date(),
      status: "streaming",
      turnId,
      iterationIndex,
    });
  } else {
    // llm.thinking carries the COMPLETE reasoning snapshot for the
    // iteration; the same text was already streamed incrementally via
    // llm.delta(reasoning=…) and accumulated into prev.reasoning on the
    // live path. appendText dedups (skips when the snapshot is already
    // present) so the text isn't doubled — mirrors how content avoids
    // the same delta + terminal-snapshot double. On replay the server
    // drops llm.delta, so prev has no reasoning yet and this sets it once.
    messages[idx] = {
      ...prev,
      reasoning: appendText(prev.reasoning, reasoningText),
      turnId: turnId ?? prev.turnId,
      iterationIndex: iterationIndex ?? prev.iterationIndex,
    };
  }

  return {
    ...state,
    messages,
    terminal: false,
    isRunning: true,
  };
}

function applyIterationSummary(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  const turnId = optionalStringValue(event.data.turn_id);
  const iterationIndex = optionalNumberValue(event.data.iteration_index);
  const summary = optionalStringValue(event.data.summary);
  if (!turnId || iterationIndex === null || !summary) return state;

  const idx = state.messages.findIndex(
    (m) =>
      m.role === "assistant" &&
      m.turnId === turnId &&
      m.iterationIndex === iterationIndex,
  );
  if (idx < 0) return state;

  const toolCallIds = Array.isArray(event.data.tool_call_ids)
    ? (event.data.tool_call_ids as unknown[]).map((x) => String(x))
    : [];

  const messages = [...state.messages];
  messages[idx] = {
    ...messages[idx]!,
    iterationSummary: {
      iterationIndex,
      summary,
      toolCallIds,
      startedAt: stringValue(event.data.started_at),
      endedAt: stringValue(event.data.ended_at),
    },
  };
  return { ...state, messages };
}

function applyTurnSummary(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  const turnId = optionalStringValue(event.data.turn_id);
  if (!turnId) return state;

  const recap = stringValue(event.data.recap);
  const artifacts = parseTurnArtifacts(event.data.artifacts);

  // Attach to the LAST assistant message in this turn.  The last
  // iteration of the turn is what the SDK renders the TurnSummaryCard
  // under.
  let idx = -1;
  for (let i = state.messages.length - 1; i >= 0; i--) {
    const message = state.messages[i]!;
    if (message.role === "assistant" && message.turnId === turnId) {
      idx = i;
      break;
    }
  }
  if (idx < 0) return state;

  const messages = [...state.messages];
  messages[idx] = {
    ...messages[idx]!,
    turnSummary: { turnId, recap, artifacts },
  };
  return { ...state, messages };
}

function parseTurnArtifacts(raw: unknown): AgentChatTurnArtifactRef[] {
  if (!Array.isArray(raw)) return [];
  const out: AgentChatTurnArtifactRef[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const obj = item as Record<string, unknown>;
    const kind = obj.kind;
    if (
      kind !== "file" &&
      kind !== "artifact" &&
      kind !== "url" &&
      kind !== "command"
    ) {
      continue;
    }
    const label = typeof obj.label === "string" ? obj.label : "";
    const ref = typeof obj.ref === "string" ? obj.ref : "";
    if (!label || !ref) continue;
    out.push({ kind, label, ref });
  }
  return out;
}

function applyToolCall(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  const messages = [...state.messages];
  const assistantIdx = findLastAssistantIndex(messages);
  let assistant = assistantIdx >= 0 ? messages[assistantIdx] : null;
  const userAfterAssistant = assistantIdx >= 0 &&
    hasUserAfterIndex(messages, assistantIdx);

  if (!assistant || assistant.status === "complete" || userAfterAssistant) {
    assistant = {
      id: `evt-${event.eventId}-tc`,
      role: "assistant",
      content: "",
      createdAt: new Date(),
      status: "streaming",
    };
    messages.push(assistant);
  } else {
    assistant = { ...assistant };
    messages[assistantIdx] = assistant;
  }

  const existingCalls = assistant.toolCalls ?? [];
  const toolCallId = stringValue(event.data.tool_call_id) || `tc-${event.eventId}`;
  if (!existingCalls.some((tc) => tc.id === toolCallId)) {
    const entry: AgentChatToolCallInfo = {
      id: toolCallId,
      toolName: stringValue(event.data.name) ||
        stringValue(event.data.tool_name) ||
        "unknown",
      args: typeof event.data.arguments === "string"
        ? event.data.arguments
        : JSON.stringify(event.data.arguments ?? {}),
      status: "running",
    };
    if (event.data.checkpoint_hash) {
      entry.checkpointHash = String(event.data.checkpoint_hash);
    }
    assistant.toolCalls = [...existingCalls, entry];
  }

  return {
    ...state,
    messages,
    terminal: false,
    isRunning: true,
  };
}

function applyToolResult(
  messages: AgentChatMessage[],
  data: Record<string, unknown>,
): AgentChatMessage[] {
  const targetId = stringValue(data.tool_call_id);
  let matchIdx = -1;
  for (let i = messages.length - 1; i >= 0; i--) {
    if (
      messages[i]?.role === "assistant" &&
      messages[i]?.toolCalls?.some((tc) => tc.id === targetId)
    ) {
      matchIdx = i;
      break;
    }
  }
  if (matchIdx < 0) return messages;

  const next = [...messages];
  const matchMsg = next[matchIdx]!;
  const rawResult = data.content ?? data.result;
  const formattedResult = typeof rawResult === "string"
    ? rawResult
    : JSON.stringify(rawResult ?? null);
  const isCancelled = data.cancelled === true ||
    formattedResult === "[cancelled (sibling error)]";
  next[matchIdx] = {
    ...matchMsg,
    toolCalls: matchMsg.toolCalls!.map((tc) =>
      tc.id === targetId
        ? {
            ...tc,
            result: formattedResult,
            status: isCancelled ? "error" : "complete",
            cancelled: isCancelled || undefined,
          }
        : tc,
    ),
  };
  return next;
}

function applyTerminalEvent(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  const messages = [...state.messages];
  const errorInfo = buildErrorInfo(event);
  const doneIdx = findLastAssistantIndex(messages);

  if (doneIdx >= 0 && messages[doneIdx]?.status === "streaming") {
    const doneMsg = messages[doneIdx]!;
    messages[doneIdx] = {
      ...doneMsg,
      toolCalls: doneMsg.toolCalls?.map((tc) =>
        tc.status === "running"
          ? {
              ...tc,
              status: event.type === "session.pause" ? "complete" : "error",
              result: tc.result ??
                (event.type === "session.pause" ? "[interrupted]" : "[failed]"),
            }
          : tc,
      ),
      status: event.type === "session.fail" ? "error" : "complete",
      errorInfo: errorInfo ?? doneMsg.errorInfo,
    };
  } else if (event.type === "session.fail" && errorInfo) {
    messages.push({
      id: `error-${event.eventId}`,
      role: "system",
      systemKind: "error",
      content: "",
      createdAt: new Date(),
      status: "error",
      errorInfo,
    });
  }

  return {
    ...state,
    messages,
    terminal: true,
    isRunning: false,
  };
}

function buildErrorInfo(
  event: AgentChatRuntimeEvent,
): AgentChatErrorInfo | undefined {
  if (
    event.type !== "session.fail" ||
    typeof event.data.error_category !== "string"
  ) {
    return undefined;
  }
  return {
    category: event.data.error_category as AgentChatErrorInfo["category"],
    title: stringValue(event.data.error_title) ||
      "The session failed due to an internal error.",
    detail: stringValue(event.data.error_detail) || stringValue(event.data.error),
    retryable: Boolean(event.data.retryable),
  };
}

function applyExpertResult(
  messages: AgentChatMessage[],
  eventId: number,
  data: Record<string, unknown>,
): AgentChatMessage[] {
  const match = findLatestConsultExpertCall(messages);
  if (!match) return messages;
  // The slash path emits expert.result without a preceding tool.result
  // event (the LLM did not issue the consult_expert call), so the
  // existing tc.result is undefined.  Populate it from the event's
  // ``content`` field so the renderer's "Response" section shows the
  // deliverable for both the slash and LLM-tool-call paths.  When tc.result
  // is already set (LLM path), keep it untouched -- it came from the
  // tool.result event and is the authoritative copy.
  const content = stringValue(data.content);
  const next = [...messages];
  const msg = next[match.msgIdx]!;
  next[match.msgIdx] = {
    ...msg,
    toolCalls: msg.toolCalls?.map((tc) => {
      if (tc.id !== match.toolId) return tc;
      const updated: AgentChatToolCallInfo = {
        ...tc,
        expertResultEventId: eventId,
        status: "complete",
      };
      if (tc.result === undefined && content) {
        updated.result = JSON.stringify({ content });
      }
      return updated;
    }),
  };
  return next;
}

function applyExpertDelegation(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  // The LLM-initiated path emits a tool.call(consult_expert) BEFORE the
  // expert.delegation event, so a pending consult_expert tool call is
  // already on the latest assistant message.  Skip — the existing frame
  // is the canonical render target.  The slash path emits
  // expert.delegation as the first signal that the consultation
  // happened, so we synthesize the consult_expert frame here.
  if (findLatestConsultExpertCall(state.messages) !== null) {
    return state;
  }

  const expertName = stringValue(event.data.expert);
  const task = stringValue(event.data.task);
  if (!expertName) return state;

  const messages = [...state.messages];
  const assistantIdx = findLastAssistantIndex(messages);
  let assistant = assistantIdx >= 0 ? messages[assistantIdx] : null;
  const userAfterAssistant = assistantIdx >= 0 &&
    hasUserAfterIndex(messages, assistantIdx);

  if (!assistant || assistant.status === "complete" || userAfterAssistant) {
    assistant = {
      id: `evt-${event.eventId}-expert-delegation`,
      role: "assistant",
      content: "",
      createdAt: new Date(),
      status: "streaming",
    };
    messages.push(assistant);
  } else {
    assistant = { ...assistant };
    messages[assistantIdx] = assistant;
  }

  const toolCall: AgentChatToolCallInfo = {
    id: `expert-delegation-${event.eventId}`,
    toolName: "consult_expert",
    args: JSON.stringify({ expert: expertName, question: task }),
    status: "running",
  };
  assistant.toolCalls = [...(assistant.toolCalls ?? []), toolCall];

  return {
    ...state,
    messages,
    terminal: false,
    isRunning: true,
  };
}

function applyExpertFailure(
  messages: AgentChatMessage[],
  eventId: number,
  data: Record<string, unknown>,
): AgentChatMessage[] {
  // Mirror applyExpertResult but mark the call as failed.  The
  // ExpertToolBlock renderer derives its "failed" badge from
  // ``parseExpertResult(tc.result)`` finding an ``error`` field, so we
  // shape the result as a JSON error blob.  Attach expertResultEventId
  // so it can still be addressed (for ordering / replay), though the
  // feedback UI is gated to non-running tool calls and existence of
  // adapter.submitExpertFeedback -- rating a failure is supported but
  // optional.
  const match = findLatestConsultExpertCall(messages);
  if (!match) return messages;
  const errorMsg = stringValue(data.error) || "Expert consultation failed.";
  const next = [...messages];
  const msg = next[match.msgIdx]!;
  next[match.msgIdx] = {
    ...msg,
    toolCalls: msg.toolCalls?.map((tc) => {
      if (tc.id !== match.toolId) return tc;
      return {
        ...tc,
        expertResultEventId: eventId,
        status: "error",
        result: tc.result ?? JSON.stringify({ error: errorMsg }),
      };
    }),
  };
  return next;
}

function applyExpertFeedback(
  messages: AgentChatMessage[],
  type: "expert.endorse" | "expert.override",
  data: Record<string, unknown>,
): AgentChatMessage[] {
  const resultEventId = typeof data.target_event_id === "number"
    ? data.target_event_id
    : undefined;
  if (resultEventId == null) return messages;
  const rating = type === "expert.endorse" ? "up" : "down";
  const reason = typeof data.reason === "string" ? data.reason : undefined;
  const next = [...messages];
  for (let i = next.length - 1; i >= 0; i--) {
    const msg = next[i];
    if (!msg?.toolCalls) continue;
    const hit = msg.toolCalls.find(
      (tc) => tc.expertResultEventId === resultEventId,
    );
    if (!hit) continue;
    if (
      hit.expertFeedback?.rating === rating &&
      hit.expertFeedback?.reason === reason
    ) {
      return messages;
    }
    next[i] = {
      ...msg,
      toolCalls: msg.toolCalls.map((tc) =>
        tc.expertResultEventId === resultEventId
          ? { ...tc, expertFeedback: { rating, reason } }
          : tc,
      ),
    };
    return next;
  }
  return messages;
}

function applyUserFeedback(
  messages: AgentChatMessage[],
  data: Record<string, unknown>,
): AgentChatMessage[] {
  // The backend emits the same `user.feedback` event for interactive
  // user ratings (source="user") and automated judges
  // (source="judge"). The chat UI only reflects the human user's
  // thumbs state; judge ratings flow into training-data selectors,
  // not into this surface.
  const source = typeof data.source === "string" ? data.source : "user";
  if (source !== "user") return messages;

  const targetEventId =
    typeof data.target_event_id === "number" ? data.target_event_id : undefined;
  if (targetEventId == null) return messages;

  const rating = data.rating === "down" ? "down" : "up";
  const reason = typeof data.reason === "string" ? data.reason : undefined;

  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg?.role !== "assistant") continue;
    if (msg.llmResponseEventId !== targetEventId) continue;
    if (
      msg.userFeedback?.rating === rating &&
      msg.userFeedback?.reason === reason
    ) {
      return messages;
    }
    const next = [...messages];
    next[i] = { ...msg, userFeedback: { rating, reason } };
    return next;
  }
  return messages;
}

function applyAskUserQuestionResponse(
  messages: AgentChatMessage[],
  data: Record<string, unknown>,
): AgentChatMessage[] {
  const targetToolId = stringValue(data.tool_call_id);
  const responses = Array.isArray(data.responses) ? data.responses : undefined;
  if (!targetToolId || !responses) return messages;
  const next = [...messages];
  for (let i = next.length - 1; i >= 0; i--) {
    const msg = next[i];
    if (!msg?.toolCalls?.some((tc) => tc.id === targetToolId)) continue;
    next[i] = {
      ...msg,
      toolCalls: msg.toolCalls.map((tc) =>
        tc.id === targetToolId
          ? {
              ...tc,
              askUserQuestionAnswers: responses.map((response) => {
                const row = objectValue(response) ?? {};
                return {
                  question: stringValue(row.question),
                  answer: stringValue(row.answer),
                  is_other: Boolean(row.is_other),
                };
              }),
            }
          : tc,
      ),
    };
    return next;
  }
  return messages;
}

function applyPolicyDenied(
  state: AgentChatState,
  data: Record<string, unknown>,
): AgentChatState {
  const messages = [...state.messages];
  const policyIdx = findLastAssistantIndex(messages);
  if (policyIdx >= 0) {
    const policyMsg = messages[policyIdx]!;
    messages[policyIdx] = {
      ...policyMsg,
      content: `${policyMsg.content}\n\n**Policy denied**: ${
        stringValue(data.reason) || "Action blocked by governance policy."
      }`,
      status: "error",
    };
  }
  return {
    ...state,
    messages,
    isRunning: false,
  };
}

function withMessages(
  state: AgentChatState,
  messages: AgentChatMessage[],
): AgentChatState {
  return { ...state, messages };
}

function findLastAssistantIndex(messages: AgentChatMessage[]): number {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i]?.role === "assistant") return i;
  }
  return -1;
}

function hasUserAfterIndex(messages: AgentChatMessage[], idx: number): boolean {
  for (let i = idx + 1; i < messages.length; i++) {
    if (messages[i]?.role === "user") return true;
  }
  return false;
}

function findToolNameById(
  messages: AgentChatMessage[],
  toolCallId: string,
): string | null {
  if (!toolCallId) return null;
  for (let i = messages.length - 1; i >= 0; i--) {
    const tc = messages[i]?.toolCalls?.find((c) => c.id === toolCallId);
    if (tc) return tc.toolName;
  }
  return null;
}

/**
 * Append a research source to the runtime state when a successful
 * ``research_memory(add)`` tool result arrives.  Other tool results
 * pass through unchanged.
 *
 * Dedup is by ``sourceId``: the harness already returns the same
 * ``source_id`` for a duplicate URL (see
 * ``surogates.research.memory_bank.add_entry``), so a second add of
 * the same URL is a no-op here.  We also dedup defensively in case a
 * pubsub-driven event arrives twice.
 */
function collectResearchSource(
  state: AgentChatState,
  toolName: string | null,
  data: Record<string, unknown>,
): AgentChatState {
  if (toolName !== "research_memory") return state;
  const rawResult = data.content ?? data.result;
  const resultText = typeof rawResult === "string"
    ? rawResult
    : JSON.stringify(rawResult ?? {});
  let parsed: {
    success?: boolean;
    source_id?: string;
    url?: string;
    title?: string;
  };
  try {
    parsed = JSON.parse(resultText);
  } catch {
    return state;
  }
  if (!parsed.success || !parsed.source_id || !parsed.url) return state;
  if (state.researchSources.some((s) => s.sourceId === parsed.source_id)) {
    return state;
  }
  return {
    ...state,
    researchSources: [
      ...state.researchSources,
      {
        sourceId: parsed.source_id,
        url: parsed.url,
        title: parsed.title ?? "",
      },
    ],
  };
}

function findLatestConsultExpertCall(
  messages: AgentChatMessage[],
): { msgIdx: number; toolId: string } | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (!msg?.toolCalls) continue;
    for (let j = msg.toolCalls.length - 1; j >= 0; j--) {
      const tc = msg.toolCalls[j];
      if (
        tc?.toolName === "consult_expert" &&
        tc.expertResultEventId === undefined
      ) {
        return { msgIdx: i, toolId: tc.id };
      }
    }
  }
  return null;
}

function objectValue(value: unknown): Record<string, unknown> | undefined {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return undefined;
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberValue(value: unknown): number {
  return typeof value === "number" ? value : 0;
}

/**
 * Distinguishes "missing" from "zero" — `numberValue` collapses both to
 * 0 which is fine for token counts but wrong for an event correlator
 * like ``iteration_index`` where 0 is a valid first iteration.
 */
function optionalNumberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function optionalStringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

/**
 * Pull ``turn_id`` and ``iteration_index`` off an LLM event payload.
 * Both fields are optional so older harnesses without the Simple
 * chat-mode plumbing still produce parseable events.
 */
function readTurnMeta(
  data: Record<string, unknown>,
): { turnId?: string; iterationIndex?: number } {
  const turnId = optionalStringValue(data.turn_id);
  const iterationIndex = optionalNumberValue(data.iteration_index);
  return {
    turnId,
    iterationIndex: iterationIndex === null ? undefined : iterationIndex,
  };
}
