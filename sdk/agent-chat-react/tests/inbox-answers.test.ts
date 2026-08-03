// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The rules deciding what an inbox answer says and whether it departed
// from the menu, exercised directly rather than through the panel.

import { describe, expect, it } from "vitest";
import {
  answerText,
  buildInboxResponse,
  isOtherAnswer,
  parseInboxQuestions,
} from "../src/components/inbox/inbox-answers";
import type { InboxQuestion } from "../src/components/inbox/inbox-answers";
import type { AgentChatInboxItem } from "../src/types";

function item(questions: unknown): AgentChatInboxItem {
  return { payload: { questions } } as unknown as AgentChatInboxItem;
}

const OPEN: InboxQuestion = {
  prompt: "What is the deploy tag?",
  allowOther: true,
};
const MENU: InboxQuestion = {
  prompt: "Which region?",
  choices: [{ label: "eu-west" }, { label: "us-east" }],
  allowOther: true,
};

describe("parseInboxQuestions", () => {
  it("treats a missing allow_other as permitting an off-menu answer", () => {
    const [question] = parseInboxQuestions(item([{ prompt: "Which region?" }]));
    expect(question?.allowOther).toBe(true);
  });

  it("honours an explicit closed menu", () => {
    const [question] = parseInboxQuestions(
      item([{ prompt: "Which region?", allow_other: false }]),
    );
    expect(question?.allowOther).toBe(false);
  });

  it("drops entries with no prompt and choices with no label", () => {
    const questions = parseInboxQuestions(
      item([
        { prompt: "" },
        { prompt: "Pick", choices: [{ label: "a" }, { description: "x" }] },
      ]),
    );
    expect(questions).toHaveLength(1);
    expect(questions[0]?.choices).toEqual([
      { label: "a", description: undefined },
    ]);
  });

  it("returns nothing when the payload carries no question array", () => {
    expect(parseInboxQuestions(item(undefined))).toEqual([]);
  });
});

describe("isOtherAnswer", () => {
  it("is false for an open question however it was answered", () => {
    // No menu means nothing to depart from.
    expect(isOtherAnswer(OPEN, { typed: "v1.2.3", useOther: true })).toBe(false);
    expect(isOtherAnswer(OPEN, undefined)).toBe(false);
  });

  it("distinguishes a menu pick from a typed answer", () => {
    expect(isOtherAnswer(MENU, { picked: "eu-west" })).toBe(false);
    expect(isOtherAnswer(MENU, { typed: "frankfurt", useOther: true })).toBe(
      true,
    );
  });
});

describe("answerText", () => {
  it("reads the picked label for a menu question", () => {
    expect(answerText(MENU, { picked: "eu-west", typed: "stale" })).toBe(
      "eu-west",
    );
  });

  it("reads the typed value once the user switched to Other", () => {
    expect(
      answerText(MENU, { picked: "eu-west", typed: "frankfurt", useOther: true }),
    ).toBe("frankfurt");
  });

  it("trims, and reports empty for an untouched draft", () => {
    expect(answerText(OPEN, { typed: "   " })).toBe("");
    expect(answerText(OPEN, undefined)).toBe("");
  });
});

describe("buildInboxResponse", () => {
  it("shapes an off-menu answer", () => {
    expect(buildInboxResponse(MENU, { typed: " frankfurt ", useOther: true }))
      .toEqual({
        question: "Which region?",
        answer: "frankfurt",
        is_other: true,
      });
  });

  it("shapes an open-question answer", () => {
    expect(buildInboxResponse(OPEN, { typed: "v1.2.3" })).toEqual({
      question: "What is the deploy tag?",
      answer: "v1.2.3",
      is_other: false,
    });
  });
});
