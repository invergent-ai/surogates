import { describe, expect, it } from "vitest";
import {
  applyAgentChatEvent,
  createInitialAgentChatState,
} from "../src/runtime/reducer";
import type { AgentChatState } from "../src/types";

function withMessages(
  messages: AgentChatState["messages"],
): AgentChatState {
  return {
    ...createInitialAgentChatState(),
    messages,
  };
}

describe("applyAgentChatEvent", () => {
  it("reconciles optimistic user messages with authoritative user.message events", () => {
    const state = withMessages([
      {
        id: "local-1",
        role: "user",
        content: "hello",
        createdAt: new Date("2026-01-01T00:00:00Z"),
        status: "complete",
      },
    ]);

    const next = applyAgentChatEvent(state, {
      type: "user.message",
      eventId: 42,
      data: { content: "hello" },
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]?.id).toBe("evt-42");
    expect(next.messages[0]?.content).toBe("hello");
  });

  it("does not duplicate llm.response content after llm.delta streamed it", () => {
    const afterDelta = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.delta",
      eventId: 10,
      data: { content: "streamed" },
    });

    const next = applyAgentChatEvent(afterDelta, {
      type: "llm.response",
      eventId: 11,
      data: { message: { content: "streamed" } },
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]?.content).toBe("streamed");
    expect(next.messages[0]?.status).toBe("complete");
  });

  it("keeps running true across harness.crash and exposes retry indicator", () => {
    const running = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.request",
      eventId: 1,
      data: {},
    });

    const next = applyAgentChatEvent(running, {
      type: "harness.crash",
      eventId: 2,
      data: {
        error_title: "Provider unavailable",
        error_detail: "upstream 503",
      },
    });

    expect(next.isRunning).toBe(true);
    expect(next.retryIndicator).toEqual({
      title: "Provider unavailable",
      detail: "upstream 503",
      attempt: 1,
    });
  });

  it("marks session.fail as terminal and inserts a standalone error when no assistant slot exists", () => {
    const next = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "session.fail",
      eventId: 9,
      data: {
        error_category: "provider_error",
        error_title: "The provider failed.",
        error_detail: "bad gateway",
        retryable: true,
      },
    });

    expect(next.terminal).toBe(true);
    expect(next.isRunning).toBe(false);
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]).toMatchObject({
      id: "error-9",
      role: "system",
      systemKind: "error",
      status: "error",
      errorInfo: {
        category: "provider_error",
        title: "The provider failed.",
        detail: "bad gateway",
        retryable: true,
      },
    });
  });

  it("attaches artifact metadata as a system timeline message", () => {
    const next = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "artifact.created",
      eventId: 12,
      data: {
        artifact_id: "art-1",
        name: "Report",
        kind: "markdown",
        version: 3,
        size: 128,
      },
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]).toMatchObject({
      id: "evt-12",
      role: "system",
      content: "Report",
      systemKind: "artifact",
      systemMeta: {
        artifact_id: "art-1",
        name: "Report",
        kind: "markdown",
        version: 3,
        size: 128,
      },
    });
  });

  it("attaches clarify.response answers to the matching tool call", () => {
    const state = withMessages([
      {
        id: "evt-1",
        role: "assistant",
        content: "",
        createdAt: new Date("2026-01-01T00:00:00Z"),
        status: "streaming",
        toolCalls: [
          {
            id: "tc-1",
            toolName: "clarify",
            args: "{}",
            status: "running",
          },
        ],
      },
    ]);

    const next = applyAgentChatEvent(state, {
      type: "clarify.response",
      eventId: 13,
      data: {
        tool_call_id: "tc-1",
        responses: [
          { question: "Pick one", answer: "A", is_other: false },
        ],
      },
    });

    expect(next.messages[0]?.toolCalls?.[0]?.clarifyAnswers).toEqual([
      { question: "Pick one", answer: "A", is_other: false },
    ]);
  });
});
