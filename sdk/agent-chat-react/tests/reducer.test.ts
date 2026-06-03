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

  it("attaches ask_user_question.response answers to the matching tool call", () => {
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
            toolName: "ask_user_question",
            args: "{}",
            status: "running",
          },
        ],
      },
    ]);

    const next = applyAgentChatEvent(state, {
      type: "ask_user_question.response",
      eventId: 13,
      data: {
        tool_call_id: "tc-1",
        responses: [
          { question: "Pick one", answer: "A", is_other: false },
        ],
      },
    });

    expect(next.messages[0]?.toolCalls?.[0]?.askUserQuestionAnswers).toEqual([
      { question: "Pick one", answer: "A", is_other: false },
    ]);
  });

  it("merges a replayed llm.response into the existing matching tool-call turn", () => {
    const afterThinking = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.thinking",
      eventId: 1,
      data: { reasoning: "Need user input." },
    });
    const afterToolCall = applyAgentChatEvent(afterThinking, {
      type: "tool.call",
      eventId: 2,
      data: {
        tool_call_id: "tc-ask",
        name: "ask_user_question",
        arguments: { questions: [{ prompt: "Pick one" }] },
      },
    });

    const next = applyAgentChatEvent(afterToolCall, {
      type: "llm.response",
      eventId: 3,
      data: {
        message: {
          role: "assistant",
          content: "I need one decision before continuing.",
          tool_calls: [
            {
              id: "tc-ask",
              type: "function",
              function: {
                name: "ask_user_question",
                arguments: "{\"questions\":[{\"prompt\":\"Pick one\"}]}",
              },
            },
          ],
        },
      },
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]?.toolCalls?.map((tc) => tc.id)).toEqual([
      "tc-ask",
    ]);
    // On tool-call iterations the reducer keeps ``content`` separate
    // from ``reasoning`` (see the NOTE in applyLlmResponse): the
    // chain-of-thought stays in ``reasoning`` while the assistant's
    // user-facing prose lands in ``content``. They must not be folded.
    expect(next.messages[0]?.reasoning).toContain("Need user input.");
    expect(next.messages[0]?.content).toContain(
      "I need one decision before continuing.",
    );
  });

  it("folds browser lifecycle events into browser state and timeline markers", () => {
    const provisioned = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "browser.provisioned",
      eventId: 21,
      data: {},
    });

    expect(provisioned.browser).toEqual({
      status: "live",
      controlOwner: null,
    });
    expect(provisioned.messages.at(-1)).toMatchObject({
      id: "browser-marker-21",
      role: "system",
      systemKind: "browser_marker",
      status: "complete",
    });
    expect(provisioned.messages.at(-1)?.content).toMatch(/browser ready/i);

    const granted = applyAgentChatEvent(provisioned, {
      type: "browser.control_granted",
      eventId: 22,
      data: { owner_user_id: "user-1" },
    });

    expect(granted.browser).toEqual({
      status: "user-control",
      controlOwner: "user-1",
    });
    expect(granted.messages.at(-1)).toMatchObject({
      id: "browser-marker-22",
      role: "system",
      systemKind: "browser_marker_warning",
      status: "complete",
    });
    expect(granted.messages.at(-1)?.content).toMatch(/took control/i);

    const returned = applyAgentChatEvent(granted, {
      type: "browser.control_returned",
      eventId: 23,
      data: {},
    });

    expect(returned.browser).toEqual({
      status: "live",
      controlOwner: null,
    });

    const destroyed = applyAgentChatEvent(returned, {
      type: "browser.destroyed",
      eventId: 24,
      data: {},
    });

    expect(destroyed.browser).toBeNull();
    expect(destroyed.messages.at(-1)).toMatchObject({
      id: "browser-marker-24",
      systemKind: "browser_marker",
    });
    expect(destroyed.messages.at(-1)?.content).toMatch(/browser closed/i);
  });

  it("stores llmResponseEventId on a freshly created assistant message", () => {
    const next = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.response",
      eventId: 17,
      data: { message: { content: "hi there" } },
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]?.role).toBe("assistant");
    expect(next.messages[0]?.llmResponseEventId).toBe(17);
  });

  it("updates llmResponseEventId when a second llm.response lands on the same message", () => {
    const afterDelta = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.delta",
      eventId: 10,
      data: { content: "partial" },
    });
    const afterFirst = applyAgentChatEvent(afterDelta, {
      type: "llm.response",
      eventId: 11,
      data: { message: { content: "partial", tool_calls: [{ id: "tc-1" }] } },
    });
    const afterSecond = applyAgentChatEvent(afterFirst, {
      type: "llm.response",
      eventId: 25,
      data: { message: { content: "final answer" } },
    });

    expect(afterSecond.messages).toHaveLength(1);
    expect(afterSecond.messages[0]?.llmResponseEventId).toBe(25);
  });

  it("applies user.feedback (source=user) to the assistant message with matching llmResponseEventId", () => {
    const seeded = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.response",
      eventId: 50,
      data: { message: { content: "answer" } },
    });

    const next = applyAgentChatEvent(seeded, {
      type: "user.feedback",
      eventId: 51,
      data: {
        target_event_id: 50,
        rating: "down",
        source: "user",
        reason: "missed a column",
      },
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]?.userFeedback).toEqual({
      rating: "down",
      reason: "missed a column",
    });
  });

  it("ignores user.feedback events emitted by judges (source != 'user')", () => {
    const seeded = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.response",
      eventId: 60,
      data: { message: { content: "answer" } },
    });

    const next = applyAgentChatEvent(seeded, {
      type: "user.feedback",
      eventId: 61,
      data: {
        target_event_id: 60,
        rating: "up",
        source: "judge",
      },
    });

    expect(next.messages[0]?.userFeedback).toBeUndefined();
  });

  it("returns the same messages reference when user.feedback replays unchanged", () => {
    const seeded = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.response",
      eventId: 70,
      data: { message: { content: "answer" } },
    });
    const once = applyAgentChatEvent(seeded, {
      type: "user.feedback",
      eventId: 71,
      data: { target_event_id: 70, rating: "up", source: "user" },
    });
    const twice = applyAgentChatEvent(once, {
      type: "user.feedback",
      eventId: 71,
      data: { target_event_id: 70, rating: "up", source: "user" },
    });

    expect(twice.messages).toBe(once.messages);
  });
});

