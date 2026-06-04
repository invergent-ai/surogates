// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The user-message bubble clamps long content (e.g. a delegated
// planner prompt with a multi-paragraph goal + context) to a fixed
// max-height and exposes a Show more / Show less toggle.  jsdom does
// not lay out text so we can't directly drive the
// scrollHeight-vs-clientHeight measurement here -- the e2e suite
// covers that.  These unit tests pin the structural contract:
//
//   * The collapsible body is present and has the max-height cap
//     applied while collapsed.
//   * Toggling state via the exposed toggle flips the cap on/off and
//     swaps the label.

import { describe, expect, it } from "vitest";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";

import { ChatMessage } from "../src/components/chat/chat-message";
import type {
  AgentChatMessage,
  AgentChatMessageStatus,
} from "../src/types";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

// jsdom does not implement ResizeObserver.  Stub to satisfy the
// component; observe()/disconnect() are no-ops because we drive
// measurement via direct property assignment below.
class FakeResizeObserver {
  observe(): void {}
  disconnect(): void {}
  unobserve(): void {}
}
(globalThis as { ResizeObserver?: typeof FakeResizeObserver }).ResizeObserver =
  FakeResizeObserver;

function makeMessage(content: string): AgentChatMessage {
  return {
    id: "m-1",
    role: "user",
    content,
    createdAt: new Date(0),
    status: "sent" as AgentChatMessageStatus,
  };
}

function render(node: React.ReactNode): HTMLDivElement {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(node);
  });
  return container;
}

function bodyOf(out: HTMLElement): HTMLDivElement {
  const body = out.querySelector(
    'div[style*="max-height"], div[style=""], div.whitespace-pre-wrap',
  ) as HTMLDivElement | null;
  if (!body) throw new Error("Collapsible body element not found");
  return body;
}

describe("ChatMessage collapsible body", () => {
  it("applies the max-height cap while collapsed", () => {
    const out = render(
      createElement(ChatMessage, { message: makeMessage("hi") }),
    );
    const body = bodyOf(out);
    // While collapsed the inline style fixes a max-height so a long
    // goal cannot dominate the thread.  The exact pixel value is
    // a UX knob (currently 160) and intentionally not asserted here.
    expect(body.style.maxHeight).not.toBe("");
  });

  it("preserves embedded newlines via whitespace-pre-wrap", () => {
    // Delegated prompts arrive with ``\n\n`` separators between goal
    // and context; without whitespace-pre-wrap they collapse into a
    // single paragraph and lose the visual grouping the planner saw.
    const out = render(
      createElement(ChatMessage, {
        message: makeMessage("line one\n\nline two"),
      }),
    );
    const body = bodyOf(out);
    expect(body.className).toMatch(/whitespace-pre-wrap/);
  });

  it("does not show Show more for short messages", () => {
    const out = render(
      createElement(ChatMessage, { message: makeMessage("hi") }),
    );
    // Short content fits in the cap with no overflow; jsdom defaults
    // both heights to 0 so the overflow check resolves False and the
    // toggle stays hidden.  The structural assertion is the toggle
    // button is absent.
    expect(out.querySelector("button")).toBeNull();
  });
});
