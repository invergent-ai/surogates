/**
 * Synthetic turn-summary artifact cards must resolve to a rich
 * ArtifactBlock, not degrade to a plain text bullet.
 *
 * Regression for: deriveFileArtifactsFromMessages used the artifact
 * *name* as the ref, but TurnSummaryCard.resolveArtifactRef matches on
 * artifact_id — so derived create_artifact cards never resolved. The
 * derivation now keys off the artifact.created system marker (which
 * carries artifact_id), so resolution succeeds and ArtifactBlock mounts
 * (calling adapter.getArtifact with the real id).
 */
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatThread } from "../src/components/chat/chat-thread";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type { AgentChatAdapter, ChatMessage } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const getArtifact = vi.fn().mockResolvedValue(null);

function adapterStub(): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    listSessions: vi.fn().mockResolvedValue({ sessions: [], total: 0 }),
    createSession: vi.fn(),
    getSession: vi.fn(),
    sendMessage: vi.fn(),
    getArtifact,
    openEventStream: vi.fn(() => ({
      addEventListener: vi.fn(),
      close: vi.fn(),
      onerror: null,
    })),
  } as unknown as AgentChatAdapter;
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  getArtifact.mockClear();
});

function mount(node: ReactElement): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <AgentChatAdapterProvider value={{ adapter: adapterStub(), sessionId: "s-1" }}>
        <TooltipProvider>{node}</TooltipProvider>
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

const noop = () => Promise.resolve();

describe("synthetic artifact summary resolution", () => {
  it("resolves a derived create_artifact card by artifact_id (rich block, not text)", () => {
    const messages: ChatMessage[] = [
      {
        id: "user-1",
        role: "user",
        content: "make a chart",
        createdAt: new Date(),
        status: "complete",
      } as ChatMessage,
      {
        id: "iter-0",
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
            args: JSON.stringify({ name: "Revenue Chart" }),
            status: "complete",
            result: "{}",
          },
        ],
      } as ChatMessage,
      {
        id: "sys-artifact",
        role: "system",
        content: "Revenue Chart",
        createdAt: new Date(),
        status: "complete",
        systemKind: "artifact",
        systemMeta: {
          artifact_id: "art-xyz",
          name: "Revenue Chart",
          kind: "chart",
          version: 1,
        },
      } as ChatMessage,
      {
        id: "final",
        role: "assistant",
        content: "Done.",
        createdAt: new Date(),
        status: "complete",
        turnId: "t-1",
        iterationIndex: 1,
      } as ChatMessage,
    ];

    mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={false}
        terminal={true}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );

    // The ArtifactBlock resolved the ref to the real artifact_id and
    // fetched it — proof the card rendered rich, not as plain text.
    expect(getArtifact).toHaveBeenCalledWith(
      expect.objectContaining({ artifactId: "art-xyz" }),
    );
  });
});
