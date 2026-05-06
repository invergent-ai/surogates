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
    async listSlashCommands() {
      return [
        {
          value: "/review",
          label: "/review",
          description: "Review the current work",
        },
      ];
    },
    async getWorkspaceTree() {
      return {
        root: "workspace",
        entries: [
          { name: "src", path: "src", kind: "dir" as const, children: [
            { name: "main.py", path: "src/main.py", kind: "file" as const, size: 12 },
          ] },
        ],
        truncated: false,
      };
    },
    async getWorkspaceFile() {
      return {
        path: "src/main.py",
        content: "print('hi')",
        size: 11,
        mime_type: "text/x-python",
        encoding: "utf-8" as const,
        truncated: false,
      };
    },
    async uploadWorkspaceFile() {
      return { path: "uploaded.txt", size: 4 };
    },
    async deleteWorkspaceFile() {},
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
  it("renders messages received from the runtime stream", async () => {
    const stream = new FakeEventStream();
    const adapter = createAdapter(stream);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });

    act(() => {
      stream.emit("user.message", 1, { content: "hello from stream" });
      stream.emit("llm.response", 2, { message: { content: "assistant reply" } });
    });

    expect(container.textContent).toContain("hello from stream");
    expect(container.textContent).toContain("assistant reply");
  });

  it("shows slash commands from the adapter from the composer command menu", async () => {
    const stream = new FakeEventStream();
    const adapter = createAdapter(stream);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
    });

    const trigger = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Slash commands"]',
    );
    expect(trigger).not.toBeNull();

    await act(async () => {
      trigger!.click();
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("/review");
    expect(document.body.textContent).toContain("Review the current work");
  });

  it("packages the workspace panel and file viewer with the chat component", async () => {
    const stream = new FakeEventStream();
    const adapter = createAdapter(stream);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Workspace");
    expect(container.textContent).toContain("main.py");

    const fileButton = Array.from(
      container.querySelectorAll<HTMLElement>('[role="treeitem"]'),
    )
      .reverse()
      .find((element) => element.textContent?.includes("main.py"));
    expect(fileButton).toBeDefined();

    await act(async () => {
      fileButton!.click();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("src/main.py");
    expect(container.textContent).toContain("print('hi')");
  });
});
