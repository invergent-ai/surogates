import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
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

function createAdapter(
  stream: FakeEventStream,
  options: { session?: AgentChatSession } = {},
) {
  return {
    ...NO_BROWSER_ADAPTER,
    async listSessions() {
      return { sessions: [], total: 0 };
    },
    async createSession() {
      return session("created");
    },
    async getSession(input) {
      return options.session ?? session(input.sessionId);
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

function session(
  id: string,
  overrides: Partial<AgentChatSession> = {},
): AgentChatSession {
  return { id, status: "active", ...overrides };
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

  it("shows a loading state instead of the new-chat empty state while session history loads", async () => {
    const stream = new FakeEventStream();
    const adapter = createAdapter(stream);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });

    expect(container.textContent).not.toContain("Start a conversation");
    expect(container.textContent).toContain("Loading conversation");

    act(() => {
      stream.emit("user.message", 1, { content: "previous session message" });
    });

    expect(container.textContent).toContain("previous session message");
    expect(container.textContent).not.toContain("Loading conversation");
  });

  it("shows builtin and adapter slash commands from the composer command menu", async () => {
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

    expect(document.body.textContent).toContain("/loop");
    expect(document.body.textContent).toContain("Schedule recurring prompt");
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

  it("renders only WorkspacePanel when no browser is provisioned", async () => {
    const stream = new FakeEventStream();
    const adapter = createAdapter(stream);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });

    expect(container.querySelector('[data-testid="workspace-panel"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="browser-pane"]')).toBeNull();
  });

  it("stacks BrowserPane above WorkspacePanel when browser is live", async () => {
    const stream = new FakeEventStream();
    const adapter = {
      ...createAdapter(stream),
      browserLiveViewUrl() {
        return "about:blank#browser-live";
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
      stream.emit("browser.provisioned", 10, { session_id: "s-1" });
      await Promise.resolve();
    });

    const browserPane = container.querySelector('[data-testid="browser-pane"]');
    const layout = container.querySelector('[data-testid="agent-chat-layout"]');
    const chatPanel = container.querySelector('[data-testid="chat-panel"]');
    const workspacePanel = container.querySelector('[data-testid="workspace-panel"]');
    const browserPanel = container.querySelector('[data-testid="browser-panel"]');
    const rightStack = container.querySelector('[data-testid="right-stack"]');
    const workspacePanelFrame = container.querySelector(
      '[data-testid="workspace-panel-frame"]',
    );
    expect(browserPane).not.toBeNull();
    expect((layout as HTMLElement | null)?.style.direction).toBe("ltr");
    expect(layout?.className).toContain("relative");
    expect((chatPanel as HTMLElement | null)?.style.right).toBe("440px");
    expect((rightStack as HTMLElement | null)?.style.right).toBe("0px");
    expect((rightStack as HTMLElement | null)?.style.width).toBe("440px");
    expect(chatPanel?.className).toContain("absolute");
    expect(chatPanel?.className).toContain("flex");
    expect(chatPanel?.className).toContain("flex-col");
    expect(chatPanel?.className).toContain("min-h-0");
    expect(rightStack?.className).toContain("absolute");
    expect(browserPanel?.className).toContain("w-full");
    expect(browserPanel?.className).toContain("h-1/2");
    expect(browserPanel?.className).toContain("overflow-hidden");
    expect(workspacePanel?.className).toContain("w-full");
    expect(workspacePanelFrame?.className).toContain("h-1/2");
    expect(workspacePanelFrame?.className).toContain("w-full");
    expect(workspacePanelFrame?.className).toContain("overflow-hidden");
    expect(workspacePanel).not.toBeNull();
    expect(
      browserPane!.compareDocumentPosition(workspacePanel!) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("opens the workspace by default and can collapse and expand it", async () => {
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

    const collapseButton = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Collapse workspace"]',
    );
    expect(collapseButton).not.toBeNull();

    await act(async () => {
      collapseButton!.click();
      await Promise.resolve();
    });

    expect(container.textContent).not.toContain("main.py");
    expect(container.textContent).not.toContain("Workspace");

    expect(
      container.querySelector<HTMLButtonElement>(
        'button[aria-label="Expand workspace"]',
      ),
    ).toBeNull();

    const expandStrip = container.querySelector<HTMLElement>(
      '[role="button"][aria-label="Expand workspace"]',
    );
    expect(expandStrip).not.toBeNull();
    expect(expandStrip!.querySelector("svg")).not.toBeNull();

    await act(async () => {
      expandStrip!.click();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Workspace");
    expect(container.textContent).toContain("main.py");
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

  it("shows a visible explanation for failed tool result dots", async () => {
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
        tool_call_id: "crawl-1",
        name: "web_crawl",
        arguments: {
          url: "https://example.com",
        },
      });
      stream.emit("tool.result", 2, {
        tool_call_id: "crawl-1",
        content: JSON.stringify({
          error: "Tavily API error: 401",
          message: "Unauthorized",
        }),
      });
      stream.emit("tool.call", 3, {
        tool_call_id: "terminal-1",
        name: "terminal",
        arguments: {
          command: "pip install python-pptx",
        },
      });
      stream.emit("tool.result", 4, {
        tool_call_id: "terminal-1",
        content: JSON.stringify({
          error: "sandbox_unavailable",
          reason: "Sandbox pod sandbox-0df712402538 failed to become ready",
        }),
      });
    });

    expect(container.textContent).toContain(
      "Tavily API error: 401: Unauthorized",
    );
    expect(container.textContent).toContain("Command failed");
    expect(container.textContent).toContain(
      "Sandbox is unavailable. Workspace commands cannot run right now.",
    );
    expect(container.textContent).not.toContain("sandbox-0df712402538");
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

  it("renders scheduled run sessions as read-only", async () => {
    const stream = new FakeEventStream();
    const adapter = createAdapter(stream, {
      session: session("loop-run-1", {
        channel: "scheduled",
        config: { scheduled_session_id: "loop-1" },
      }),
    });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="loop-run-1" />);
      await Promise.resolve();
      await Promise.resolve();
    });

    const textarea = container.querySelector("textarea");
    const uploadButton = container.querySelector(
      'button[aria-label="Upload files"]',
    ) as HTMLButtonElement | null;

    expect(textarea).toBeNull();
    expect(container.textContent).toContain("Scheduled run is read-only");
    expect(uploadButton?.disabled).toBe(true);
  });
});
