import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { useAgentChatRuntime } from "../src/runtime/use-agent-chat-runtime";
import type {
  AgentChatAdapter,
  AgentChatEventsPage,
  AgentChatEventStream,
  AgentChatEventType,
  AgentChatPolledEvent,
  AgentChatRuntimeApi,
  AgentChatSession,
  AgentChatSseMessageEvent,
} from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

class FakeEventStream implements AgentChatEventStream {
  onerror: (() => void) | null = null;
  closed = false;
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

  close(): void {
    this.closed = true;
  }

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

type AdapterCalls = {
  opened: Array<{ sessionId: string; after: number; stream: FakeEventStream }>;
  sent: Array<{ sessionId: string; content: string }>;
  outcomes?: Array<{ sessionId: string; description: string; rubric?: string }>;
  paused: string[];
  retried: string[];
  created: Array<{ agentId?: string; system?: string }>;
};

function session(id: string, status: string = "active"): AgentChatSession {
  return { id, status };
}

function createFakeAdapter(calls: AdapterCalls) {
  return {
    ...NO_BROWSER_ADAPTER,
    async listSessions() {
      return { sessions: [], total: 0 };
    },
    async createSession(input) {
      calls.created.push(input);
      return session("created-session");
    },
    async getSession(input) {
      return session(input.sessionId);
    },
    async sendMessage(input) {
      calls.sent.push(input);
      return { eventId: 1, status: "accepted" };
    },
    async defineOutcome(input) {
      (calls.outcomes ??= []).push(input);
      return { eventId: 2, outcomeId: "outc_test" };
    },
    async pauseSession(input) {
      calls.paused.push(input.sessionId);
    },
    async retrySession(input) {
      calls.retried.push(input.sessionId);
      return session(input.sessionId);
    },
    async getArtifact() {
      throw new Error("not used by runtime tests");
    },
    async submitAskUserQuestionResponse() {
      return { eventId: 1 };
    },
    async getWorkspaceTree() {
      return { root: "workspace", entries: [], truncated: false };
    },
    async getWorkspaceFile() {
      throw new Error("not used by runtime tests");
    },
    async uploadWorkspaceFile() {
      return { path: "uploaded.txt", size: 4 };
    },
    async deleteWorkspaceFile() {},
    getWorkspaceDownloadUrl(input) {
      return `/api/v1/sessions/${input.sessionId}/workspace/download?path=${encodeURIComponent(input.path)}`;
    },
    openEventStream(input) {
      const stream = new FakeEventStream();
      calls.opened.push({ ...input, stream });
      return stream;
    },
  } satisfies AgentChatAdapter;
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

function renderRuntime(props: {
  adapter: AgentChatAdapter;
  agentId?: string;
  sessionId: string | null;
  onSessionChange?: (sessionId: string) => void;
}) {
  let api: AgentChatRuntimeApi | null = null;

  function Harness() {
    api = useAgentChatRuntime(props);
    return null;
  }

  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(<Harness />);
  });

  return {
    get api() {
      if (!api) throw new Error("runtime not rendered");
      return api;
    },
  };
}

