import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { InboxPanel } from "../src/components/inbox/inbox-panel";
import type {
  AgentChatAdapter,
  AgentChatArtifactPayload,
  AgentChatInboxItem,
  AgentChatInboxList,
  AgentChatSession,
  AgentChatSessionList,
  AgentChatWorkspaceFile,
  AgentChatWorkspaceTree,
  AgentChatWorkspaceUpload,
} from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

function session(input: Partial<AgentChatSession> & { id: string }): AgentChatSession {
  return {
    status: "completed",
    title: "Session",
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
    ...input,
  };
}

function inboxItem(input: Partial<AgentChatInboxItem> & { id: number }): AgentChatInboxItem {
  const { id, ...rest } = input;
  return {
    id,
    orgId: "org-1",
    userId: "user-1",
    sessionId: "session-1",
    sourceEventId: 1,
    kind: "task_complete",
    status: "pending",
    title: "Task finished",
    body: "All done.",
    payload: { outcome: "success", duration_seconds: 90 },
    actionRef: null,
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
    readAt: null,
    respondedAt: null,
    ...rest,
  };
}

function createAdapter(items: AgentChatInboxItem[]): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    async listSessions(): Promise<AgentChatSessionList> {
      return { sessions: [], total: 0 };
    },
    async createSession() {
      return session({ id: "created" });
    },
    async getSession(input) {
      return session({ id: input.sessionId });
    },
    async sendMessage() {
      return { eventId: 1, status: "accepted" };
    },
    async pauseSession() {},
    async retrySession(input) {
      return session({ id: input.sessionId });
    },
    async getArtifact(): Promise<AgentChatArtifactPayload> {
      throw new Error("not used by inbox tests");
    },
    async submitClarifyResponse() {
      return { eventId: 1 };
    },
    async getWorkspaceTree(): Promise<AgentChatWorkspaceTree> {
      return { root: "workspace", entries: [], truncated: false };
    },
    async getWorkspaceFile(): Promise<AgentChatWorkspaceFile> {
      throw new Error("not used by inbox tests");
    },
    async uploadWorkspaceFile(): Promise<AgentChatWorkspaceUpload> {
      return { path: "uploaded.txt", size: 4 };
    },
    async deleteWorkspaceFile() {},
    getWorkspaceDownloadUrl(input) {
      return `/api/v1/sessions/${input.sessionId}/workspace/download?path=${encodeURIComponent(input.path)}`;
    },
    openEventStream() {
      throw new Error("not used by inbox tests");
    },
    async listInbox(): Promise<AgentChatInboxList> {
      return { items, nextCursor: null };
    },
    async getInboxItem(input) {
      const item = items.find((candidate) => candidate.id === input.itemId);
      if (!item) throw new Error("missing item");
      return item;
    },
    async markInboxItemRead(input) {
      const item = items.find((candidate) => candidate.id === input.itemId);
      if (!item) throw new Error("missing item");
      item.readAt = item.readAt ?? "2026-01-01T00:01:00Z";
      return item;
    },
    async acknowledgeInboxItem(input) {
      const item = items.find((candidate) => candidate.id === input.itemId);
      if (!item) throw new Error("missing item");
      item.status = "acknowledged";
      item.respondedAt = "2026-01-01T00:02:00Z";
      return item;
    },
    async respondGovernanceInboxItem(input) {
      const item = items.find((candidate) => candidate.id === input.itemId);
      if (!item) throw new Error("missing item");
      item.payload = { ...item.payload, decision: input.decision };
      item.status = "responded";
      item.respondedAt = "2026-01-01T00:02:00Z";
      return item;
    },
    async respondActionRequiredInboxItem(input) {
      const item = items.find((candidate) => candidate.id === input.itemId);
      if (!item) throw new Error("missing item");
      item.status = "responded";
      item.respondedAt = "2026-01-01T00:02:00Z";
      return item;
    },
    openInboxStream() {
      return {
        addEventListener() {},
        close() {},
        onerror: null,
      };
    },
  };
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

