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

  it("shows a coordinator's follow-up to a thread as the message it reads, and runs the turn", () => {
    const next = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "coordinator.message",
      eventId: 43,
      data: { content: "[From the project's coordinator]\nUse the 2025 figures." },
    });

    expect(next.messages.map((m) => [m.id, m.role, m.content])).toEqual([
      ["evt-43", "user", "[From the project's coordinator]\nUse the 2025 figures."],
    ]);
    expect(next.isRunning).toBe(true);
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

  it("does not duplicate reasoning after llm.delta streamed it then llm.thinking re-sent it", () => {
    // The harness streams reasoning as incremental llm.delta(reasoning)
    // chunks AND emits one terminal llm.thinking carrying the complete
    // text. Both reach the same streaming message on the live path; the
    // reducer must not store the reasoning twice.
    const reasoning =
      "The user wants me to delegate this. Let me use delegate_task.\n";
    let state = createInitialAgentChatState();
    for (const chunk of ["The user wants me ", "to delegate this. ", "Let me use delegate_task.\n"]) {
      state = applyAgentChatEvent(state, {
        type: "llm.delta",
        eventId: 10,
        data: { reasoning: chunk, turn_id: "t1", iteration_index: 0 },
      });
    }
    expect(state.messages[0]?.reasoning).toBe(reasoning);
    expect(state.messages[0]?.reasoningDeltaCount).toBe(3);

    const next = applyAgentChatEvent(state, {
      type: "llm.thinking",
      eventId: 11,
      data: { reasoning, turn_id: "t1", iteration_index: 0 },
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]?.reasoning).toBe(reasoning);
    expect(next.messages[0]?.reasoningDeltaCount).toBe(3);
  });

  it("keeps the live stream count separate from reported reasoning totals", () => {
    let state = createInitialAgentChatState();
    for (let eventId = 1; eventId <= 5; eventId++) {
      state = applyAgentChatEvent(state, {
        type: "llm.delta", eventId,
        data: { reasoning: "Many tokens in one chunk. " },
      });
    }
    expect(state.messages[0]?.reasoningTokens).toBeUndefined();
    expect(state.messages[0]?.reasoningDeltaCount).toBe(5);
    for (const reasoning_tokens of [100, 500, 1200, 1200, 2500]) {
      state = applyAgentChatEvent(state, {
        type: "llm.delta", eventId: 10,
        data: { reasoning_tokens },
      });
      expect(state.messages).toHaveLength(1);
      expect(state.messages[0]?.reasoningTokens).toBe(reasoning_tokens);
      expect(state.messages[0]?.reasoningDeltaCount).toBe(5);
    }
    state = applyAgentChatEvent(state, {
      type: "llm.thinking", eventId: 11,
      data: { reasoning: state.messages[0]?.reasoning },
    });
    expect(state.messages[0]?.reasoningTokens).toBe(2500);
    state = applyAgentChatEvent(state, {
      type: "llm.response", eventId: 12,
      data: { message: { content: "Done" }, reasoning_tokens: 2600 },
    });
    expect(state.messages[0]?.reasoningTokens).toBe(2600);
  });

  it("preserves reported reasoning usage on replay without streamed deltas", () => {
    let state = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.thinking", eventId: 1,
      data: { reasoning: "Historical trace" },
    });
    state = applyAgentChatEvent(state, {
      type: "llm.response", eventId: 2,
      data: { message: { content: "Done" }, reasoning_tokens: 1200 },
    });
    expect(state.messages[0]?.reasoningTokens).toBe(1200);
  });

  it("restores snapshot counts without adding them to live deltas", () => {
    let state = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.delta", eventId: 1, data: { reasoning: "Partial" },
    });
    state = applyAgentChatEvent(state, {
      type: "llm.thinking", eventId: 2,
      data: { reasoning: "Partial", reasoning_delta_count: 293, reasoning_tokens: 300 },
    });
    expect(state.messages[0]?.reasoningDeltaCount).toBe(293);
    expect(state.messages[0]?.reasoningTokens).toBe(300);
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

  it("keeps a local-folder chat's browser as on its computer through a take-over there, and says once when that computer has no browser", () => {
    const at = (state: ReturnType<typeof createInitialAgentChatState>, type: string, eventId: number, data: Record<string, unknown> = {}) =>
      applyAgentChatEvent(state, { type, eventId, data: { session_id: "s-1", computer: true, ...data } } as Parameters<typeof applyAgentChatEvent>[1]);
    const opened = at(createInitialAgentChatState(), "browser.provisioned", 31);
    expect(opened.browser).toEqual({ status: "live", controlOwner: null, computer: true });

    // Taken over on its computer and handed back, as the desktop's pane tells the server: still the computer's.
    const taken = at(opened, "browser.control_granted", 32, { owner_user_id: "u-1" });
    expect(taken.browser).toEqual({ status: "user-control", controlOwner: "u-1", computer: true });
    expect(taken.messages.at(-1)?.content).toMatch(/took control of the browser/i);
    const returned = at(taken, "browser.control_returned", 33, { released_by: "u-1" });
    expect(returned.browser).toEqual({ status: "live", controlOwner: null, computer: true });

    const none = at(returned, "browser.unavailable", 34);
    expect(none.browser).toEqual({ status: "unavailable", controlOwner: null, computer: true });
    expect(none.messages.at(-1)).toMatchObject({ id: "browser-marker-34", systemKind: "browser_marker_warning" });
    expect(none.messages.at(-1)?.content).toMatch(/no supported browser/i);

    // An agent that tries the browser again and again is told each time; the chat says it once.
    const again = at(at(none, "browser.unavailable", 35), "browser.unavailable", 36);
    expect(again.browser).toEqual(none.browser);
    expect(again.messages).toHaveLength(none.messages.length);
    // Said anew once a browser was there between.
    const gone = at(at(again, "browser.provisioned", 37), "browser.unavailable", 38);
    expect(gone.messages.at(-1)).toMatchObject({ id: "browser-marker-38", systemKind: "browser_marker_warning" });
  });

  it("counts the tabs of a chat and of its sub-agents on its computer: one that closes leaves the others' browser showing, and it goes with the last", () => {
    const at = (state: ReturnType<typeof createInitialAgentChatState>, type: string, eventId: number, session: string) =>
      applyAgentChatEvent(state, { type, eventId, data: { session_id: session, computer: true } } as Parameters<typeof applyAgentChatEvent>[1]);
    const said = (state: ReturnType<typeof createInitialAgentChatState>) => state.messages.map((message) => message.content);
    const open = { status: "live", controlOwner: null, computer: true };

    // A sub-agent's tab, as the server writes it to its root chat's log: the chat's browser is open there.
    const sub = at(createInitialAgentChatState(), "browser.provisioned", 41, "child-1");
    expect(sub.browser).toEqual(open);
    expect(said(sub)).toEqual(["Browser ready."]);
    // The chat's own tab beside it: nothing the chat shows changes.
    const both = at(sub, "browser.provisioned", 42, "s-1");
    expect(both.browser).toEqual(open);
    expect(said(both)).toEqual(["Browser ready."]);

    // The sub-agent's closed: the chat's own is open still, and the chat says nothing closed.
    const own = at(both, "browser.destroyed", 43, "child-1");
    expect(own.browser).toEqual(open);
    expect(said(own)).toEqual(["Browser ready."]);
    // Told of that close again: still the chat's own tab.
    expect(at(own, "browser.destroyed", 44, "child-1").browser).toEqual(open);
    // The last tab closed: the browser goes, and the chat says so.
    const none = at(own, "browser.destroyed", 45, "s-1");
    expect(none.browser).toBeNull();
    expect(said(none)).toEqual(["Browser ready.", "Browser closed."]);

    // The chat's own closed first: the sub-agent's keeps the browser there.
    const theirs = at(both, "browser.destroyed", 46, "s-1");
    expect(theirs.browser).toEqual(open);
    expect(at(theirs, "browser.destroyed", 47, "child-1").browser).toBeNull();

    // No supported browser there, found by a sub-agent's call: its own tab goes, and the chat's own is
    // the one left, told of again or not.
    const again = at(at(both, "browser.unavailable", 48, "child-1"), "browser.provisioned", 49, "s-1");
    expect(again.browser).toEqual(open);
    expect(at(again, "browser.destroyed", 50, "s-1").browser).toBeNull();

    // The server's state, asked before it had the first tab, left the chat with no browser: the next tab
    // shows it again, as a tab beside another, with no new line.
    const shown = at({ ...sub, browser: null }, "browser.provisioned", 51, "s-1");
    expect(shown.browser).toEqual(open);
    expect(said(shown)).toEqual(["Browser ready."]);
  });

  it("takes away only its own session's tab when a call finds no supported browser on the chat's computer, and says so once no tab is left", () => {
    const at = (state: ReturnType<typeof createInitialAgentChatState>, type: string, eventId: number, session: string) =>
      applyAgentChatEvent(state, { type, eventId, data: { session_id: session, computer: true } } as Parameters<typeof applyAgentChatEvent>[1]);
    const said = (state: ReturnType<typeof createInitialAgentChatState>) => state.messages.map((message) => message.content);
    const open = { status: "live", controlOwner: null, computer: true };
    const none = { status: "unavailable", controlOwner: null, computer: true };
    const both = at(at(createInitialAgentChatState(), "browser.provisioned", 71, "s-1"), "browser.provisioned", 72, "child-1");

    // A sub-agent's call found none while the chat's own tab is open: the chat's browser is as it was,
    // as the server's state answers it, and the chat says nothing.
    const own = at(both, "browser.unavailable", 73, "child-1");
    expect(own.browser).toEqual(open);
    expect(own.browserTabs).toEqual(["s-1"]);
    expect(said(own)).toEqual(["Browser ready."]);
    // The chat's own call takes its own tab the same way, and leaves the sub-agent's.
    const theirs = at(both, "browser.unavailable", 74, "s-1");
    expect(theirs.browser).toEqual(open);
    expect(theirs.browserTabs).toEqual(["child-1"]);
    // One with no tab of its own open takes none.
    const neither = at(both, "browser.unavailable", 75, "child-2");
    expect(neither.browser).toEqual(open);
    expect(neither.browserTabs).toEqual(["s-1", "child-1"]);
    // The last tab's own session finds none: no tab is left, and the chat says there is no browser.
    const gone = at(own, "browser.unavailable", 76, "s-1");
    expect(gone.browser).toEqual(none);
    expect(gone.browserTabs).toEqual([]);
    expect(said(gone)).toEqual(["Browser ready.", "No supported browser on the chat's computer."]);
  });

  it("says that a chat's computer has no supported browser though the server's state said so first, and once", () => {
    const at = (state: ReturnType<typeof createInitialAgentChatState>, eventId: number) =>
      applyAgentChatEvent(state, { type: "browser.unavailable", eventId, data: { session_id: "s-1", computer: true } });
    const said = (state: ReturnType<typeof createInitialAgentChatState>) => state.messages.map((message) => message.content);
    const none = { status: "unavailable" as const, controlOwner: null, computer: true };

    // At a reload the server's state can answer before the chat's own event is replayed: the chat has said nothing yet.
    const first = at({ ...createInitialAgentChatState(), browser: none }, 61);
    expect(first.browser).toEqual(none);
    expect(said(first)).toEqual(["No supported browser on the chat's computer."]);
    // The agent's next tries add nothing: it is what the chat said last of its browser.
    expect(said(at(at(first, 62), 63))).toEqual(said(first));
    // Nor after the conversation went on: nothing of its browser was said between.
    const talked = applyAgentChatEvent(first, { type: "user.message", eventId: 64, data: { content: "Try again" } });
    expect(said(at(talked, 65))).toEqual(["No supported browser on the chat's computer.", "Try again"]);
    // A state that lost it is told again, with no second line.
    const told = at({ ...first, browser: null }, 66);
    expect(told.browser).toEqual(none);
    expect(said(told)).toEqual(said(first));
  });

  it("tracks whether the session waits for its computer", () => {
    const initial = createInitialAgentChatState();
    expect(initial.deviceWait).toBeNull();

    const waiting = applyAgentChatEvent(initial, {
      type: "device.waiting",
      eventId: 31,
      data: { device_id: "dev-1", device_name: "Flavius's ThinkPad", reason: "offline" },
    });
    expect(waiting.deviceWait).toEqual({
      deviceId: "dev-1",
      deviceName: "Flavius's ThinkPad",
      reason: "offline",
    });
    expect(waiting.messages).toEqual(initial.messages);

    const resumed = applyAgentChatEvent(waiting, {
      type: "device.resumed",
      eventId: 32,
      data: { device_id: "dev-1" },
    });
    expect(resumed.deviceWait).toBeNull();
  });

  it("forgets a wait when the session wakes again", () => {
    const waiting = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "device.waiting",
      eventId: 41,
      data: { device_id: "dev-1", device_name: "Flavius's ThinkPad", reason: "offline" },
    });
    // The worker that saw the computer away is gone; the next one announces the wait again.
    const woken = applyAgentChatEvent(waiting, { type: "harness.wake", eventId: 42, data: {} });
    expect(woken.deviceWait).toBeNull();
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

