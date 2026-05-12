import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { useAgentChatRuntime } from "../src/runtime/use-agent-chat-runtime";
import type {
  AgentChatAdapter,
  AgentChatEventStream,
  AgentChatEventType,
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
    async submitClarifyResponse() {
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
