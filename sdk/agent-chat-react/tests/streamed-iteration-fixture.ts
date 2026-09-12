import type { AgentChatRuntimeEvent } from "../src/types";

// Event ordering from session 0468500e: the first tool finishes before
// the reasoning snapshot and the second tool.call arrive.
export function streamedIterationEvents(): AgentChatRuntimeEvent[] {
  const events: AgentChatRuntimeEvent[] = [];
  const emit = (type: AgentChatRuntimeEvent["type"], data: Record<string, unknown>) => {
    events.push({ type, eventId: events.length + 1, data });
  };
  emit("user.message", { content: "Check the weather" });
  for (const [iterationIndex, tokens] of [34, 143, 232].entries()) {
    const meta = { turn_id: "turn-1", iteration_index: iterationIndex };
    emit("llm.request", meta);
    for (let i = 0; i < tokens; i++) {
      emit("llm.delta", { ...meta, reasoning: "Reasoning. " });
    }
    // The current iteration is still reasoning.
    if (iterationIndex === 2) break;
    const firstToolId = `tool-${iterationIndex}-a`;
    const secondToolId = `tool-${iterationIndex}-b`;
    emit("tool.call", { tool_call_id: firstToolId, name: "web_search", arguments: {} });
    emit("tool.result", { tool_call_id: firstToolId, content: "Search complete" });
    emit("llm.delta", { ...meta, reasoning_tokens: tokens });
    emit("llm.thinking", { ...meta, reasoning: "Reasoning. ".repeat(tokens) });
    emit("tool.call", { tool_call_id: secondToolId, name: "web_search", arguments: {} });
    emit("llm.response", {
      ...meta,
      reasoning_tokens: tokens,
      message: {
        content: "",
        tool_calls: [firstToolId, secondToolId].map((id) => ({
          id, type: "function", function: { name: "web_search", arguments: "{}" },
        })),
      },
    });
    emit("tool.result", { tool_call_id: secondToolId, content: "Search complete" });
  }
  return events;
}