describe("autonomous resume after session.complete (mission/research)", () => {
  it("session.resume clears terminal even when no user.message precedes it", () => {
    // Intake turn ends: session.complete -> terminal.
    let s = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "session.complete",
      eventId: 1,
      data: {},
    });
    expect(s.terminal).toBe(true);

    // The mission coordinator resumes ON ITS OWN (no user message). The
    // resume + the events it drives must NOT be gated, or the live thread
    // freezes until a reload.
    s = applyAgentChatEvent(s, { type: "session.resume", eventId: 2, data: {} });
    expect(s.terminal).toBe(false);
    expect(s.isRunning).toBe(true);

    // Subsequent coordinator activity now flows (terminal already cleared).
    s = applyAgentChatEvent(s, { type: "llm.request", eventId: 3, data: {} });
    s = applyAgentChatEvent(s, {
      type: "llm.delta",
      eventId: 4,
      data: { content: "working" },
    });
    expect(s.terminal).toBe(false);
    expect(s.messages.some((m) => m.content.includes("working"))).toBe(true);
  });
});

describe("insufficient credits (402 token-credit gate)", () => {
  it("detects the proxy payload via isInsufficientCreditsError", async () => {
    const { isInsufficientCreditsError } = await import("../src/runtime/reducer");
    expect(
      isInsufficientCreditsError(
        "Error code: 402 - {'detail': {'error': 'insufficient_credits', 'available': -31725}}",
      ),
    ).toBe(true);
    expect(isInsufficientCreditsError("network unreachable")).toBe(false);
    expect(isInsufficientCreditsError(undefined)).toBe(false);
  });

  it("flags a 402 session.fail as a billing card (non-retryable)", () => {
    const state = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "session.fail",
      eventId: 1,
      data: {
        error_category: "provider_error",
        error:
          "Error code: 402 - {'detail': {'error': 'insufficient_credits', " +
          "'resource': 'tokens', 'requested': 14769, 'available': -31725}}",
        retryable: true, // proxy says transient; we override — retrying re-hits 402
      },
    });
    const errMsg = state.messages.find((m) => m.errorInfo);
    expect(errMsg?.errorInfo?.insufficientCredits).toBe(true);
    expect(errMsg?.errorInfo?.retryable).toBe(false);
    expect(errMsg?.errorInfo?.title).toBe("You're out of credits");
  });

  it("leaves a normal failure untouched", () => {
    const state = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "session.fail",
      eventId: 1,
      data: {
        error_category: "network",
        error: "connection reset",
        retryable: true,
      },
    });
    const errMsg = state.messages.find((m) => m.errorInfo);
    expect(errMsg?.errorInfo?.insufficientCredits).toBeFalsy();
    expect(errMsg?.errorInfo?.retryable).toBe(true);
  });

  it("appends loop.result as completed assistant output", () => {
    const running = {
      ...createInitialAgentChatState(),
      isRunning: false,
    };

    const next = applyAgentChatEvent(running, {
      type: "loop.result",
      eventId: 42,
      data: {
        content: "Loop says: done.",
        run_session_id: "run-1",
        scheduled_session_id: "schedule-1",
        run_completed_at: "2026-07-03T12:00:00Z",
      },
    });

    expect(next.isRunning).toBe(false);
    expect(next.lastEventId).toBe(42);
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]).toMatchObject({
      id: "evt-42",
      role: "assistant",
      content: "Loop says: done.",
      status: "complete",
      loopResult: {
        runSessionId: "run-1",
        scheduledSessionId: "schedule-1",
        runCompletedAt: "2026-07-03T12:00:00Z",
      },
    });
  });
});

