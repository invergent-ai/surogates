// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A one-question ask_user_question is a conversational turn, so its
// answer belongs in the thread as the user's own message.  The server
// emits only ``ask_user_question.response`` for it -- the /messages
// route returns early once it resolves a pending question, so no
// ``user.message`` follows -- which leaves the reducer as the single
// place both answer paths (typed reply, tapped chip) converge.

import { describe, expect, it } from "vitest";
import {
  applyAgentChatEvent,
  createInitialAgentChatState,
} from "../src/runtime/reducer";
import type { AgentChatRuntimeEvent, AgentChatState } from "../src/types";

function withMessages(messages: AgentChatState["messages"]): AgentChatState {
  return { ...createInitialAgentChatState(), messages };
}

function askTurn(questions: unknown[]): AgentChatState["messages"][number] {
  return {
    id: "evt-1",
    role: "assistant",
    content: "",
    createdAt: new Date("2026-01-01T00:00:00Z"),
    status: "streaming",
    toolCalls: [
      {
        id: "tc-1",
        toolName: "ask_user_question",
        args: JSON.stringify({ questions }),
        status: "running",
      },
    ],
  };
}

function optimisticUserMessage(content: string) {
  return {
    id: "local-1700000000000-abc123",
    role: "user" as const,
    content,
    createdAt: new Date("2026-01-01T00:00:01Z"),
    status: "complete" as const,
  };
}

function response(
  responses: { question: string; answer: string; is_other: boolean }[],
  eventId = 42,
): AgentChatRuntimeEvent {
  return {
    type: "ask_user_question.response",
    eventId,
    data: { tool_call_id: "tc-1", responses },
  };
}

const ONE_QUESTION = [{ prompt: "What subjects do you like?" }];

