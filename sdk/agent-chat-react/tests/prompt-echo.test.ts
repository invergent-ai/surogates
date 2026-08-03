// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A conversational ask suppresses its prompt when the agent already
// wrote the question into the message body. Getting this wrong in the
// permissive direction hides the question entirely, so the rule is
// pinned here rather than only observed through rendering.

import { describe, expect, it } from "vitest";
import { promptEchoedInContent } from "../src/components/chat/tools/ask-user-question-tool";

describe("promptEchoedInContent", () => {
  it("suppresses a prompt the body ends with", () => {
    expect(
      promptEchoedInContent(
        "You said you want the foundations. What would feel like real progress?",
        "What would feel like real progress?",
      ),
    ).toBe(true);
  });

  it("ignores markdown emphasis, case and whitespace runs", () => {
    expect(
      promptEchoedInContent(
        "Right.  **What   happens at 0 degrees?**",
        "what happens at 0 degrees?",
      ),
    ).toBe(true);
  });

  it("keeps a prompt the body only quotes mid-way", () => {
    // Rendering it twice is a stutter; suppressing on a coincidence
    // leaves nothing to answer, so the tie goes to showing it.
    const prompt = "Do the atoms stop completely, or keep slowing down?";
    expect(
      promptEchoedInContent(`${prompt} Take your time.`, prompt),
    ).toBe(false);
  });

  it("keeps a short prompt that only coincidentally appears", () => {
    // "sure?" falls inside "I'm not sure?" -- suppressing here would
    // leave the user with no visible question at all.
    expect(
      promptEchoedInContent("I'm not sure? Let me think first.", "Sure?"),
    ).toBe(false);
  });

  it("keeps the prompt when the body says something else", () => {
    expect(
      promptEchoedInContent("Good. Let's build on that.", "What happens next?"),
    ).toBe(false);
  });

  it("keeps the prompt when there is no body", () => {
    expect(promptEchoedInContent(undefined, "Ready?")).toBe(false);
    expect(promptEchoedInContent("", "Ready?")).toBe(false);
  });
});