describe("a /clear the harness ran after its user had typed more", () => {
  const said = (id: number, role: "user" | "assistant", content: string) => ({
    id: `evt-${id}`,
    role,
    content,
    createdAt: new Date("2026-01-01T00:00:00Z"),
    status: "complete" as const,
  });
  const cleared = (state: AgentChatState, answers?: number) =>
    applyAgentChatEvent(state, {
      type: "context.compact",
      eventId: 90,
      data: { strategy: "clear", compacted_messages: [], ...(answers === undefined ? {} : { answers }) },
    }).messages.map((message) => message.content);

  it("keeps the question typed behind the command, which the model answers next", () => {
    const state = withMessages([
      said(1, "user", "Open the report."),
      said(2, "assistant", "It is open."),
      said(3, "user", "/clear"),
      said(4, "user", "And Q1?"),
    ]);

    expect(cleared(state, 3)).toEqual(["And Q1?"]);
  });

  it("clears a message the turn under way read, with that turn", () => {
    const state = withMessages([
      said(1, "user", "Go on."),
      said(3, "user", "/clear"),
      said(4, "user", "Also check Q1."),
      said(5, "assistant", "Both done."),
      said(6, "user", "And Q2?"),
    ]);

    // The command waited for the turn: only what that turn did not read is left.
    expect(cleared(state, 3)).toEqual(["And Q2?"]);
  });

  it("clears everything when the compaction names no message", () => {
    const state = withMessages([said(1, "user", "Open the report."), said(2, "user", "And Q1?")]);

    expect(cleared(state)).toEqual([]);
  });
});
