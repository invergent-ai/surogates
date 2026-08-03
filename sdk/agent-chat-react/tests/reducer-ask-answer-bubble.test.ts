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
    expect(next.messages[0]?.toolCalls?.[0]?.askUserQuestionAnswers).toHaveLength(2);
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
