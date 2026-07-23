// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A structured artifact must render exactly ONE live ArtifactBlock per
// thread. Historically the artifact.created marker mounted a block AND
// the TurnSummaryCard mounted a second one for the same ref — html/svg
// animations visibly played twice and every payload was fetched twice.

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatThread } from "../src/components/chat/chat-thread";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type { AgentChatAdapter, ChatMessage } from "../src/types";

function adapterStub(): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    listSessions: vi.fn().mockResolvedValue({ sessions: [], total: 0 }),
    createSession: vi.fn(),
    getSession: vi.fn(),
    sendMessage: vi.fn(),
    openEventStream: vi.fn(() => ({
      addEventListener: vi.fn(),
      close: vi.fn(),
      onerror: null,
    })),
    getArtifact: vi.fn().mockResolvedValue({
      kind: "markdown",
      spec: { content: "Artifact body" },
    }),
  } as unknown as AgentChatAdapter;
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function mount(node: ReactElement): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <AgentChatAdapterProvider
        value={{ adapter: adapterStub(), sessionId: "s-1" }}
      >
        <TooltipProvider>{node}</TooltipProvider>
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

const noop = () => {};

function artifactTurnMessages(): ChatMessage[] {
  return [
    {
      id: "user-1",
      role: "user",
      content: "make me an animation",
      createdAt: new Date(),
      status: "complete",
    },
    // Mirrors the real event order: the tool iteration, then the
    // artifact.created marker, then the final response carrying the
    // turn summary — all inside one turn group.
    {
      id: "asst-1",
      role: "assistant",
      content: "",
      createdAt: new Date(),
      status: "complete",
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        {
          id: "c1",
          toolName: "create_artifact",
          args: JSON.stringify({ name: "Particle box" }),
          status: "complete",
          result: "{}",
        },
      ],
    },
    {
      id: "sys-1",
      role: "system",
      content: "Particle box",
      createdAt: new Date(),
      status: "complete",
      systemKind: "artifact",
      systemMeta: {
        artifact_id: "art-1",
        name: "Particle box",
        kind: "html",
        version: 1,
      },
    },
    {
      id: "asst-2",
      role: "assistant",
      content: "Here is your animation.",
      createdAt: new Date(),
      status: "complete",
      turnId: "t-1",
      iterationIndex: 1,
      turnSummary: {
        turnId: "t-1",
        recap: "Built an interactive particle animation.",
        artifacts: [
          { kind: "artifact", label: "Particle box", ref: "art-1" },
        ],
      },
    },
  ];
}

describe("artifact single-render", () => {
  for (const viewMode of ["simple", "expert"] as const) {
    it(`mounts exactly one live ArtifactBlock in ${viewMode} mode`, () => {
      const dom = mount(
        <ChatThread
          sessionId="s-1"
          messages={artifactTurnMessages()}
          isRunning={false}
          terminal={true}
          onSend={noop}
          onStop={noop}
          viewMode={viewMode}
        />,
      );
      const liveBlocks = dom.querySelectorAll("[data-artifact-anchor]");
      expect(liveBlocks).toHaveLength(1);
      expect(liveBlocks[0]?.getAttribute("data-artifact-anchor")).toBe("art-1");
    });
  }

  it("renders the summary's artifact as a reference card, not a second block", () => {
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={artifactTurnMessages()}
        isRunning={false}
        terminal={true}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    // The recap card still surfaces the artifact — as a compact
    // clickable reference with the artifact's name and kind label.
    const refButtons = [...dom.querySelectorAll("button")].filter((b) =>
      b.textContent?.includes("HTML preview"),
    );
    expect(refButtons).toHaveLength(1);
    expect(refButtons[0]?.textContent).toContain("Particle box");
  });
});
