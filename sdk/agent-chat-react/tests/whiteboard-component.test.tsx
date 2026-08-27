import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentWhiteboard } from "../src/components/whiteboard/agent-whiteboard";
import type { AgentChatAdapter } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

// happy-dom has no canvas 2D context; the component must survive that.
const recordingContext = () =>
  new Proxy({} as Record<string, unknown>, {
    get(target, property) {
      if (property in target) return target[property as string];
      if (property === "measureText") {
        return (t: string) => ({ width: t.length * 8 });
      }
      return () => undefined;
    },
    set(target, property, value) {
      target[property as string] = value;
      return true;
    },
  }) as unknown as CanvasRenderingContext2D;

function stubCanvas() {
  HTMLCanvasElement.prototype.getContext = (() =>
    recordingContext()) as unknown as HTMLCanvasElement["getContext"];
  HTMLCanvasElement.prototype.toDataURL = () => "data:image/png;base64,AAAA";
}
stubCanvas();

if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

function makeAdapter(overrides: Partial<AgentChatAdapter> = {}) {
  const sendMessage = vi.fn(
    async (_input: Record<string, unknown>) => ({ eventId: 1 }),
  );
  const adapter = {
    createSession: vi.fn(async () => ({ id: "s1", status: "active" })),
    getSession: vi.fn(async () => ({ id: "s1", status: "active" })),
    sendMessage,
    getWorkspaceFile: vi.fn(async () => {
      throw new Error("404");
    }),
    uploadWorkspaceFile: vi.fn(async () => ({ path: "p", size: 1 })),
    listEvents: vi.fn(async () => ({ events: [], nextCursor: 0 })),
    pollEvents: vi.fn(async () => ({ events: [], hasMore: false })),
    openEventStream: vi.fn(() => ({
      addEventListener: vi.fn(),
      close: vi.fn(),
      onerror: null,
    })),
    pauseSession: vi.fn(async () => undefined),
    retrySession: vi.fn(async () => ({ id: "s1", status: "active" })),
    getWorkspaceTree: vi.fn(async () => ({
      root: "b", entries: [], truncated: false,
    })),
    deleteWorkspaceFile: vi.fn(async () => undefined),
    getWorkspaceDownloadUrl: () => "",
    getBrowserState: vi.fn(async () => null),
    ...overrides,
  } as unknown as AgentChatAdapter;
  return { adapter, sendMessage };
}

let root: Root | null = null;
let host: HTMLDivElement | null = null;

async function render(element: ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(element);
  });
  return host;
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

function byLabel(el: HTMLElement, label: string) {
  return el.querySelector<HTMLElement>(`[aria-label="${label}"]`);
}

describe("AgentWhiteboard", () => {
  it("renders a canvas", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    expect(byLabel(el, "Whiteboard canvas")).not.toBeNull();
  });

  it("renders the tool rail with every tool", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    for (const tool of ["Pen", "Eraser", "Select", "Pan"]) {
      expect(byLabel(el, tool)).not.toBeNull();
    }
  });

  it("starts on the pen tool", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    expect(byLabel(el, "Pen")?.getAttribute("aria-pressed")).toBe("true");
    expect(byLabel(el, "Pan")?.getAttribute("aria-pressed")).toBe("false");
  });

  it("switches the active tool on click", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await act(async () => {
      byLabel(el, "Pan")?.click();
    });
    expect(byLabel(el, "Pan")?.getAttribute("aria-pressed")).toBe("true");
    expect(byLabel(el, "Pen")?.getAttribute("aria-pressed")).toBe("false");
  });

  it("offers both speeds", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    expect(byLabel(el, "Ask")).not.toBeNull();
    expect(byLabel(el, "Think harder")).not.toBeNull();
  });

  it("sends an image and whiteboard metadata on Ask", async () => {
    const { adapter, sendMessage } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await act(async () => {
      byLabel(el, "Ask")?.click();
    });
    expect(sendMessage).toHaveBeenCalledTimes(1);
    const payload = sendMessage.mock.calls[0][0] as unknown as {
      images?: { data: string }[];
      metadata?: { whiteboard?: Record<string, unknown> };
    };
    expect(payload.images?.[0].data).toContain("data:image/png");
    expect(payload.metadata?.whiteboard).toHaveProperty("sourceRect");
    expect(payload.metadata?.whiteboard).toHaveProperty("imageScale");
  });

  it("sends mode sketch by default", async () => {
    const { adapter, sendMessage } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await act(async () => {
      byLabel(el, "Ask")?.click();
    });
    const payload = sendMessage.mock.calls[0][0] as unknown as {
      metadata?: { whiteboard?: { mode?: string } };
    };
    expect(payload.metadata?.whiteboard?.mode).toBe("sketch");
  });

  it("sends mode deep from Think harder", async () => {
    const { adapter, sendMessage } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await act(async () => {
      byLabel(el, "Think harder")?.click();
    });
    const payload = sendMessage.mock.calls[0][0] as unknown as {
      metadata?: { whiteboard?: { mode?: string } };
    };
    expect(payload.metadata?.whiteboard?.mode).toBe("deep");
  });

  it("keeps the transcript collapsed by default", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    expect(byLabel(el, "Toggle transcript")?.getAttribute("aria-expanded"))
      .toBe("false");
  });

  it("opens the transcript on click", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await act(async () => {
      byLabel(el, "Toggle transcript")?.click();
    });
    expect(byLabel(el, "Toggle transcript")?.getAttribute("aria-expanded"))
      .toBe("true");
  });

  it("survives a missing canvas document", async () => {
    // getWorkspaceFile rejects in the default adapter; the board must
    // still mount rather than blanking the surface.
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    expect(byLabel(el, "Whiteboard canvas")).not.toBeNull();
  });

  it("disables the controls when disabled", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" disabled />,
    );
    expect(byLabel(el, "Ask")).toHaveProperty("disabled", true);
    expect(byLabel(el, "Pen")).toHaveProperty("disabled", true);
  });
});
