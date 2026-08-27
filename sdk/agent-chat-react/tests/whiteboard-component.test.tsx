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

let sharedCalls: unknown[][] = [];

function stubCanvas() {
  HTMLCanvasElement.prototype.getContext = (() => {
    const ctx = recordingContext();
    // Route every draw through one recorder so a test can read back the
    // view transform the component actually painted with.
    (ctx as unknown as { calls: unknown[][] }).calls = sharedCalls;
    return new Proxy(ctx as object, {
      get(target, property) {
        if (property === "calls") return sharedCalls;
        const value = Reflect.get(target, property);
        if (typeof value !== "function") return value;
        return (...args: unknown[]) => {
          sharedCalls.push([property, ...args]);
          return value(...args);
        };
      },
    });
  }) as unknown as HTMLCanvasElement["getContext"];
  HTMLCanvasElement.prototype.toDataURL = () => "data:image/png;base64,AAAA";
}
stubCanvas();

/** The x/y translation of the most recent view transform painted. */
function lastViewTranslation(): { x: number; y: number } | null {
  for (let i = sharedCalls.length - 1; i >= 0; i--) {
    const call = sharedCalls[i];
    if (call[0] === "setTransform" && call.length === 7) {
      return { x: call[5] as number, y: call[6] as number };
    }
  }
  return null;
}