describe("conversational ask answers become user messages", () => {
  it("appends the answer as a user message when nothing optimistic exists", () => {
    // The tapped-chip path: submitting through the widget sends no
    // message of its own, so there is nothing to adopt.
    const state = withMessages([askTurn(ONE_QUESTION)]);

    const next = applyAgentChatEvent(
      state,
      response([
        { question: "What subjects do you like?", answer: "computers", is_other: false },
      ]),
    );

    expect(next.messages).toHaveLength(2);
    expect(next.messages[1]).toMatchObject({
      id: "evt-42",
      role: "user",
      content: "computers",
      status: "complete",
    });
    // The tool call still carries the structured answer for consumers
    // that read it (inbox, operator review).
    expect(next.messages[0]?.toolCalls?.[0]?.askUserQuestionAnswers).toEqual([
      { question: "What subjects do you like?", answer: "computers", is_other: false },
    ]);
  });

  it("adopts the optimistic composer message instead of duplicating it", () => {
    const state = withMessages([
      askTurn(ONE_QUESTION),
      optimisticUserMessage("computers"),
    ]);

    const next = applyAgentChatEvent(
      state,
      response([
        { question: "What subjects do you like?", answer: "computers", is_other: false },
      ]),
    );

    expect(next.messages).toHaveLength(2);
    expect(next.messages[1]?.id).toBe("evt-42");
    expect(next.messages[1]?.content).toBe("computers");
  });

  it("adopts the optimistic message even when the answer was canonicalised", () => {
    // Typing "yes" against a choice labelled "Yes" comes back as "Yes".
    // Matching on text would strand the optimistic copy beside the
    // synthesised one.
    const state = withMessages([
      askTurn([
        { prompt: "Ready?", choices: [{ label: "Yes" }, { label: "No" }] },
      ]),
      optimisticUserMessage("yes"),
    ]);

    const next = applyAgentChatEvent(
      state,
      response([{ question: "Ready?", answer: "Yes", is_other: false }]),
    );

    expect(next.messages).toHaveLength(2);
    expect(next.messages[1]?.content).toBe("Yes");
  });

  it("does not append twice when the event is redelivered", () => {
    // An SSE reconnect replays from the last acknowledged cursor.
    const state = withMessages([askTurn(ONE_QUESTION)]);
    const once = applyAgentChatEvent(
      state,
      response([
        { question: "What subjects do you like?", answer: "computers", is_other: false },
      ]),
    );
    const twice = applyAgentChatEvent(
      once,
      response([
        { question: "What subjects do you like?", answer: "computers", is_other: false },
      ]),
    );

    expect(twice.messages).toHaveLength(2);
  });

  it("leaves a multi-question batch to its recap block", () => {
    // Batch answers are recorded per question in the widget; emitting
    // them as chat messages would misrepresent one form submission as
    // several separate replies.
    const state = withMessages([
      askTurn([{ prompt: "Which approach?" }, { prompt: "Which region?" }]),
    ]);

    const next = applyAgentChatEvent(
      state,
      response([
        { question: "Which approach?", answer: "Cron", is_other: false },
        { question: "Which region?", answer: "eu-west", is_other: false },
      ]),
    );

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]?.toolCalls?.[0]?.askUserQuestionAnswers).toEqual([
      { question: "Which approach?", answer: "Cron", is_other: false },
      { question: "Which region?", answer: "eu-west", is_other: false },
    ]);
  });

  it("ignores an empty answer rather than adding a blank bubble", () => {
    const state = withMessages([askTurn(ONE_QUESTION)]);

    const next = applyAgentChatEvent(
      state,
      response([
        { question: "What subjects do you like?", answer: "   ", is_other: false },
      ]),
    );

    expect(next.messages).toHaveLength(1);
  });

  it("ignores a second response for the same tool call", () => {
    // Both answer surfaces are live at once for a conversational ask,
    // and the widget's respond route emits without checking whether the
    // composer already answered. The worker takes the first event and
    // ignores the rest; the thread must not show the reply twice.
    const state = withMessages([askTurn(ONE_QUESTION)]);
    const first = applyAgentChatEvent(
      state,
      response(
        [{ question: "What subjects do you like?", answer: "computers", is_other: false }],
        42,
      ),
    );
    const second = applyAgentChatEvent(
      first,
      response(
        [{ question: "What subjects do you like?", answer: "sports", is_other: false }],
        43,
      ),
    );

    expect(second.messages.filter((m) => m.role === "user")).toHaveLength(1);
    expect(second.messages[1]?.content).toBe("computers");
    // The tool call must agree with the bubble. Recording the losing
    // submission here would leave the thread saying one thing and the
    // operator-facing recap another.
    expect(
      second.messages[0]?.toolCalls?.[0]?.askUserQuestionAnswers,
    ).toEqual([
      { question: "What subjects do you like?", answer: "computers", is_other: false },
    ]);
  });

  it("leaves a failed send standing instead of adopting it", () => {
    // markSendError keeps the local- id and appends the failure to the
    // body. Adopting it would erase the notice and attach the answer to
    // a message that never reached the server.
    const failed = {
      ...optimisticUserMessage("computers\n\n*Failed to send: network*"),
      status: "error" as const,
    };
    const state = withMessages([askTurn(ONE_QUESTION), failed]);

    const next = applyAgentChatEvent(
      state,
      response([
        { question: "What subjects do you like?", answer: "sports", is_other: false },
      ]),
    );

    expect(next.messages).toHaveLength(3);
    expect(next.messages[1]?.status).toBe("error");
    expect(next.messages[1]?.content).toContain("Failed to send");
    expect(next.messages[2]?.content).toBe("sports");
  });

  it("adopts the reply, not a follow-up sent before the event arrived", () => {
    // The composer re-opens the moment the optimistic message lands, so
    // a user can send again before the answer round-trips. Taking the
    // newest local message would overwrite that follow-up with the
    // answer and strand the real reply.
    const state = withMessages([
      askTurn(ONE_QUESTION),
      optimisticUserMessage("computers"),
      { ...optimisticUserMessage("actually, also sports"), id: "local-2" },
    ]);

    const next = applyAgentChatEvent(
      state,
      response([
        { question: "What subjects do you like?", answer: "computers", is_other: false },
      ]),
    );

    expect(next.messages).toHaveLength(3);
    expect(next.messages[1]?.id).toBe("evt-42");
    expect(next.messages[1]?.content).toBe("computers");
    expect(next.messages[2]?.content).toBe("actually, also sports");
  });

  it("steps over a failed send to adopt the retry", () => {
    const failed = {
      ...optimisticUserMessage("computers\n\n*Failed to send: network*"),
      status: "error" as const,
    };
    const state = withMessages([
      askTurn(ONE_QUESTION),
      failed,
      { ...optimisticUserMessage("computers"), id: "local-retry" },
    ]);

    const next = applyAgentChatEvent(
      state,
      response([
        { question: "What subjects do you like?", answer: "computers", is_other: false },
      ]),
    );

    expect(next.messages).toHaveLength(3);
    expect(next.messages[1]?.status).toBe("error");
    expect(next.messages[2]?.id).toBe("evt-42");
  });

  it("does not adopt a user message the server already confirmed", () => {
    // A promoted (evt-) user message belongs to an earlier turn: the
    // answer is a new message, not a rewrite of that one.
    const state = withMessages([
      {
        id: "evt-0",
        role: "user",
        content: "ready!",
        createdAt: new Date("2026-01-01T00:00:00Z"),
        status: "complete",
      },
      askTurn(ONE_QUESTION),
    ]);

    const next = applyAgentChatEvent(
      state,
      response([
        { question: "What subjects do you like?", answer: "computers", is_other: false },
      ]),
    );

    expect(next.messages).toHaveLength(3);
    expect(next.messages[0]?.content).toBe("ready!");
    expect(next.messages[2]?.content).toBe("computers");
  });
});