describe("useAgentChatRuntime", () => {
  it("opens the adapter event stream with the current session id and cursor", () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      paused: [],
      retried: [],
      created: [],
    };
    const adapter = createFakeAdapter(calls);

    renderRuntime({ adapter, sessionId: "s-1" });

    expect(calls.opened).toHaveLength(1);
    expect(calls.opened[0]).toMatchObject({ sessionId: "s-1", after: 0 });
  });

  it("marks session history as loading until replay events arrive", () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      paused: [],
      retried: [],
      created: [],
    };
    const adapter = createFakeAdapter(calls);
    const runtime = renderRuntime({ adapter, sessionId: "s-1" });

    expect(runtime.api.isLoadingHistory).toBe(true);

    act(() => {
      calls.opened[0]?.stream.emit("user.message", 1, {
        content: "loaded message",
      });
    });

    expect(runtime.api.isLoadingHistory).toBe(false);
    expect(runtime.api.messages[0]?.content).toBe("loaded message");
  });

  it("refreshes the workspace when browser_screenshot completes", () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      paused: [],
      retried: [],
      created: [],
    };
    const adapter = createFakeAdapter(calls);
    const runtime = renderRuntime({ adapter, sessionId: "s-1" });

    expect(runtime.api.workspaceRefreshKey).toBe(0);

    act(() => {
      calls.opened[0]?.stream.emit("tool.call", 1, {
        tool_call_id: "shot-1",
        name: "browser_screenshot",
        arguments: {},
      });
      calls.opened[0]?.stream.emit("tool.result", 2, {
        tool_call_id: "shot-1",
        content: JSON.stringify({
          saved: true,
          path: "/workspace/browser-screenshots/shot.png",
          relative_path: "browser-screenshots/shot.png",
        }),
      });
    });

    expect(runtime.api.workspaceRefreshKey).toBe(1);
  });

  it("closes the old stream when session id changes", () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      paused: [],
      retried: [],
      created: [],
    };
    const adapter = createFakeAdapter(calls);

    renderRuntime({ adapter, sessionId: "s-1" });
    const first = calls.opened[0]?.stream;
    act(() => {
      root?.render(
        <HarnessWrapper adapter={adapter} sessionId="s-2" />,
      );
    });

    expect(first?.closed).toBe(true);
    expect(calls.opened[calls.opened.length - 1]).toMatchObject({
      sessionId: "s-2",
      after: 0,
    });
  });

  it("does not carry the previous session event cursor into the next session", () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      paused: [],
      retried: [],
      created: [],
    };
    const adapter = createFakeAdapter(calls);

    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root?.render(
        <HarnessWrapper adapter={adapter} sessionId="s-1" />,
      );
    });
    act(() => {
      calls.opened[0]?.stream.emit("user.message", 42, {
        content: "from session one",
      });
    });

    act(() => {
      root?.render(
        <HarnessWrapper adapter={adapter} sessionId="s-2" />,
      );
    });

    expect(calls.opened[calls.opened.length - 1]).toMatchObject({
      sessionId: "s-2",
      after: 0,
    });
  });

  it("optimistically appends a user message before sendMessage resolves", async () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      paused: [],
      retried: [],
      created: [],
    };
    const adapter = createFakeAdapter(calls);
    const runtime = renderRuntime({ adapter, sessionId: "s-1" });

    await act(async () => {
      await runtime.api.send("hello");
    });

    expect(runtime.api.messages[0]).toMatchObject({
      role: "user",
      content: "hello",
      status: "complete",
    });
    expect(runtime.api.messages[0]?.id).toMatch(/^local-/);
    expect(calls.sent).toEqual([{ sessionId: "s-1", content: "hello" }]);
  });

  it("defines a goal through the outcome event endpoint for /goal text", async () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      outcomes: [],
      paused: [],
      retried: [],
      created: [],
    };
    const adapter = createFakeAdapter(calls);
    const runtime = renderRuntime({ adapter, sessionId: "s-1" });

    await act(async () => {
      await runtime.api.send("/goal Fix all tests");
    });

    expect(calls.outcomes).toEqual([
      { sessionId: "s-1", description: "Fix all tests" },
    ]);
    expect(calls.sent).toEqual([]);
    expect(runtime.api.messages[0]).toMatchObject({
      role: "user",
      content: "/goal Fix all tests",
      status: "complete",
    });
  });

  it("extracts a rubric when defining a goal", async () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      outcomes: [],
      paused: [],
      retried: [],
      created: [],
    };
    const adapter = createFakeAdapter(calls);
    const runtime = renderRuntime({ adapter, sessionId: "s-1" });

    await act(async () => {
      await runtime.api.send("/goal Fix all tests\n\nRubric:\n- pytest passes");
    });

    expect(calls.outcomes).toEqual([
      {
        sessionId: "s-1",
        description: "Fix all tests",
        rubric: "- pytest passes",
      },
    ]);
    expect(calls.sent).toEqual([]);
  });

  it("sends goal controls as normal slash commands", async () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      outcomes: [],
      paused: [],
      retried: [],
      created: [],
    };
    const adapter = createFakeAdapter(calls);
    const runtime = renderRuntime({ adapter, sessionId: "s-1" });

    await act(async () => {
      await runtime.api.send("/goal status");
    });

    expect(calls.sent).toEqual([{ sessionId: "s-1", content: "/goal status" }]);
    expect(calls.outcomes).toEqual([]);
  });

  it("creates a session before sending when sessionId is null", async () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      paused: [],
      retried: [],
      created: [],
    };
    const changed: string[] = [];
    const adapter = createFakeAdapter(calls);
    const runtime = renderRuntime({
      adapter,
      agentId: "agent-1",
      sessionId: null,
      onSessionChange: (sessionId) => changed.push(sessionId),
    });

    await act(async () => {
      await runtime.api.send("first");
    });

    expect(calls.created).toEqual([{ agentId: "agent-1" }]);
    expect(calls.sent).toEqual([
      { sessionId: "created-session", content: "first" },
    ]);
    expect(changed).toEqual(["created-session"]);
  });

  it("creates a session before defining a first-turn goal", async () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      outcomes: [],
      paused: [],
      retried: [],
      created: [],
    };
    const changed: string[] = [];
    const adapter = createFakeAdapter(calls);
    const runtime = renderRuntime({
      adapter,
      agentId: "agent-1",
      sessionId: null,
      onSessionChange: (sessionId) => changed.push(sessionId),
    });

    await act(async () => {
      await runtime.api.send("/goal Fix all tests");
    });

    expect(calls.created).toEqual([{ agentId: "agent-1" }]);
    expect(calls.outcomes).toEqual([
      { sessionId: "created-session", description: "Fix all tests" },
    ]);
    expect(calls.sent).toEqual([]);
    expect(changed).toEqual(["created-session"]);
  });

  it("shows the first message as running while creating a session", async () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      paused: [],
      retried: [],
      created: [],
    };
    let resolveCreate: ((session: AgentChatSession) => void) | null = null;
    const adapter: AgentChatAdapter = {
      ...createFakeAdapter(calls),
      async createSession(input) {
        calls.created.push(input);
        return await new Promise<AgentChatSession>((resolve) => {
          resolveCreate = resolve;
        });
      },
    };
    const runtime = renderRuntime({
      adapter,
      agentId: "agent-1",
      sessionId: null,
    });

    let sendPromise: Promise<void>;
    await act(async () => {
      sendPromise = runtime.api.send("first");
      await Promise.resolve();
    });

    expect(runtime.api.isRunning).toBe(true);
    expect(runtime.api.messages[0]).toMatchObject({
      role: "user",
      content: "first",
      status: "complete",
    });

    await act(async () => {
      resolveCreate?.(session("created-session"));
      await sendPromise;
    });
  });

  it("keeps the first local message visible when the created session id is applied", async () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      paused: [],
      retried: [],
      created: [],
    };
    let api: AgentChatRuntimeApi | null = null;
    let routeSessionId: string | null = null;
    let resolveCreate: ((session: AgentChatSession) => void) | null = null;
    let resolveSend:
      | ((value: { eventId: number; status: "accepted" }) => void)
      | null = null;

    function rerender() {
      root?.render(
        <HarnessWrapper
          adapter={adapter}
          sessionId={routeSessionId}
          onRuntime={(runtime) => {
            api = runtime;
          }}
        />,
      );
    }

    const adapter: AgentChatAdapter = {
      ...createFakeAdapter(calls),
      async createSession(input) {
        calls.created.push(input);
        const created = await new Promise<AgentChatSession>((resolve) => {
          resolveCreate = resolve;
        });
        routeSessionId = created.id;
        rerender();
        return created;
      },
      async sendMessage(input) {
        calls.sent.push(input);
        return await new Promise<{ eventId: number; status: "accepted" }>(
          (resolve) => {
            resolveSend = resolve;
          },
        );
      },
    };

    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      rerender();
    });

    let sendPromise: Promise<void>;
    await act(async () => {
      sendPromise = api!.send("first");
      await Promise.resolve();
    });

    expect(api!.messages).toHaveLength(1);
    expect(api!.messages[0]).toMatchObject({
      role: "user",
      content: "first",
      status: "complete",
    });

    await act(async () => {
      resolveCreate?.(session("created-session"));
      await Promise.resolve();
    });

    expect(calls.sent).toEqual([
      { sessionId: "created-session", content: "first" },
    ]);
    expect(api!.isLoadingHistory).toBe(false);
    expect(api!.messages).toHaveLength(1);
    expect(api!.messages[0]).toMatchObject({
      role: "user",
      content: "first",
      status: "complete",
    });

    act(() => {
      calls.opened[calls.opened.length - 1]?.stream.emit("user.message", 1, {
        content: "first",
      });
    });

    expect(api!.messages).toHaveLength(1);
    expect(api!.messages[0]?.id).toBe("evt-1");

    await act(async () => {
      resolveSend?.({ eventId: 1, status: "accepted" });
      await sendPromise;
    });
  });

  it("calls pauseSession and marks the current stream stopped on stop", async () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      paused: [],
      retried: [],
      created: [],
    };
    const adapter = createFakeAdapter(calls);
    const runtime = renderRuntime({ adapter, sessionId: "s-1" });

    act(() => {
      calls.opened[0]?.stream.emit("llm.delta", 3, { content: "working" });
    });
    expect(runtime.api.isRunning).toBe(true);

    await act(async () => {
      await runtime.api.stop();
    });

    expect(calls.paused).toEqual(["s-1"]);
    expect(runtime.api.isRunning).toBe(false);
    expect(runtime.api.messages[0]?.status).toBe("complete");
  });

  it("ignores late live events after stop so running does not flash again", async () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      paused: [],
      retried: [],
      created: [],
    };
    const adapter = createFakeAdapter(calls);
    const runtime = renderRuntime({ adapter, sessionId: "s-1" });

    act(() => {
      calls.opened[0]?.stream.emit("llm.delta", 3, { content: "working" });
    });
    expect(runtime.api.isRunning).toBe(true);

    await act(async () => {
      await runtime.api.stop();
    });
    expect(runtime.api.isRunning).toBe(false);

    act(() => {
      calls.opened[0]?.stream.emit("session.resume", 4, {});
      calls.opened[0]?.stream.emit("llm.request", 5, {});
      calls.opened[0]?.stream.emit("llm.delta", 6, { content: "late" });
      calls.opened[0]?.stream.emit("tool.call", 7, {
        tool_call_id: "late-tool",
        name: "terminal",
        arguments: {},
      });
    });

    expect(runtime.api.isRunning).toBe(false);
    expect(runtime.api.messages[0]?.content).toBe("working");
  });

  it("calls retrySession and clears terminal state on retry", async () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      paused: [],
      retried: [],
      created: [],
    };
    const adapter = createFakeAdapter(calls);
    const runtime = renderRuntime({ adapter, sessionId: "s-1" });

    act(() => {
      calls.opened[0]?.stream.emit("session.fail", 5, {
        error_category: "provider_error",
        error_title: "Failed",
        retryable: true,
      });
    });
    expect(runtime.api.isRunning).toBe(false);

    await act(async () => {
      await runtime.api.retry();
    });

    expect(calls.retried).toEqual(["s-1"]);
    expect(runtime.api.isRunning).toBe(true);
  });

  it("reopens a completed session stream before sending a follow-up", async () => {
    const calls: AdapterCalls = {
      opened: [],
      sent: [],
      paused: [],
      retried: [],
      created: [],
    };
    const adapter = createFakeAdapter(calls);
    const runtime = renderRuntime({ adapter, sessionId: "s-1" });

    act(() => {
      calls.opened[0]?.stream.emit("user.message", 1, { content: "first" });
      calls.opened[0]?.stream.emit("llm.response", 2, {
        message: { role: "assistant", content: "done" },
      });
      calls.opened[0]?.stream.emit("session.complete", 3, {});
      calls.opened[0]?.stream.emit("session.done", 0, { status: "completed" });
      calls.opened[0]!.stream.onerror?.();
    });

    expect(runtime.api.isRunning).toBe(false);
    expect(calls.opened).toHaveLength(1);

    await act(async () => {
      await runtime.api.send("again");
    });

    expect(calls.sent).toEqual([{ sessionId: "s-1", content: "again" }]);
    expect(calls.opened).toHaveLength(2);
    expect(calls.opened[1]).toMatchObject({ sessionId: "s-1", after: 3 });
  });
});

