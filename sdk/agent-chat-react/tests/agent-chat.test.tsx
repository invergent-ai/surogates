import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
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

/** A live preview means the browser card is offered; null would hide it. */
async function livePreview() {
  return { src: "data:image/png;base64,cHJldmlldw==" };
}

function createAdapter(
  stream: FakeEventStream,
  options: { session?: AgentChatSession } = {},
) {
  return {
    ...NO_BROWSER_ADAPTER,
    getBrowserPreviewSnapshot: livePreview,
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
    async submitAskUserQuestionResponse() {
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

/** The right column starts closed; a card above the composer opens it. */
async function openPane(
  node: HTMLElement,
  which: "browser" | "files",
): Promise<void> {
  const card = node.querySelector<HTMLButtonElement>(
    `[data-testid="session-pane-card-${which}"]`,
  );
  if (!card) throw new Error(`no ${which} card to open the pane with`);
  await act(async () => {
    card.click();
  });
}

function session(
  id: string,
  overrides: Partial<AgentChatSession> = {},
): AgentChatSession {
  return { id, status: "active", ...overrides };
}

function setTextareaValue(
  textarea: HTMLTextAreaElement,
  value: string,
): void {
  const valueSetter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  )?.set;
  valueSetter?.call(textarea, value);
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

// These tests exercise per-tool / per-block rendering details that
// live inside the Expert-mode timeline. The Simple-mode IterationGroup
// collapses those into a one-line label, so we pin the runtime to
// Expert mode via the localStorage cache used by the SDK runtime.
beforeEach(() => {
  window.localStorage.setItem(
    "@invergent/agent-chat-react:viewMode",
    "expert",
  );
});

afterEach(() => {
  if (root) {
    act(() => root?.unmount());
  }
  root = null;
  container?.remove();
  container = null;
  window.localStorage.removeItem("@invergent/agent-chat-react:viewMode");
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
      await Promise.resolve();
    });

    const textarea = container.querySelector<HTMLTextAreaElement>("textarea");
    if (!textarea) throw new Error("textarea not rendered");

    await act(async () => {
      setTextareaValue(textarea, "/");
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("/loop");
    expect(document.body.textContent).toContain("Schedule recurring prompt");
    expect(document.body.textContent).toContain("/goal");
    expect(document.body.textContent).toContain("Define an outcome goal");
    expect(document.body.textContent).toContain("/mission");
    expect(document.body.textContent).toContain(
      "Start an orchestrated rubric-judged mission",
    );
    expect(document.body.textContent).toContain("/review");
    expect(document.body.textContent).toContain("Review the current work");
  });

  it("renders an EXPERT badge for expert-typed slash entries", async () => {
    const stream = new FakeEventStream();
    const baseAdapter = createAdapter(stream);
    const adapter: AgentChatAdapter = {
      ...baseAdapter,
      async listSlashCommands() {
        return [
          {
            value: "/sql_writer",
            label: "/sql_writer",
            description: "Writes PostgreSQL queries",
            isExpert: true,
          },
          {
            value: "/docx",
            label: "/docx",
            description: "Edit DOCX files",
            // isExpert intentionally omitted to verify the badge is
            // gated on the boolean rather than rendered for every
            // adapter-provided entry.
          },
        ];
      },
    };
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });

    const textarea = container.querySelector<HTMLTextAreaElement>("textarea");
    if (!textarea) throw new Error("textarea not rendered");

    await act(async () => {
      setTextareaValue(textarea, "/");
      // Two microtasks: one for the textarea controller to propagate,
      // one for the listSlashCommands promise to resolve.
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Both entries appear.
    expect(document.body.textContent).toContain("/sql_writer");
    expect(document.body.textContent).toContain("/docx");

    // The badge sits next to the expert entry's label and carries an
    // aria-label so screen readers announce it.
    const badge = document.body.querySelector<HTMLElement>(
      '[aria-label="Expert specialist"]',
    );
    if (badge === null) throw new Error("expert badge not rendered");
    expect(badge.textContent?.toLowerCase()).toBe("expert");

    // Only the expert entry gets a badge.
    const badges = document.body.querySelectorAll(
      '[aria-label="Expert specialist"]',
    );
    expect(badges.length).toBe(1);
  });

  it("sends a message submitted while streaming without pausing the session", async () => {
    const stream = new FakeEventStream();
    const calls: string[] = [];
    const adapter: AgentChatAdapter = {
      ...createAdapter(stream),
      async pauseSession(input) {
        calls.push(`stop:${input.sessionId}`);
      },
      async sendMessage(input) {
        calls.push(`send:${input.sessionId}:${input.content}`);
        return { eventId: 5, status: "accepted" };
      },
    };
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });

    act(() => {
      stream.emit("llm.request", 1, {});
    });

    const textarea = container.querySelector<HTMLTextAreaElement>("textarea");
    const form = container.querySelector<HTMLFormElement>("form");
    expect(textarea).not.toBeNull();
    expect(form).not.toBeNull();

    await act(async () => {
      setTextareaValue(textarea!, "interrupt with this");
      form!.requestSubmit();
      await Promise.resolve();
      await Promise.resolve();
    });

    // The harness steers a mid-turn user.message into the running wake,
    // so pausing first would only cost a sandbox teardown and replay.
    expect(calls).toEqual(["send:s-1:interrupt with this"]);
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

    await openPane(container, "files");
    // The Files card is the accordion's header; a second "Workspace" title
    // inside the panel would just repeat it.
    expect(container.textContent).not.toContain("Workspace");
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

    await openPane(container, "files");
    expect(container.querySelector('[data-testid="workspace-panel"]')).not.toBeNull();
    // No browser in this session, so no browser card and no browser pane.
    expect(
      container.querySelector('[data-testid="session-pane-card-browser"]'),
    ).toBeNull();
    expect(container.querySelector('[data-testid="browser-pane"]')).toBeNull();
  });

  it("toggles the browser pane from its card", async () => {
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

    await openPane(container, "browser");
    expect(container.querySelector('[data-testid="browser-panel"]')).not.toBeNull();

    // The same card hides it again — show and hide live on one button.
    await openPane(container, "browser");
    expect(container.querySelector('[data-testid="browser-panel"]')).toBeNull();
    expect(container.querySelector('[data-testid="right-stack"]')).toBeNull();
  });

  it("expands the files accordion in the chat column, not a drawer", async () => {
    const stream = new FakeEventStream();
    const adapter = createAdapter(stream);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });

    const card = () =>
      container!.querySelector('[data-testid="session-pane-card-files"]');
    expect(card()?.getAttribute("aria-expanded")).toBe("false");

    await openPane(container!, "files");
    const panel = container!.querySelector('[data-testid="workspace-panel"]');
    expect(panel).not.toBeNull();
    // Inline above the composer: the chat column contains it, and no right
    // column opens for it — the drawer is the browser's alone now.
    expect(
      container!.querySelector('[data-testid="chat-panel"]')!.contains(panel!),
    ).toBe(true);
    expect(container!.querySelector('[data-testid="right-stack"]')).toBeNull();
    expect(card()?.getAttribute("aria-expanded")).toBe("true");

    // The same header collapses it.
    await openPane(container!, "files");
    expect(
      container!.querySelector('[data-testid="workspace-panel"]'),
    ).toBeNull();
    expect(card()?.getAttribute("aria-expanded")).toBe("false");
  });

  it("offers a phone tab for the open pane, and closes when it goes away", async () => {
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

    const tabLabels = () =>
      Array.from(
        container?.querySelectorAll<HTMLElement>(
          '[data-testid="mobile-pane-toggle"] button',
        ) ?? [],
      ).map((button) => button.textContent);

    // Closed column: nothing to toggle between, so no toggle at all.
    expect(tabLabels()).toEqual([]);

    // Files expand inline in the chat column, so they never earn a phone tab.
    await openPane(container!, "files");
    expect(tabLabels()).toEqual([]);

    await act(async () => {
      stream.emit("browser.provisioned", 10, { session_id: "s-1" });
      await Promise.resolve();
    });
    // Provisioning a browser does not open the pane, it offers a card.
    expect(tabLabels()).toEqual([]);
    await openPane(container!, "browser");
    expect(tabLabels()).toEqual(["Chat", "Browser"]);

    // Park the layout on the browser tab, then close the browser: the tab it
    // was showing no longer exists, so it must not be left on a blank pane.
    const browserTab = Array.from(
      container?.querySelectorAll<HTMLElement>(
        '[data-testid="mobile-pane-toggle"] button',
      ) ?? [],
    ).find((button) => button.textContent === "Browser");
    await act(async () => {
      browserTab?.click();
      await Promise.resolve();
    });
    expect(
      container
        ?.querySelector('[data-testid="right-stack"]')
        ?.getAttribute("data-mobile-view"),
    ).toBe("browser");

    await act(async () => {
      stream.emit("browser.destroyed", 11, { session_id: "s-1" });
      await Promise.resolve();
    });
    // The open pane vanished, so the column closes rather than showing a
    // blank tab, and the layout falls back to the chat.
    expect(tabLabels()).toEqual([]);
    expect(
      container
        ?.querySelector('[data-testid="chat-panel"]')
        ?.getAttribute("data-mobile-view"),
    ).toBe("chat");
  });

  it("gives the column to whichever pane a card opened", async () => {
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

    // Closed until a card opens it -- the panes are tabs, never both at once.
    expect(container.querySelector('[data-testid="browser-pane"]')).toBeNull();
    await openPane(container, "browser");

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
    // One pane at a time: the workspace is a tab away, not stacked below.
    expect(workspacePanel).toBeNull();
    expect(workspacePanelFrame).toBeNull();
    expect((layout as HTMLElement | null)?.style.direction).toBe("ltr");
    // Right-stack width is exposed as a CSS custom property on the layout
    // element so responsive classes can reference it via var().
    expect(
      (layout as HTMLElement | null)?.style.getPropertyValue("--right-stack-w"),
    ).toBe("50%");
    // Desktop two-pane layout is `md:`-prefixed (mobile-first single column).
    expect(layout?.className).toContain("md:relative");
    expect(chatPanel?.className).toContain("md:absolute");
    expect(chatPanel?.className).toContain("md:left-0");
    expect(chatPanel?.className).toContain("md:right-(--right-stack-w,440px)");
    expect(chatPanel?.className).toContain("flex");
    expect(chatPanel?.className).toContain("flex-col");
    expect(chatPanel?.className).toContain("min-h-0");
    expect(rightStack?.className).toContain("md:absolute");
    expect(rightStack?.className).toContain("md:right-0");
    expect(rightStack?.className).toContain("md:w-(--right-stack-w,440px)");
    // The open pane owns the whole column now, at every width -- there is no
    // half to share, because the other pane is behind a tab.
    expect(browserPanel?.className).toContain("w-full");
    expect(browserPanel?.className).toContain("h-full");
    expect(browserPanel?.className).toContain("overflow-hidden");
    expect(browserPanel?.className).not.toContain("md:h-1/2");
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

    await openPane(container, "files");
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

    await openPane(container, "files");
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

    await openPane(container, "files");
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

  it("opens the file preview as a full drawer, sized like the browser pane", async () => {
    const stream = new FakeEventStream();
    const adapter = createAdapter(stream);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });

    await openPane(container, "files");
    // The tree alone opens no drawer.
    expect(container.querySelector('[data-testid="right-stack"]')).toBeNull();

    const fileButton = Array.from(
      container.querySelectorAll<HTMLElement>('[role="treeitem"]'),
    )
      .reverse()
      .find((element) => element.textContent?.includes("main.py"));
    await act(async () => {
      fileButton!.click();
      await Promise.resolve();
    });

    // The preview claims the right column at the browser pane's geometry —
    // not a strip inside the accordion.
    const drawer = container.querySelector(
      '[data-testid="file-preview-panel"]',
    );
    const rightStack = container.querySelector('[data-testid="right-stack"]');
    expect(drawer).not.toBeNull();
    expect(rightStack?.contains(drawer!)).toBe(true);
    expect(rightStack?.className).toContain("md:w-(--right-stack-w,440px)");
    expect(
      container.querySelector('[data-testid="chat-panel"]')?.className,
    ).toContain("md:absolute");

    const closeButton = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Close file"]',
    );
    await act(async () => {
      closeButton!.click();
      await Promise.resolve();
    });
    expect(
      container.querySelector('[data-testid="file-preview-panel"]'),
    ).toBeNull();
    expect(container.querySelector('[data-testid="right-stack"]')).toBeNull();
  });

  it("ignores folder clicks and clears the old file when switching", async () => {
    const stream = new FakeEventStream();
    const resolvers: Array<() => void> = [];
    const requested: string[] = [];
    const adapter = {
      ...createAdapter(stream),
      async getWorkspaceTree() {
        return {
          root: "workspace",
          entries: [
            {
              name: "src",
              path: "src",
              kind: "dir" as const,
              children: [
                { name: "a.py", path: "src/a.py", kind: "file" as const, size: 1 },
                { name: "b.py", path: "src/b.py", kind: "file" as const, size: 1 },
              ],
            },
          ],
          truncated: false,
        };
      },
      getWorkspaceFile(input: { sessionId: string; path: string }) {
        requested.push(input.path);
        return new Promise<{
          path: string;
          content: string;
          size: number;
          mime_type: string;
          encoding: "utf-8";
          truncated: boolean;
        }>((resolve) => {
          resolvers.push(() =>
            resolve({
              path: input.path,
              content: `content of ${input.path}`,
              size: 1,
              mime_type: "text/x-python",
              encoding: "utf-8",
              truncated: false,
            }),
          );
        });
      },
    };
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });
    await openPane(container, "files");

    // Reversed: a folder's treeitem wraps its children, so the first match
    // for a file name is the folder, not the file row.
    const row = (label: string) =>
      Array.from(
        container!.querySelectorAll<HTMLElement>('[role="treeitem"]'),
      )
        .reverse()
        .find((element) => element.textContent?.includes(label));
    // The folder's select gesture is its name button, not the wrapper.
    const folderButton = () =>
      Array.from(container!.querySelectorAll<HTMLButtonElement>("button")).find(
        (button) => button.textContent?.trim() === "src",
      );

    await act(async () => {
      row("a.py")!.click();
      await Promise.resolve();
    });
    expect(requested).toEqual(["src/a.py"]);
    expect(
      container.querySelector('[data-testid="file-preview-panel"]'),
    ).not.toBeNull();
    await act(async () => {
      resolvers.shift()?.();
      await Promise.resolve();
    });
    expect(container.textContent).toContain("content of src/a.py");

    // A folder is not a document: clicking one must neither close the drawer
    // nor make it fetch the directory.
    await act(async () => {
      folderButton()!.click();
      await Promise.resolve();
    });
    expect(
      container.querySelector('[data-testid="file-preview-panel"]'),
    ).not.toBeNull();
    expect(container.textContent).toContain("content of src/a.py");
    expect(requested).toEqual(["src/a.py"]);

    // Switching files clears the old document immediately — the previous
    // file must not sit under the next one's loading skeleton.
    await act(async () => {
      row("b.py")!.click();
      await Promise.resolve();
    });
    expect(container.textContent).not.toContain("content of src/a.py");
    expect(
      container.querySelector('[data-testid="file-preview-panel"]'),
    ).not.toBeNull();

    await act(async () => {
      resolvers.shift()?.();
      await Promise.resolve();
    });
    expect(container.textContent).toContain("content of src/b.py");
  });

  it("reinitializes the files accordion when the session changes", async () => {
    const stream = new FakeEventStream();
    const adapter = createAdapter(stream);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
      await Promise.resolve();
    });

    await openPane(container, "files");
    const fileButton = Array.from(
      container.querySelectorAll<HTMLElement>('[role="treeitem"]'),
    )
      .reverse()
      .find((element) => element.textContent?.includes("main.py"));
    await act(async () => {
      fileButton!.click();
      await Promise.resolve();
    });
    expect(
      container.querySelector('[data-testid="file-preview-panel"]'),
    ).not.toBeNull();

    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="s-2" />);
      await Promise.resolve();
    });

    // Another session's files: the accordion folds and the previous
    // session's preview does not follow across.
    expect(
      container
        .querySelector('[data-testid="session-pane-card-files"]')
        ?.getAttribute("aria-expanded"),
    ).toBe("false");
    expect(
      container.querySelector('[data-testid="workspace-panel"]'),
    ).toBeNull();
    expect(
      container.querySelector('[data-testid="file-preview-panel"]'),
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

    await openPane(container, "files");
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

    await openPane(container, "files");
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

  it("renders the slash-invoked expert path (delegation -> result) with no preceding consult_expert tool call", async () => {
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
      // Slash path: user types /sql_writer ... -> harness emits a raw
      // user.message, then expert.delegation directly (no tool.call
      // for consult_expert), then expert.result with the deliverable.
      stream.emit("user.message", 1, {
        content: "/sql_writer write a query for the orders table",
      });
      stream.emit("expert.delegation", 2, {
        expert: "sql_writer",
        task: "write a query for the orders table",
        tools: ["terminal"],
        max_iterations: 10,
      });
      stream.emit("expert.result", 3, {
        expert: "sql_writer",
        success: true,
        content: "SELECT * FROM orders LIMIT 10;",
        iterations_used: 2,
      });
    });

    // The synthesized frame renders just like the LLM-initiated one.
    expect(container.textContent).toContain("Consulted expert");
    expect(container.textContent).toContain("sql_writer");
    expect(container.textContent).toContain("SELECT * FROM orders LIMIT 10;");
    // And the user's raw slash message is preserved in the thread.
    expect(container.textContent).toContain("/sql_writer");
  });

  it("renders expert.failure with the error message", async () => {
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
      stream.emit("user.message", 1, {
        content: "/sql_writer something",
      });
      stream.emit("expert.delegation", 2, {
        expert: "sql_writer",
        task: "something",
      });
      stream.emit("expert.failure", 3, {
        expert: "sql_writer",
        success: false,
        error: "Expert 'sql_writer' has no endpoint configured.",
      });
    });

    expect(container.textContent).toContain("Consulted expert");
    expect(container.textContent).toContain("sql_writer");
    expect(container.textContent).toContain("failed");
  });

  it("does not double-render when expert.delegation arrives after an LLM-issued consult_expert", async () => {
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
          expert: "sql_writer",
          question: "write a query",
        },
      });
      // The harness/service emits expert.delegation after the LLM
      // issued its tool.call.  The reducer must NOT synthesize a
      // second frame -- the LLM-issued one is the canonical render.
      stream.emit("expert.delegation", 2, {
        expert: "sql_writer",
        task: "write a query",
      });
      stream.emit("tool.result", 3, {
        tool_call_id: "expert-1",
        content: "SELECT 1;",
      });
      stream.emit("expert.result", 4, {
        expert: "sql_writer",
        success: true,
        content: "SELECT 1;",
      });
    });

    // Exactly one "Consulted expert" frame in the DOM.
    const matches = container.textContent?.match(/Consulted expert/g) ?? [];
    expect(matches.length).toBe(1);
  });

  it("shows a visible explanation for failed non-terminal tool dots and hides failed terminal calls", async () => {
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
    // Failed terminal calls are intentionally hidden from the timeline.
    expect(container.textContent).not.toContain("Command failed");
    expect(container.textContent).not.toContain(
      "Sandbox is unavailable. Workspace commands cannot run right now.",
    );
    expect(container.textContent).not.toContain("pip install python-pptx");
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

    // After switching to a sessionless chat the workspace pane is hidden
    // entirely (no right stack at all). Stale files from the previous
    // session must not leak into the DOM.
    expect(container.textContent).not.toContain("old-session.txt");
    expect(
      container.querySelector('[data-testid="workspace-panel"]'),
    ).toBeNull();
    expect(
      container.querySelector('[data-testid="right-stack"]'),
    ).toBeNull();
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

    // The composer still renders a disabled textarea so the
    // Simple/Advanced toggle stays accessible -- but the textarea is
    // disabled and the read-only reason is surfaced as its placeholder.
    const textarea = container.querySelector<HTMLTextAreaElement>("textarea");
    expect(textarea).not.toBeNull();
    expect(textarea?.disabled).toBe(true);
    expect(textarea?.placeholder).toContain("Scheduled run is read-only");

    // The tools panel stays: a read-only run still has a workspace worth
    // opening. What read-only removes is the ability to add material to the
    // next turn, so the Attach group is gone from inside it.
    const tools = container.querySelector(
      'button[aria-label="Composer tools"]',
    ) as HTMLButtonElement | null;
    expect(tools).not.toBeNull();
    await act(async () => {
      tools?.click();
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("Workspace");
    expect(document.body.textContent).not.toContain("Add local files");
  });
});
