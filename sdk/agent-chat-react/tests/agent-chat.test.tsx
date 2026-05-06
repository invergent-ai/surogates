import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { AgentChat } from "../src/agent-chat";
import type {
  AgentChatAdapter,
  AgentChatEventStream,
  AgentChatEventType,
  AgentChatSession,
  AgentChatSseMessageEvent,
} from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

class FakeEventStream implements AgentChatEventStream {
  onerror: (() => void) | null = null;
  readonly listeners = new Map<
    AgentChatEventType,
    Array<(event: AgentChatSseMessageEvent) => void>
  >();

  addEventListener(
    type: AgentChatEventType,
    listener: (event: AgentChatSseMessageEvent) => void,
  ): void {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  close(): void {}

  emit(type: AgentChatEventType, eventId: number, data: Record<string, unknown>) {
    const event = {
      data: JSON.stringify(data),
      lastEventId: String(eventId),
    };
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

function createAdapter(stream: FakeEventStream): AgentChatAdapter {
  return {
    async listSessions() {
      return { sessions: [], total: 0 };
    },
    async createSession() {
      return session("created");
    },
    async getSession(input) {
      return session(input.sessionId);
    },
    async sendMessage() {
      return { eventId: 1, status: "accepted" };
    },
    async pauseSession() {},
    async retrySession(input) {
      return session(input.sessionId);
    },
    async getArtifact() {
      return {
        meta: {
          artifact_id: "a-1",
          session_id: "s-1",
          name: "Report",
          kind: "markdown",
          version: 1,
          size: 12,
          created_at: "2026-01-01T00:00:00Z",
        },
        kind: "markdown",
        spec: { content: "Artifact body" },
      };
    },
    async submitClarifyResponse() {
      return { eventId: 1 };
    },
    openEventStream() {
      return stream;
    },
  };
}

function session(id: string): AgentChatSession {
  return { id, status: "active" };
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) {
    act(() => root?.unmount());
  }
  root = null;
  container?.remove();
  container = null;
});

describe("AgentChat", () => {
  it("renders messages received from the runtime stream", () => {
    const stream = new FakeEventStream();
    const adapter = createAdapter(stream);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    act(() => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
    });

    act(() => {
      stream.emit("user.message", 1, { content: "hello from stream" });
      stream.emit("llm.response", 2, { message: { content: "assistant reply" } });
    });

    expect(container.textContent).toContain("hello from stream");
    expect(container.textContent).toContain("assistant reply");
  });
});