describe("useAgentChatRuntime — attachment upload orchestration", () => {
  type UploadCall = {
    sessionId: string;
    directory?: string;
    filename: string;
    bytes: number;
  };

  function buildAttachmentAdapter(opts: {
    createSession: (
      input: { agentId?: string; system?: string },
    ) => Promise<AgentChatSession> | AgentChatSession;
    uploadWorkspaceFile: (input: {
      sessionId: string;
      file: File;
      directory?: string;
    }) =>
      | Promise<{ path: string; size: number }>
      | { path: string; size: number };
    sendMessage?: (input: {
      sessionId: string;
      content: string;
      images?: unknown;
      attachments?: unknown;
    }) =>
      | Promise<{ eventId?: number; status?: string }>
      | { eventId?: number; status?: string };
    openEventStream?: () => AgentChatEventStream;
  }): {
    adapter: AgentChatAdapter;
    uploads: UploadCall[];
    sent: Array<{
      sessionId: string;
      content: string;
      images?: unknown;
      attachments?: unknown;
    }>;
  } {
    const uploads: UploadCall[] = [];
    const sent: Array<{
      sessionId: string;
      content: string;
      images?: unknown;
      attachments?: unknown;
    }> = [];
    const adapter: AgentChatAdapter = {
      ...NO_BROWSER_ADAPTER,
      async listSessions() {
        return { sessions: [], total: 0 };
      },
      async createSession(input) {
        return await opts.createSession(input);
      },
      async getSession(input) {
        return session(input.sessionId);
      },
      async sendMessage(input) {
        sent.push({
          sessionId: input.sessionId,
          content: input.content,
          images: (input as { images?: unknown }).images,
          attachments: (input as { attachments?: unknown }).attachments,
        });
        return opts.sendMessage
          ? await opts.sendMessage(input as never)
          : { eventId: 1, status: "accepted" };
      },
      async defineOutcome() {
        return { eventId: 2, outcomeId: "outc_test" };
      },
      async pauseSession() {},
      async retrySession(input) {
        return session(input.sessionId);
      },
      async getArtifact() {
        throw new Error("not used by runtime tests");
      },
      async submitAskUserQuestionResponse() {
        return { eventId: 1 };
      },
      async getWorkspaceTree() {
        return { root: "workspace", entries: [], truncated: false };
      },
      async getWorkspaceFile() {
        throw new Error("not used by runtime tests");
      },
      async uploadWorkspaceFile(input) {
        const bytes = await input.file.arrayBuffer();
        uploads.push({
          sessionId: input.sessionId,
          directory: input.directory,
          filename: input.file.name,
          bytes: bytes.byteLength,
        });
        return await opts.uploadWorkspaceFile(input);
      },
      async deleteWorkspaceFile() {},
      getWorkspaceDownloadUrl(input) {
        return `/api/v1/sessions/${input.sessionId}/workspace/download?path=${encodeURIComponent(input.path)}`;
      },
      openEventStream() {
        return opts.openEventStream
          ? opts.openEventStream()
          : new FakeEventStream();
      },
    };
    return { adapter, uploads, sent };
  }

  it("creates a session, uploads the file to uploads/, then sends attachment refs", async () => {
    const { adapter, uploads, sent } = buildAttachmentAdapter({
      createSession: async () => session("s-new"),
      uploadWorkspaceFile: async ({ file, directory }) => ({
        path: `${directory ?? ""}/${file.name}`,
        size: file.size,
      }),
    });

    const runtime = renderRuntime({ adapter, sessionId: null });

    const pdf = new File([new Uint8Array([1, 2, 3, 4, 5])], "report.pdf", {
      type: "application/pdf",
    });

    await act(async () => {
      await runtime.api.send("summarize", undefined, [
        {
          file: pdf,
          filename: "report.pdf",
          mimeType: "application/pdf",
          size: 5,
        },
      ]);
    });

    expect(uploads).toHaveLength(1);
    expect(uploads[0]?.sessionId).toBe("s-new");
    expect(uploads[0]?.directory).toBe("uploads");
    expect(uploads[0]?.filename).toMatch(/^\d+-0-report\.pdf$/);
    expect(uploads[0]?.bytes).toBe(5);

    expect(sent).toHaveLength(1);
    expect(sent[0]?.attachments).toEqual([
      {
        path: expect.stringMatching(/^uploads\/\d+-0-report\.pdf$/),
        filename: "report.pdf",
        mimeType: "application/pdf",
        size: 5,
      },
    ]);
  });

  it("paints display-only chips on the local message (path absent until SSE replay)", async () => {
    // The runtime stores the optimistic attachments on the local user
    // message via markSending.  Those entries omit `path` so the chip
    // renderer can show them disabled until the user.message SSE event
    // arrives with the persisted refs (which the reducer test covers).
    // We never emit that event here, so the optimistic message survives
    // intact for assertion.
    const { adapter } = buildAttachmentAdapter({
      createSession: async () => session("s-new"),
      uploadWorkspaceFile: async ({ file, directory }) => ({
        path: `${directory}/${file.name}`,
        size: file.size,
      }),
    });

    const runtime = renderRuntime({ adapter, sessionId: null });

    const pdf = new File([new Uint8Array([1, 2])], "report.pdf", {
      type: "application/pdf",
    });

    await act(async () => {
      await runtime.api.send("summarize", undefined, [
        {
          file: pdf,
          filename: "report.pdf",
          mimeType: "application/pdf",
          size: 2,
        },
      ]);
    });

    const local = runtime.api.messages.find(
      (m) => m.role === "user" && m.id.startsWith("local-"),
    );
    expect(local).toBeDefined();
    expect(local?.attachments).toEqual([
      {
        filename: "report.pdf",
        mimeType: "application/pdf",
        size: 2,
        path: undefined,
      },
    ]);
  });

  it("aborts the whole send and calls markSendError when an upload rejects", async () => {
    const { adapter, sent } = buildAttachmentAdapter({
      createSession: async () => session("s-new"),
      uploadWorkspaceFile: async () => {
        throw new Error("storage offline");
      },
    });

    const runtime = renderRuntime({ adapter, sessionId: null });

    const pdf = new File([new Uint8Array([1])], "x.pdf", {
      type: "application/pdf",
    });

    await act(async () => {
      await expect(
        runtime.api.send("hi", undefined, [
          {
            file: pdf,
            filename: "x.pdf",
            mimeType: "application/pdf",
            size: 1,
          },
        ]),
      ).rejects.toThrow("storage offline");
    });

    expect(sent).toHaveLength(0);
    const last = runtime.api.messages.at(-1)!;
    expect(last.status).toBe("error");
    expect(last.content).toMatch(/storage offline/i);
  });

  it("uploads on an existing session without calling createSession", async () => {
    let created = 0;
    const { adapter, uploads } = buildAttachmentAdapter({
      createSession: async () => {
        created += 1;
        return session("s-other");
      },
      uploadWorkspaceFile: async ({ file, directory }) => ({
        path: `${directory ?? ""}/${file.name}`,
        size: file.size,
      }),
    });

    const runtime = renderRuntime({ adapter, sessionId: "s-existing" });

    const pdf = new File([new Uint8Array([0])], "notes.txt", {
      type: "text/plain",
    });

    await act(async () => {
      await runtime.api.send("hi", undefined, [
        {
          file: pdf,
          filename: "notes.txt",
          mimeType: "text/plain",
          size: 1,
        },
      ]);
    });

    expect(created).toBe(0);
    expect(uploads).toHaveLength(1);
    expect(uploads[0]?.sessionId).toBe("s-existing");
  });

  it("uploads multiple files in parallel and namespaces them with index", async () => {
    let inFlight = 0;
    let peakInFlight = 0;
    const { adapter, uploads, sent } = buildAttachmentAdapter({
      createSession: async () => session("s-new"),
      uploadWorkspaceFile: async ({ file, directory }) => {
        inFlight += 1;
        peakInFlight = Math.max(peakInFlight, inFlight);
        await new Promise((resolve) => setTimeout(resolve, 0));
        inFlight -= 1;
        return { path: `${directory}/${file.name}`, size: file.size };
      },
    });

    const runtime = renderRuntime({ adapter, sessionId: null });

    const a = new File([new Uint8Array([1])], "a.pdf", {
      type: "application/pdf",
    });
    const b = new File([new Uint8Array([2])], "b.pdf", {
      type: "application/pdf",
    });

    await act(async () => {
      await runtime.api.send("hi", undefined, [
        { file: a, filename: "a.pdf", mimeType: "application/pdf", size: 1 },
        { file: b, filename: "b.pdf", mimeType: "application/pdf", size: 1 },
      ]);
    });

    expect(peakInFlight).toBe(2);
    expect(uploads.map((u) => u.filename)).toEqual([
      expect.stringMatching(/^\d+-0-a\.pdf$/),
      expect.stringMatching(/^\d+-1-b\.pdf$/),
    ]);
    expect(sent[0]?.attachments).toHaveLength(2);
  });

  it("strips path separators and NUL from the upload filename", async () => {
    const { adapter, uploads } = buildAttachmentAdapter({
      createSession: async () => session("s-new"),
      uploadWorkspaceFile: async ({ file, directory }) => ({
        path: `${directory}/${file.name}`,
        size: file.size,
      }),
    });

    const runtime = renderRuntime({ adapter, sessionId: null });

    // We construct an input ``File`` whose .name we then re-read so the
    // assertion stays valid regardless of how the runtime's File
    // constructor normalises directory chars on the host platform
    // (JSDOM, e.g., rewrites ``/`` to ``:``).  Our sanitiser is only
    // responsible for stripping the three chars the harness explicitly
    // rejects: ``/``, ``\``, and NUL.
    const raw = new File([new Uint8Array([1])], "a\\b\x00c.pdf", {
      type: "application/pdf",
    });
    expect(raw.name).toContain("\\");
    expect(raw.name).toContain("\x00");

    await act(async () => {
      await runtime.api.send("hi", undefined, [
        {
          file: raw,
          filename: raw.name,
          mimeType: "application/pdf",
          size: 1,
        },
      ]);
    });

    const uploadedName = uploads[0]?.filename ?? "";
    expect(uploadedName).toMatch(/^\d+-0-/);
    // The three forbidden chars are gone.
    expect(uploadedName).not.toContain("/");
    expect(uploadedName).not.toContain("\\");
    expect(uploadedName).not.toContain("\x00");
    // The legitimate ".pdf" suffix and surrounding chars survive.
    expect(uploadedName).toMatch(/abc\.pdf$/);
  });

  it("falls back to 'attachment' when a sanitized filename is empty", async () => {
    const { adapter, uploads } = buildAttachmentAdapter({
      createSession: async () => session("s-new"),
      uploadWorkspaceFile: async ({ file, directory }) => ({
        path: `${directory}/${file.name}`,
        size: file.size,
      }),
    });

    const runtime = renderRuntime({ adapter, sessionId: null });

    // A name built entirely from chars the sanitiser strips (``\`` and
    // NUL) is empty after sanitisation; the runtime substitutes the
    // generic "attachment" placeholder so the workspace key stays well-
    // formed.  ``/`` is excluded from this case because JSDOM's File
    // constructor rewrites it to ``:`` which the sanitiser preserves.
    const bad = new File([new Uint8Array([1])], "\\\\\x00\\\x00", {
      type: "application/octet-stream",
    });

    await act(async () => {
      await runtime.api.send("hi", undefined, [
        {
          file: bad,
          filename: bad.name,
          mimeType: "application/octet-stream",
          size: 1,
        },
      ]);
    });

    expect(uploads[0]?.filename).toMatch(/^\d+-0-attachment$/);
  });
});

