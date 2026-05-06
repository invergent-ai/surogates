import type {
  AgentChatErrorInfo,
  AgentChatMessage,
  AgentChatRuntimeEvent,
  AgentChatState,
  AgentChatTokenUsage,
  AgentChatToolCallInfo,
} from "../types";

export const EMPTY_TOKEN_USAGE: AgentChatTokenUsage = {
  inputTokens: 0,
  outputTokens: 0,
  reasoningTokens: 0,
  cachedInputTokens: 0,
  totalTokens: 0,
  contextWindow: 0,
  model: "",
};

export function createInitialAgentChatState(): AgentChatState {
  return {
    messages: [],
    isRunning: false,
    tokenUsage: EMPTY_TOKEN_USAGE,
    retryIndicator: null,
    lastEventId: 0,
    sessionDone: false,
    hadDeltas: false,
    terminal: false,
  };
}

export function applyAgentChatEvent(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  let nextState: AgentChatState = {
    ...state,
    lastEventId: Math.max(state.lastEventId, event.eventId),
    sessionDone: state.sessionDone || event.type === "session.done",
  };

  nextState = applyRetryIndicator(nextState, event);

  switch (event.type) {
    case "user.message":
      return withMessages(nextState, applyUserMessage(
        nextState.messages,
        event.eventId,
        event.data,
      ));

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

    case "llm.delta":
      return applyLlmDelta(nextState, event);

    case "llm.response":
      return applyLlmResponse(nextState, event);

    case "llm.thinking":
      return applyLlmThinking(nextState, event);

    case "tool.call":
      return applyToolCall(nextState, event);

    case "tool.result":
      return withMessages(
        nextState,
        applyToolResult(nextState.messages, event.data),
      );

    case "harness.wake":
    case "llm.request":
      return nextState.terminal ? nextState : { ...nextState, isRunning: true };

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

    case "expert.result":
      return withMessages(
        nextState,
        applyExpertResult(nextState.messages, event.eventId),
      );

    case "expert.endorse":
    case "expert.override":
      return withMessages(
        nextState,
        applyExpertFeedback(nextState.messages, event.type, event.data),
      );

    case "clarify.response":
      return withMessages(
        nextState,
        applyClarifyResponse(nextState.messages, event.data),
      );

    case "policy.denied":
      return applyPolicyDenied(nextState, event.data);

    case "session.start":
      return nextState;
  }
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
  const next = [...messages];
  const localIdx = next.findIndex(
    (m) =>
      m.role === "user" &&
      m.id.startsWith("local-") &&
      m.content === content,
  );
  if (localIdx >= 0) {
    next[localIdx] = { ...next[localIdx]!, id: `evt-${eventId}` };
    return next;
  }
  next.push({
    id: `evt-${eventId}`,
    role: "user",
    content,
    createdAt: new Date(),
    status: "complete",
  });
  return next;
}

function applyLlmDelta(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  const deltaContent = stringValue(event.data.content);
  const deltaReasoning = stringValue(event.data.reasoning);
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
    };
  } else {
    messages.push({
      id: `evt-${event.eventId}`,
      role: "assistant",
      content: deltaContent,
      reasoning: deltaReasoning || undefined,
      createdAt: new Date(),
      status: "streaming",
    });
  }

  return {
    ...state,
    messages,
    hadDeltas: state.hadDeltas || Boolean(deltaContent),
    isRunning: state.terminal ? state.isRunning : true,
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
  const idx = findLastAssistantIndex(messages);
  const prevAssistant = idx >= 0 ? messages[idx] : undefined;
  const prevHasTools = Boolean(prevAssistant?.toolCalls?.length);
  const hasUserAfter = idx >= 0 && hasUserAfterIndex(messages, idx);

  if (state.hadDeltas && idx >= 0 && !hasUserAfter) {
    const current = messages[idx]!;
    if (hasToolCalls) {
      messages[idx] = {
        ...current,
        reasoning: (current.reasoning ?? "") + current.content,
        content: "",
        status: "streaming",
      };
    } else {
      messages[idx] = { ...current, status: "complete" };
    }
  } else if (prevHasTools || !prevAssistant || hasUserAfter) {
    messages.push({
      id: `evt-${event.eventId}`,
      role: "assistant",
      content: hasToolCalls ? "" : responseContent,
      reasoning: hasToolCalls && responseContent ? responseContent : undefined,
      createdAt: new Date(),
      status: hasToolCalls ? "streaming" : "complete",
    });
  } else {
    const current = messages[idx]!;
    if (hasToolCalls && responseContent) {
      messages[idx] = {
        ...current,
        reasoning: (current.reasoning ?? "") + responseContent,
        status: "streaming",
      };
    } else {
      messages[idx] = {
        ...current,
        content: responseContent || current.content,
        status: hasToolCalls ? "streaming" : "complete",
      };
    }
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

function applyLlmThinking(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState {
  const reasoningText = stringValue(event.data.reasoning) ||
    stringValue(event.data.content);
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
    });
  } else {
    messages[idx] = {
      ...prev,
      reasoning: (prev.reasoning ?? "") + reasoningText,
    };
  }

  return {
    ...state,
    messages,
    isRunning: state.terminal ? state.isRunning : true,
  };
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
    isRunning: state.terminal ? state.isRunning : true,
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
): AgentChatMessage[] {
  const match = findLatestConsultExpertCall(messages);
  if (!match) return messages;
  const next = [...messages];
  const msg = next[match.msgIdx]!;
  next[match.msgIdx] = {
    ...msg,
    toolCalls: msg.toolCalls?.map((tc) =>
      tc.id === match.toolId
        ? { ...tc, expertResultEventId: eventId }
        : tc,
    ),
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

function applyClarifyResponse(
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
              clarifyAnswers: responses.map((response) => {
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