// The canvas repaints inside requestAnimationFrame, which act() does not
// flush. Run it synchronously so a test can read back what was painted.
globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
  cb(0);
  return 0;
}) as typeof requestAnimationFrame;
globalThis.cancelAnimationFrame = (() => undefined) as typeof cancelAnimationFrame;

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

  it("offers fit-to-content, the only way home on an infinite canvas", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    expect(byLabel(el, "Fit to content")).not.toBeNull();
  });

  it("records a completed drag as one undo step", async () => {
    // The agent's output arrives selected precisely so a mis-placed
    // answer can be dragged off the user's work. Undo becoming
    // available is the observable proof the gesture reached the
    // document -- and that it is ONE step, not one per pointer sample.
    const { adapter } = makeAdapter({
      getWorkspaceFile: vi.fn(async () => ({
        path: "_whiteboard/canvas.json",
        content: JSON.stringify({
          version: 1,
          lastEventId: 0,
          objects: [{
            id: "t1", origin: "evt1", selected: true, kind: "text",
            x: 100, y: 100, text: "hi", fontSize: 32,
            maxWidth: 300, lineHeight: 1.35,
          }],
        }),
        size: 1,
        encoding: "utf-8" as const,
        truncated: false,
      })),
    });
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await act(async () => {
      byLabel(el, "Select")?.click();
    });
    expect(byLabel(el, "Undo")).toHaveProperty("disabled", true);

    const canvas = byLabel(el, "Whiteboard canvas") as HTMLCanvasElement;
    canvas.setPointerCapture = () => undefined;
    canvas.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 800, height: 600 }) as DOMRect;

    // The object sits at logical 100,100; the initial view origin is
    // -400,-300 at zoom 1, so it is on screen at 500,400.
    await act(async () => {
      canvas.dispatchEvent(
        new PointerEvent("pointerdown", {
          clientX: 500, clientY: 400, bubbles: true,
        }),
      );
      canvas.dispatchEvent(
        new PointerEvent("pointermove", {
          clientX: 560, clientY: 430, bubbles: true,
        }),
      );
      canvas.dispatchEvent(new PointerEvent("pointerup", { bubbles: true }));
    });

    expect(byLabel(el, "Undo")).toHaveProperty("disabled", false);
  });

  it("offers a text tool", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    expect(byLabel(el, "Text")).not.toBeNull();
  });

  async function typeOnBoard(el: HTMLElement, body: string) {
    await act(async () => {
      byLabel(el, "Text")?.click();
    });
    const canvas = byLabel(el, "Whiteboard canvas") as HTMLCanvasElement;
    canvas.setPointerCapture = () => undefined;
    canvas.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 800, height: 600 }) as DOMRect;
    await act(async () => {
      canvas.dispatchEvent(
        new PointerEvent("pointerdown", {
          clientX: 200, clientY: 200, bubbles: true,
        }),
      );
    });
    const box = byLabel(el, "Text") as HTMLTextAreaElement | null;
    const area = el.querySelector("textarea");
    if (!area) throw new Error("text editor did not open");
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        globalThis.HTMLTextAreaElement.prototype,
        "value",
      )?.set;
      setter?.call(area, body);
      area.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      area.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Enter", ctrlKey: true, bubbles: true,
        }),
      );
    });
    return box;
  }

  it("opens an editor when the text tool is used", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await act(async () => {
      byLabel(el, "Text")?.click();
    });
    const canvas = byLabel(el, "Whiteboard canvas") as HTMLCanvasElement;
    canvas.setPointerCapture = () => undefined;
    canvas.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 800, height: 600 }) as DOMRect;
    await act(async () => {
      canvas.dispatchEvent(
        new PointerEvent("pointerdown", {
          clientX: 200, clientY: 200, bubbles: true,
        }),
      );
    });
    expect(el.querySelector("textarea")).not.toBeNull();
  });

  it("sends typed text as transcription ground truth", async () => {
    // The model must read the exact characters rather than transcribing
    // its own rendering of them back out of the atlas.
    const { adapter, sendMessage } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await typeOnBoard(el, "integral of x squared");
    await act(async () => {
      byLabel(el, "Ask")?.click();
    });
    const payload = sendMessage.mock.calls[0][0] as unknown as {
      metadata?: { whiteboard?: { typedInput?: string } };
    };
    expect(payload.metadata?.whiteboard?.typedInput)
      .toBe("integral of x squared");
  });

  it("omits typedInput when nothing was typed", async () => {
    // typedInput is transcription ground truth for THIS turn's input;
    // an empty string would tell the model the user typed nothing at a
    // position, which is different from not typing.
    const { adapter, sendMessage } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await act(async () => {
      byLabel(el, "Ask")?.click();
    });
    const payload = sendMessage.mock.calls[0][0] as unknown as {
      metadata?: { whiteboard?: Record<string, unknown> };
    };
    expect(payload.metadata?.whiteboard).not.toHaveProperty("typedInput");
  });

  it("blocks a second Ask while the turn is still running", async () => {
    // The board has no queue: a second atlas mid-turn would describe a
    // canvas the agent has not answered yet.
    const { adapter, sendMessage } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await act(async () => {
      byLabel(el, "Ask")?.click();
    });
    expect(byLabel(el, "Ask")).toHaveProperty("disabled", true);
    await act(async () => {
      byLabel(el, "Ask")?.click();
    });
    expect(sendMessage).toHaveBeenCalledTimes(1);
  });

  it("does not reload the canvas when it adopts its own new session", async () => {
    // The board draws before it has a session; the first Ask creates one
    // and sessionId flips null -> id. Reloading there fetches a canvas
    // that does not exist yet and wipes everything drawn so far.
    const { adapter } = makeAdapter();
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root!.render(<AgentWhiteboard adapter={adapter} sessionId={null} />);
    });
    await act(async () => {
      root!.render(<AgentWhiteboard adapter={adapter} sessionId="s1" />);
    });
    expect(adapter.getWorkspaceFile).not.toHaveBeenCalled();
  });

  it("does reload when switching to a different existing session", async () => {
    const { adapter } = makeAdapter();
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root!.render(<AgentWhiteboard adapter={adapter} sessionId="s1" />);
    });
    await act(async () => {
      root!.render(<AgentWhiteboard adapter={adapter} sessionId="s2" />);
    });
    const calls = (adapter.getWorkspaceFile as unknown as {
      mock: { calls: { sessionId: string }[][] };
    }).mock.calls;
    expect(calls.map((c) => c[0].sessionId)).toEqual(["s1", "s2"]);
  });

  it("pans the board when dragging with the hand tool", async () => {
    // Regression: the setView updater read panFrom.current lazily, so
    // React ran it after the ref had been reassigned — delta zero, board
    // never moved — or after pointerup had nulled it, throwing on .x.
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await act(async () => {
      byLabel(el, "Pan")?.click();
    });
    const canvas = byLabel(el, "Whiteboard canvas") as HTMLCanvasElement;
    canvas.setPointerCapture = () => undefined;
    canvas.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 800, height: 600 }) as DOMRect;

    // The mount paint is the baseline. pointerdown only sets a ref, so
    // it triggers no repaint of its own.
    const before = lastViewTranslation();
    await act(async () => {
      canvas.dispatchEvent(
        new PointerEvent("pointerdown", {
          clientX: 400, clientY: 300, bubbles: true,
        }),
      );
    });

    await act(async () => {
      canvas.dispatchEvent(
        new PointerEvent("pointermove", {
          clientX: 500, clientY: 360, bubbles: true,
        }),
      );
    });
    const after = lastViewTranslation();

    expect(before).not.toBeNull();
    expect(after).not.toBeNull();
    // Dragging right/down reveals content up/left, so the translation
    // moves with the drag.
    expect(after!.x).not.toBe(before!.x);
    expect(after!.y).not.toBe(before!.y);
  });

  it("does not throw when a pan continues past pointerup", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await act(async () => {
      byLabel(el, "Pan")?.click();
    });
    const canvas = byLabel(el, "Whiteboard canvas") as HTMLCanvasElement;
    canvas.setPointerCapture = () => undefined;
    canvas.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 800, height: 600 }) as DOMRect;

    await act(async () => {
      canvas.dispatchEvent(
        new PointerEvent("pointerdown", {
          clientX: 400, clientY: 300, bubbles: true,
        }),
      );
      canvas.dispatchEvent(
        new PointerEvent("pointermove", {
          clientX: 450, clientY: 330, bubbles: true,
        }),
      );
      canvas.dispatchEvent(new PointerEvent("pointerup", { bubbles: true }));
      canvas.dispatchEvent(
        new PointerEvent("pointermove", {
          clientX: 500, clientY: 360, bubbles: true,
        }),
      );
    });
    expect(byLabel(el, "Whiteboard canvas")).not.toBeNull();
  });

  async function moveOver(el: HTMLElement, tool: string) {
    await act(async () => {
      byLabel(el, tool)?.click();
    });
    const canvas = byLabel(el, "Whiteboard canvas") as HTMLCanvasElement;
    canvas.setPointerCapture = () => undefined;
    canvas.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 800, height: 600 }) as DOMRect;
    sharedCalls = [];
    await act(async () => {
      canvas.dispatchEvent(
        new PointerEvent("pointermove", {
          clientX: 300, clientY: 200, bubbles: true,
        }),
      );
    });
    return canvas;
  }

  it("shows a ring under the eraser so its size is visible", async () => {
    // The eraser strokes white on a white canvas, so without the ring
    // there is nothing to show where it is or how much it will take.
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await moveOver(el, "Eraser");
    expect(sharedCalls.some((c) => c[0] === "arc")).toBe(true);
  });

  it("draws the ring with a border, not a bare outline", async () => {
    // Two strokes, dark over light, so the edge reads on blank paper
    // and on dark ink alike.
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await moveOver(el, "Eraser");
    const afterArc = sharedCalls.slice(
      sharedCalls.findIndex((c) => c[0] === "arc"),
    );
    expect(afterArc.filter((c) => c[0] === "stroke").length).toBeGreaterThan(1);
  });

  it("shows no ring for the pen", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await moveOver(el, "Pen");
    expect(sharedCalls.some((c) => c[0] === "arc")).toBe(false);
  });

  it("drops the ring when the pointer leaves the canvas", async () => {
    // Otherwise it stays frozen wherever the pointer left.
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    const canvas = await moveOver(el, "Eraser");
    sharedCalls = [];
    await act(async () => {
      canvas.dispatchEvent(new PointerEvent("pointerleave", { bubbles: true }));
    });
    expect(sharedCalls.some((c) => c[0] === "arc")).toBe(false);
  });

  it("keeps focus on the text editor it just opened", async () => {
    // The browser focuses the canvas on the click following pointerdown,
    // which blurs the textarea — and blur commits, so the editor opened
    // and vanished within one gesture. Preventing the default keeps the
    // focus where it was just put.
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await act(async () => {
      byLabel(el, "Text")?.click();
    });
    const canvas = byLabel(el, "Whiteboard canvas") as HTMLCanvasElement;
    let captured = false;
    canvas.setPointerCapture = () => {
      captured = true;
    };
    canvas.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 800, height: 600 }) as DOMRect;

    const event = new PointerEvent("pointerdown", {
      clientX: 200, clientY: 200, bubbles: true, cancelable: true,
    });
    await act(async () => {
      canvas.dispatchEvent(event);
    });

    expect(event.defaultPrevented).toBe(true);
    // The text tool never drags, so holding the pointer would only stop
    // the editor behaving like an ordinary input.
    expect(captured).toBe(false);
    expect(el.querySelector("textarea")).not.toBeNull();
  });

  it("still captures the pointer for tools that drag", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    const canvas = byLabel(el, "Whiteboard canvas") as HTMLCanvasElement;
    let captured = false;
    canvas.setPointerCapture = () => {
      captured = true;
    };
    canvas.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 800, height: 600 }) as DOMRect;
    await act(async () => {
      canvas.dispatchEvent(
        new PointerEvent("pointerdown", {
          clientX: 200, clientY: 200, bubbles: true,
        }),
      );
    });
    expect(captured).toBe(true);
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