describe("useAgentChatRuntime — reconciliation safety net", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function buildReconcilerAdapter(opts: {
    status?: () => string;
    config?: () => Record<string, unknown> | undefined;
    poll?: (after: number) => AgentChatEventsPage;
    withoutPoll?: boolean;
  }): {
    adapter: AgentChatAdapter;
    opened: FakeEventStream[];
    pollAfters: number[];
  } {
    const opened: FakeEventStream[] = [];
    const pollAfters: number[] = [];
    const base = createFakeAdapter({
      opened: [],
      sent: [],
      paused: [],
      retried: [],
      created: [],
    });
    const adapter: AgentChatAdapter = {
      ...base,
      async getSession(input) {
        return {
          id: input.sessionId,
          status: opts.status?.() ?? "active",
          config: opts.config?.(),
        };
      },
      openEventStream(input) {
        const stream = new FakeEventStream();
        opened.push(stream);
        // mirror the cursor onto the stream for assertions
        (stream as unknown as { openedAfter: number }).openedAfter =
          input.after;
        return stream;
      },
    };
    if (!opts.withoutPoll) {
      adapter.pollEvents = async (input) => {
        pollAfters.push(input.after);
        return opts.poll
          ? opts.poll(input.after)
          : { events: [], hasMore: false };
      };
    }
    return { adapter, opened, pollAfters };
  }

  it("self-heals a frozen stream by polling missed events and reviving the stream", async () => {
    let status = "active";
    const polled: AgentChatPolledEvent[] = [];
    const { adapter, opened } = buildReconcilerAdapter({
      status: () => status,
      // A mission session: the server can resume autonomously, which is
      // exactly when a frozen client would otherwise stall forever.
      config: () => ({ active_mission_id: "m-1" }),
      poll: (after) => ({
        events: polled.filter((e) => e.id > after),
        hasMore: false,
      }),
    });

    const runtime = renderRuntime({ adapter, sessionId: "s-1" });

    // The live turn streams to completion.
    act(() => {
      opened[0]?.emit("user.message", 1, { content: "go" });
      opened[0]?.emit("llm.delta", 2, { content: "partial" });
      opened[0]?.emit("llm.response", 3, {
        message: { role: "assistant", content: "partial" },
      });
      opened[0]?.emit("session.complete", 4, {});
      opened[0]?.emit("session.done", 0, { status: "completed" });
    });

    // The stream then "freezes": onerror fires AFTER the done-latch has
    // committed (as it does in the real app, asynchronously), so the
    // built-in reconnect declines and the thread is dead.
    act(() => {
      opened[0]?.onerror?.();
    });

    expect(runtime.api.isRunning).toBe(false);
    expect(opened).toHaveLength(1); // no reconnect — the stream is dead

    // The server actually kept going: the mission resumed and produced a
    // final answer the dead stream never delivered.
    status = "active";
    polled.push(
      { id: 5, type: "session.resume", data: {} },
      { id: 6, type: "llm.delta", data: { content: "final answer" } },
      {
        id: 7,
        type: "llm.response",
        data: { message: { role: "assistant", content: "final answer" } },
      },
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    // The reconciliation poll delivered the missed events...
    expect(
      runtime.api.messages.some((m) => m.content === "final answer"),
    ).toBe(true);
    // ...and the fast SSE path was revived.
    expect(opened.length).toBeGreaterThanOrEqual(2);

    // The revived stream replays from its open cursor, re-delivering the
    // events the poll already applied.  The monotonic dedup guard must drop
    // them so "final answer" is not doubled.
    const revived = opened[opened.length - 1]!;
    act(() => {
      revived.emit("session.resume", 5, {});
      revived.emit("llm.delta", 6, { content: "final answer" });
      revived.emit("llm.response", 7, {
        message: { role: "assistant", content: "final answer" },
      });
    });
    expect(
      runtime.api.messages.filter((m) => m.content === "final answer"),
    ).toHaveLength(1);
  });

  it("does not double-apply an event delivered by both the stream and the poll", async () => {
    const { adapter, opened } = buildReconcilerAdapter({
      status: () => "active",
      // The poll always returns event id 1 regardless of cursor — the
      // worst-case overlap the dedup guard must absorb.
      poll: () => ({
        events: [{ id: 1, type: "llm.delta", data: { content: "X" } }],
        hasMore: false,
      }),
    });

    const runtime = renderRuntime({ adapter, sessionId: "s-1" });

    act(() => {
      opened[0]?.emit("llm.delta", 1, { content: "X" });
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    const assistant = runtime.api.messages.find((m) => m.role === "assistant");
    expect(assistant?.content).toBe("X"); // not "XX"
  });

  it("revives a dead stream even without pollEvents when the session is live", async () => {
    const { adapter, opened } = buildReconcilerAdapter({
      status: () => "active",
      withoutPoll: true,
    });

    const runtime = renderRuntime({ adapter, sessionId: "s-1" });

    act(() => {
      opened[0]?.emit("session.done", 0, { status: "completed" });
    });
    act(() => {
      opened[0]?.onerror?.();
    });
    expect(opened).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    // Status-only reconciliation: the server says active, so the stream is
    // reopened even though this adapter cannot poll events.
    expect(opened.length).toBeGreaterThanOrEqual(2);
    expect(runtime.api).toBeDefined();
  });

  it("does not revive the stream for a finished non-mission session", async () => {
    const { adapter, opened } = buildReconcilerAdapter({
      status: () => "completed",
      config: () => ({}),
    });

    renderRuntime({ adapter, sessionId: "s-1" });

    act(() => {
      opened[0]?.emit("session.done", 0, { status: "completed" });
    });
    act(() => {
      opened[0]?.onerror?.();
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });

    // A genuinely-done chat only resumes on a local user action, so the
    // reconciler must not churn the stream open again.
    expect(opened).toHaveLength(1);
  });
});

function HarnessWrapper(props: {
  adapter: AgentChatAdapter;
  sessionId: string | null;
  onSessionChange?: (sessionId: string) => void;
  onRuntime?: (runtime: AgentChatRuntimeApi) => void;
}) {
  const runtime = useAgentChatRuntime(props);
  props.onRuntime?.(runtime);
  return null;
}
