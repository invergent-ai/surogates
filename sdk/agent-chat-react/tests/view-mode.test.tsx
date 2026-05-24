/**
 * Persistence + runtime wiring for the Simple/Expert chat view mode.
 *
 * Default is "simple". The runtime prefers the adapter's persisted
 * value when implemented, falling back to localStorage otherwise.
 * setViewMode writes through to both the in-memory state and whichever
 * persistence the adapter supports.
 */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { useAgentChatRuntime } from "../src/runtime/use-agent-chat-runtime";
import type { AgentChatAdapter, AgentChatRuntimeApi } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const VIEW_MODE_KEY = "@invergent/agent-chat-react:viewMode";


function makeAdapter(
  overrides: Partial<AgentChatAdapter> = {},
): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    listSessions: vi.fn().mockResolvedValue({ sessions: [], total: 0 }),
    createSession: vi.fn(),
    getSession: vi.fn(),
    sendMessage: vi.fn(),
    openEventStream: vi.fn(() => ({
      addEventListener: vi.fn(),
      close: vi.fn(),
      onerror: null,
    })),
    getSessionEvents: vi.fn().mockResolvedValue({ events: [], total: 0 }),
    getBrowserState: vi.fn().mockResolvedValue(null),
    ...overrides,
  } as unknown as AgentChatAdapter;
}


/**
 * Mount the hook in a tiny consumer component and expose the latest
 * API value via a ref so individual tests can call its methods.
 */
function mountRuntime(adapter: AgentChatAdapter): {
  current: AgentChatRuntimeApi;
  unmount: () => void;
} {
  const ref: { current: AgentChatRuntimeApi | null } = { current: null };

  function Consumer() {
    ref.current = useAgentChatRuntime({ adapter, sessionId: null });
    return null;
  }

  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(<Consumer />);
  });

  return {
    get current() {
      if (!ref.current) throw new Error("runtime not mounted");
      return ref.current;
    },
    unmount: () => {
      act(() => root.unmount());
      container.remove();
    },
  };
}

let active: ReturnType<typeof mountRuntime> | null = null;

afterEach(() => {
  active?.unmount();
  active = null;
});

beforeEach(() => {
  window.localStorage.clear();
});


describe("viewMode runtime", () => {
  it("defaults to simple when no preference is stored", () => {
    active = mountRuntime(makeAdapter());
    expect(active.current.viewMode).toBe("simple");
  });

  it("falls back to localStorage when the adapter lacks getChatViewMode", () => {
    window.localStorage.setItem(VIEW_MODE_KEY, "expert");
    active = mountRuntime(makeAdapter());
    expect(active.current.viewMode).toBe("expert");
  });

  it("prefers the adapter's persisted value when implemented", async () => {
    const adapter = makeAdapter({
      getChatViewMode: vi.fn().mockResolvedValue("expert"),
    });
    active = mountRuntime(adapter);
    // Allow the adapter's async load + React re-render.
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });
    expect(active.current.viewMode).toBe("expert");
  });

  it("ignores invalid adapter return values", async () => {
    const adapter = makeAdapter({
      getChatViewMode: vi.fn().mockResolvedValue("garbage" as never),
    });
    active = mountRuntime(adapter);
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });
    expect(active.current.viewMode).toBe("simple");
  });

  it("setViewMode writes through to the adapter when present", () => {
    const setChatViewMode = vi.fn().mockResolvedValue(undefined);
    const adapter = makeAdapter({ setChatViewMode });
    active = mountRuntime(adapter);
    act(() => {
      active!.current.setViewMode("expert");
    });
    expect(active.current.viewMode).toBe("expert");
    expect(setChatViewMode).toHaveBeenCalledWith("expert");
  });

  it("setViewMode writes to localStorage even when adapter persistence exists", () => {
    const setChatViewMode = vi.fn().mockResolvedValue(undefined);
    const adapter = makeAdapter({ setChatViewMode });
    active = mountRuntime(adapter);
    act(() => {
      active!.current.setViewMode("expert");
    });
    // localStorage is the source of truth on the first mount before
    // adapter.getChatViewMode resolves — keep them in sync.
    expect(window.localStorage.getItem(VIEW_MODE_KEY)).toBe("expert");
  });

  it("setViewMode falls back to localStorage when the adapter lacks the method", () => {
    active = mountRuntime(makeAdapter());
    act(() => {
      active!.current.setViewMode("expert");
    });
    expect(window.localStorage.getItem(VIEW_MODE_KEY)).toBe("expert");
  });

  it("setViewMode swallows adapter persistence failures", () => {
    const setChatViewMode = vi
      .fn()
      .mockRejectedValue(new Error("network down"));
    const adapter = makeAdapter({ setChatViewMode });
    active = mountRuntime(adapter);
    act(() => {
      active!.current.setViewMode("expert");
    });
    expect(active.current.viewMode).toBe("expert");
  });
});