describe("applyAgentChatEvent — user.message images and attachments", () => {
  it("hydrates images on replay from event.data.images (normalizing mime_type)", () => {
    const next = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "user.message",
      eventId: 7,
      data: {
        content: "look",
        images: [
          { data: "data:image/png;base64,xxxx", mime_type: "image/png" },
        ],
      },
    });

    const msg = next.messages.at(-1)!;
    expect(msg.role).toBe("user");
    expect(msg.images).toEqual([
      { data: "data:image/png;base64,xxxx", mimeType: "image/png" },
    ]);
  });

  it("hydrates attachments on replay, normalizing mime_type to mimeType", () => {
    const next = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "user.message",
      eventId: 8,
      data: {
        content: "summarize",
        attachments: [{
          path: "uploads/1715-report.pdf",
          filename: "report.pdf",
          mime_type: "application/pdf",
          size: 12345,
        }],
      },
    });

    const msg = next.messages.at(-1)!;
    expect(msg.attachments).toEqual([{
      path: "uploads/1715-report.pdf",
      filename: "report.pdf",
      mimeType: "application/pdf",
      size: 12345,
    }]);
  });

  it("replaces optimistic display attachments with persisted refs on event arrival", () => {
    const state = withMessages([
      {
        id: "local-1",
        role: "user",
        content: "summarize",
        createdAt: new Date("2026-01-01T00:00:00Z"),
        status: "complete",
        attachments: [
          { filename: "report.pdf", mimeType: "application/pdf", size: 12345 },
        ],
      },
    ]);

    const next = applyAgentChatEvent(state, {
      type: "user.message",
      eventId: 8,
      data: {
        content: "summarize",
        attachments: [{
          path: "uploads/1715-report.pdf",
          filename: "report.pdf",
          mime_type: "application/pdf",
          size: 12345,
        }],
      },
    });

    expect(next.messages).toHaveLength(1);
    const msg = next.messages[0]!;
    expect(msg.id).toBe("evt-8");
    // After reconciliation the chip is clickable: path is now present.
    expect(msg.attachments?.[0]?.path).toBe("uploads/1715-report.pdf");
  });

  it("hydrates both images and attachments on the same user message", () => {
    const next = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "user.message",
      eventId: 9,
      data: {
        content: "compare these",
        images: [
          { data: "data:image/png;base64,abcd", mime_type: "image/png" },
        ],
        attachments: [{
          path: "uploads/notes.txt",
          filename: "notes.txt",
          mime_type: "text/plain",
          size: 42,
        }],
      },
    });

    const msg = next.messages.at(-1)!;
    expect(msg.images).toHaveLength(1);
    expect(msg.attachments).toHaveLength(1);
    expect(msg.attachments?.[0]?.filename).toBe("notes.txt");
  });

  it("leaves images undefined when payload has no images key", () => {
    const next = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "user.message",
      eventId: 10,
      data: { content: "plain text only" },
    });
    expect(next.messages.at(-1)?.images).toBeUndefined();
    expect(next.messages.at(-1)?.attachments).toBeUndefined();
  });

  it("skips malformed image entries silently", () => {
    const next = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "user.message",
      eventId: 11,
      data: {
        content: "x",
        images: [
          "garbage",
          { mime_type: "image/png" }, // missing data
          { data: "data:image/png;base64,good", mime_type: "image/png" },
        ],
      },
    });
    expect(next.messages.at(-1)?.images).toHaveLength(1);
    expect(next.messages.at(-1)?.images?.[0]?.data).toBe(
      "data:image/png;base64,good",
    );
  });

  it("skips malformed attachment entries silently", () => {
    const next = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "user.message",
      eventId: 12,
      data: {
        content: "x",
        attachments: [
          "garbage",
          { path: "uploads/no-filename" }, // missing filename
          { filename: "no-path.txt" }, // missing path
          { path: "uploads/ok.txt", filename: "ok.txt", size: 7 },
        ],
      },
    });
    const att = next.messages.at(-1)?.attachments;
    expect(att).toHaveLength(1);
    expect(att?.[0]?.path).toBe("uploads/ok.txt");
  });

  it("returns undefined images when the payload value is not an array", () => {
    const next = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "user.message",
      eventId: 13,
      data: { content: "x", images: "nope", attachments: 7 },
    });
    expect(next.messages.at(-1)?.images).toBeUndefined();
    expect(next.messages.at(-1)?.attachments).toBeUndefined();
  });

  it("recovers from an out-of-order pause after resume + user message (mid-stream send)", () => {
    // When the user sends a message mid-stream, the composer calls
    // /pause then /messages. The /messages route emits SESSION_RESUME +
    // USER_MESSAGE, but the harness's abort cleanup can emit a second
    // SESSION_PAUSE that lands *after* the resume — leaving terminal
    // sticky and suppressing the running indicator for the new turn.
    const events: Array<Parameters<typeof applyAgentChatEvent>[1]> = [
      { type: "session.pause", eventId: 1, data: {} },
      { type: "session.resume", eventId: 2, data: {} },
      { type: "session.pause", eventId: 3, data: {} },
      { type: "user.message", eventId: 4, data: { content: "follow-up" } },
      { type: "harness.wake", eventId: 5, data: {} },
      { type: "llm.request", eventId: 6, data: {} },
      { type: "llm.delta", eventId: 7, data: { content: "hi" } },
    ];

    let state = createInitialAgentChatState();
    for (const event of events) {
      state = applyAgentChatEvent(state, event);
    }

    expect(state.terminal).toBe(false);
    expect(state.isRunning).toBe(true);
  });
});
