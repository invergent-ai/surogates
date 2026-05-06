import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
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

function createFakeAdapter(calls: AdapterCalls): AgentChatAdapter {
  return {
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
    openEventStream(input) {
      const stream = new FakeEventStream();
      calls.opened.push({ ...input, stream });
      return stream;
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
});

function HarnessWrapper(props: {
  adapter: AgentChatAdapter;
  sessionId: string | null;
}) {
  useAgentChatRuntime(props);
  return null;
}
