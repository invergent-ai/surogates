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

function createAdapter(stream: FakeEventStream) {
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
    getWorkspaceDownloadUrl(input) {
      return `/api/v1/sessions/${input.sessionId}/workspace/download?path=${encodeURIComponent(input.path)}`;
    },
    openEventStream() {
      return stream;
    },
  } satisfies AgentChatAdapter;
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

  it("renders PDF workspace files without using the image preview", async () => {
    const stream = new FakeEventStream();
    const adapter: AgentChatAdapter = {
      ...createAdapter(stream),
      async getWorkspaceTree() {
        return {
          root: "workspace",
          entries: [
            {
              name: "report.pdf",
              path: "report.pdf",
              kind: "file" as const,
              size: 128,
            },
          ],
          truncated: false,
        };
      },
      async getWorkspaceFile() {
        return {
          path: "report.pdf",
          content: "JVBERi0xLjQKJcOkw7zDtsOfCg==",
          size: 18,
          mime_type: "application/pdf",
          encoding: "base64",
          truncated: false,
        };
      },
    };
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });

    const fileButton = Array.from(
      container.querySelectorAll<HTMLElement>('[role="treeitem"]'),
    ).find((element) => element.textContent?.includes("report.pdf"));
    expect(fileButton).toBeDefined();

    await act(async () => {
      fileButton!.click();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("report.pdf");
    expect(
      container.querySelector('div[aria-label="PDF viewer for report.pdf"]'),
    ).not.toBeNull();
    expect(
      container.querySelector('button[aria-label="Previous PDF page"]'),
    ).not.toBeNull();
    expect(
      container.querySelector('input[aria-label="Find in PDF"]'),
    ).not.toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("shows a clear error when a PDF workspace file is not base64 encoded", async () => {
    const stream = new FakeEventStream();
    const adapter: AgentChatAdapter = {
      ...createAdapter(stream),
      async getWorkspaceTree() {
        return {
          root: "workspace",
          entries: [
            {
              name: "broken.pdf",
              path: "broken.pdf",
              kind: "file" as const,
              size: 12,
            },
          ],
          truncated: false,
        };
      },
      async getWorkspaceFile() {
        return {
          path: "broken.pdf",
          content: "%PDF-\ufffd\n",
          size: 12,
          mime_type: "application/pdf",
          encoding: "utf-8",
          truncated: false,
        };
      },
    };
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });

    const fileButton = Array.from(
      container.querySelectorAll<HTMLElement>('[role="treeitem"]'),
    ).find((element) => element.textContent?.includes("broken.pdf"));
    expect(fileButton).toBeDefined();

    await act(async () => {
      fileButton!.click();
      await Promise.resolve();
    });

    expect(container.textContent).toContain(
      "PDF preview requires base64 file content.",
    );
    expect(
      container.querySelector('div[aria-label="PDF viewer for broken.pdf"]'),
    ).toBeNull();
  });

  it("does not show a blank PDF canvas when base64 content is invalid", async () => {
    const stream = new FakeEventStream();
    const adapter: AgentChatAdapter = {
      ...createAdapter(stream),
      async getWorkspaceTree() {
        return {
          root: "workspace",
          entries: [
            {
              name: "broken.pdf",
              path: "broken.pdf",
              kind: "file" as const,
              size: 12,
            },
          ],
          truncated: false,
        };
      },
      async getWorkspaceFile() {
        return {
          path: "broken.pdf",
          content: "%PDF-\ufffd\n",
          size: 12,
          mime_type: "application/pdf",
          encoding: "base64",
          truncated: false,
        };
      },
    };
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });

    const fileButton = Array.from(
      container.querySelectorAll<HTMLElement>('[role="treeitem"]'),
    ).find((element) => element.textContent?.includes("broken.pdf"));
    expect(fileButton).toBeDefined();

    await act(async () => {
      fileButton!.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("PDF preview data is not valid base64.");
    expect(
      container.querySelector('div[aria-label="PDF viewer for broken.pdf"]'),
    ).toBeNull();
  });

  it("keeps the workspace file viewer closed after clicking close", async () => {
    const stream = new FakeEventStream();
    const adapter = createAdapter(stream);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });

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

    expect(container.textContent).toContain("print('hi')");

    const closeButton = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Close file"]',
    );
    expect(closeButton).not.toBeNull();

    await act(async () => {
      closeButton!.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).not.toContain("print('hi')");
    expect(
      container.querySelector('button[aria-label="Close file"]'),
    ).toBeNull();
  });

  it("disables the composer and workspace upload when chat is disabled", async () => {
    const stream = new FakeEventStream();
    const adapter = createAdapter(stream);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" disabled />);
      await Promise.resolve();
    });

    const composer = container.querySelector<HTMLTextAreaElement>("textarea");
    expect(composer).not.toBeNull();
    expect(composer!.disabled).toBe(true);

    const uploadButton = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Upload files"]',
    );
    expect(uploadButton).not.toBeNull();
    expect(uploadButton!.disabled).toBe(true);
  });

  it("renders consult_expert as a dedicated expert block instead of raw JSON", async () => {
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
      stream.emit("tool.call", 1, {
        tool_call_id: "expert-1",
        name: "consult_expert",
        arguments: {
          expert: "Architecture reviewer",
          question: "Review the example app architecture.",
        },
      });
      stream.emit("tool.result", 2, {
        tool_call_id: "expert-1",
        content: "The architecture is appropriate for an SDK example.",
      });
      stream.emit("expert.result", 3, {
        summary: "The architecture is appropriate for an SDK example.",
      });
    });

    expect(container.textContent).toContain("Consulted expert");
    expect(container.textContent).toContain("Architecture reviewer");
    expect(container.textContent).toContain(
      "The architecture is appropriate for an SDK example.",
    );
    expect(container.textContent).not.toContain('"question"');
  });

  it("renders skill management and coordinator tools as dedicated blocks", async () => {
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
      stream.emit("tool.call", 1, {
        tool_call_id: "skill-1",
        name: "skill_manage",
        arguments: {
          action: "patch",
          name: "example-skill",
          old_string: "old",
          new_string: "new",
        },
      });
      stream.emit("tool.result", 2, {
        tool_call_id: "skill-1",
        content: JSON.stringify({
          success: true,
          message: "Skill updated.",
        }),
      });
      stream.emit("tool.call", 3, {
        tool_call_id: "worker-1",
        name: "spawn_worker",
        arguments: {
          agent_type: "reviewer",
          goal: "Review the example app.",
        },
      });
      stream.emit("tool.result", 4, {
        tool_call_id: "worker-1",
        content: JSON.stringify({
          worker_id: "worker-demo",
          status: "queued",
        }),
      });
    });

    expect(container.textContent).toContain("Patch skill");
    expect(container.textContent).toContain("example-skill");
    expect(container.textContent).toContain("Spawned worker");
    expect(container.textContent).toContain("worker-demo");
    expect(container.textContent).not.toContain("skill_manage");
    expect(container.textContent).not.toContain("spawn_worker");
  });

  it("does not show a previous session workspace after switching to a new chat", async () => {
    const stream = new FakeEventStream();
    let resolveTree: ((value: Awaited<ReturnType<AgentChatAdapter["getWorkspaceTree"]>>) => void) | null = null;
    const adapter = {
      ...createAdapter(stream),
      async getWorkspaceTree() {
        return await new Promise<Awaited<ReturnType<AgentChatAdapter["getWorkspaceTree"]>>>(
          (resolve) => {
            resolveTree = resolve;
          },
        );
      },
    };
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId={null} />);
      await Promise.resolve();
    });

    await act(async () => {
      resolveTree?.({
        root: "workspace",
        entries: [
          {
            name: "old-session.txt",
            path: "old-session.txt",
            kind: "file" as const,
            size: 12,
          },
        ],
        truncated: false,
      });
      await Promise.resolve();
    });

    expect(container.textContent).toContain("No workspace files");
    expect(container.textContent).not.toContain("old-session.txt");
  });
});