describe("InboxPanel", () => {
  it("renders inbox items and acknowledges task completion", async () => {
    const items = [
      inboxItem({ id: 1, title: "Deploy finished", body: "Deployment passed." }),
    ];
    const adapter = createAdapter(items);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<InboxPanel adapter={adapter} />);
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Deploy finished");
    expect(container.textContent).toContain("Task complete");

    const row = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Open inbox item Deploy finished"]',
    );
    await act(async () => {
      row?.click();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Deployment passed.");
    const ack = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Acknowledge inbox item"]',
    );
    await act(async () => {
      ack?.click();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Acknowledged");
  });

  it("submits input-required answers through the adapter", async () => {
    const submissions: Array<{ question: string; answer: string }> = [];
    const items = [
      inboxItem({
        id: 2,
        kind: "input_required",
        title: "Pick a color",
        body: "Need a color.",
        payload: {
          tool_call_id: "tc-1",
          questions: [{ prompt: "Which color?" }],
        },
      }),
    ];
    const adapter: AgentChatAdapter = {
      ...createAdapter(items),
      async submitClarifyResponse(input) {
        submissions.push(...input.responses);
        items[0] = { ...items[0], status: "responded", respondedAt: "now" };
        return { eventId: 2 };
      },
      async getInboxItem(input) {
        const item = items.find((candidate) => candidate.id === input.itemId);
        if (!item) throw new Error("missing item");
        return item;
      },
    };
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<InboxPanel adapter={adapter} />);
      await Promise.resolve();
    });

    const row = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Open inbox item Pick a color"]',
    );
    await act(async () => {
      row?.click();
      await Promise.resolve();
    });

    const input = container.querySelector<HTMLInputElement>(
      'input[aria-label="Which color?"]',
    );
    await act(async () => {
      if (input) {
        const setter = Object.getOwnPropertyDescriptor(
          HTMLInputElement.prototype,
          "value",
        )?.set;
        setter?.call(input, "blue");
        input.dispatchEvent(new InputEvent("input", { bubbles: true }));
      }
      await Promise.resolve();
    });

    const submit = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Submit inbox response"]',
    );
    await act(async () => {
      submit?.click();
      await Promise.resolve();
    });

    expect(submissions).toEqual([
      { question: "Which color?", answer: "blue", is_other: false },
    ]);
    expect(container.textContent).toContain("Responded");
  });

  it("shows action-required instructions and lets the user mark completion", async () => {
    const completed: number[] = [];
    const selectedSessions: string[] = [];
    const items = [
      inboxItem({
        id: 3,
        kind: "action_required",
        title: "Sign in required",
        body: "Open the browser session and complete sign-in.",
        payload: {
          action_type: "browser",
          instructions: "Open the browser session and complete sign-in.",
          context: "The browser is showing a login page.",
        },
        actionRef: {
          type: "open_session",
          session_id: "session-1",
          target: "browser",
        },
      }),
    ];
    const adapter: AgentChatAdapter = {
      ...createAdapter(items),
      async respondActionRequiredInboxItem(input) {
        completed.push(input.itemId);
        items[0] = { ...items[0], status: "responded", respondedAt: "now" };
        return items[0];
      },
      async getInboxItem(input) {
        const item = items.find((candidate) => candidate.id === input.itemId);
        if (!item) throw new Error("missing item");
        return item;
      },
    };
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(
        <InboxPanel
          adapter={adapter}
          onSessionSelect={(sessionId) => selectedSessions.push(sessionId)}
        />,
      );
      await Promise.resolve();
    });

    const row = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Open inbox item Sign in required"]',
    );
    await act(async () => {
      row?.click();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Action needed");
    expect(container.textContent).toContain("Open the browser session");

    const openSession = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent?.includes("Open session"),
    );
    await act(async () => {
      openSession?.click();
      await Promise.resolve();
    });
    expect(selectedSessions).toEqual(["session-1"]);

    const complete = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Mark action complete"]',
    );
    await act(async () => {
      complete?.click();
      await Promise.resolve();
    });

    expect(completed).toEqual([3]);
    expect(container.textContent).toContain("Responded");
  });
});
