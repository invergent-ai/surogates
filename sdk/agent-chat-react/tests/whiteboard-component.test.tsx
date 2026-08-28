import { act, StrictMode, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AgentWhiteboard,
  WhiteboardSurface,
} from "../src/components/whiteboard/agent-whiteboard";
import { applyCommands, emptyDoc } from "../src/components/whiteboard/doc";
import { SAVE_DEBOUNCE_MS } from "../src/components/whiteboard/persist";
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
    listSessions: vi.fn(async () => ({ sessions: [], total: 0 })),
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

  it("resumes the most recent board when the route carries no session", async () => {
    // Leaving the board and coming back landed on a blank canvas and
    // silently started a new session, with no route back to what was
    // drawn.
    const onSessionChange = vi.fn();
    const { adapter } = makeAdapter({
      listSessions: vi.fn(async () => ({
        sessions: [
          { id: "chat", status: "active", config: {} },
          {
            id: "board", status: "active",
            config: { surface: "whiteboard" },
            updatedAt: "2026-06-01T00:00:00Z",
          },
        ],
        total: 2,
      })),
    });
    await render(
      <AgentWhiteboard
        adapter={adapter}
        agentId="a1"
        sessionId={null}
        onSessionChange={onSessionChange}
      />,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(onSessionChange).toHaveBeenCalledWith("board");
  });

  it("loads the canvas under StrictMode", async () => {
    // StrictMode runs every effect twice: mount, clean up, mount again.
    // The loader used to claim the session id up front, so the first
    // run's fetch was cancelled and the second saw `previous === next`
    // and skipped loading altogether -- on every cold mount, which is
    // every switch away from the board and back. The board then held
    // nothing but the agent's replayed objects.
    const saved = applyCommands(emptyDoc(), [
      { tool: "write_text", x: 5, y: 5, text: "mine", fontSize: 20,
        maxWidth: 100 },
    ], 1);
    const { adapter } = makeAdapter({
      getWorkspaceFile: vi.fn(async () => ({
        path: "_whiteboard/canvas.json",
        content: JSON.stringify(saved),
        size: 1,
        encoding: "utf-8" as const,
        truncated: false,
      })),
    });
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root!.render(
        <StrictMode>
          <AgentWhiteboard adapter={adapter} sessionId="s1" />
        </StrictMode>,
      );
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(adapter.getWorkspaceFile).toHaveBeenCalled();

    // The fetch must have been *applied*, not merely issued -- the
    // cancelled run reached the server too. Unmounting flushes the live
    // document, so what it writes back is what was on screen.
    await act(async () => {
      root!.unmount();
      root = null;
    });
    const upload = (adapter.uploadWorkspaceFile as unknown as {
      mock: { calls: { file: File }[][] };
    }).mock.calls.at(-1)![0].file;
    const written = JSON.parse(await upload.text()) as {
      objects: { text?: string }[];
    };
    expect(written.objects.some((o) => o.text === "mine")).toBe(true);
  });

  it("loads the canvas of a board it resumed", async () => {
    // The resume path reaches the loader as `null -> id`, the same shape
    // as adopting a session the board just created -- where loading is
    // exactly wrong. Conflating them showed a resumed board holding
    // nothing but the agent's replayed objects, and the next debounced
    // save wrote that back over the user's strokes.
    const onSessionChange = vi.fn();
    const { adapter } = makeAdapter({
      listSessions: vi.fn(async () => ({
        sessions: [{
          id: "board", status: "active", config: { surface: "whiteboard" },
        }],
        total: 1,
      })),
    });
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root!.render(
        <AgentWhiteboard
          adapter={adapter}
          agentId="a1"
          sessionId={null}
          onSessionChange={onSessionChange}
        />,
      );
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(onSessionChange).toHaveBeenCalledWith("board");

    // The host routes to the session it was handed.
    await act(async () => {
      root!.render(
        <AgentWhiteboard
          adapter={adapter}
          agentId="a1"
          sessionId="board"
          onSessionChange={onSessionChange}
        />,
      );
    });
    const calls = (adapter.getWorkspaceFile as unknown as {
      mock: { calls: { sessionId: string }[][] };
    }).mock.calls;
    expect(calls.map((c) => c[0].sessionId)).toContain("board");
  });

  it("does not resume when the route already names a session", async () => {
    const onSessionChange = vi.fn();
    const { adapter } = makeAdapter();
    await render(
      <AgentWhiteboard
        adapter={adapter}
        agentId="a1"
        sessionId="s1"
        onSessionChange={onSessionChange}
      />,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(onSessionChange).not.toHaveBeenCalled();
  });

  it("offers New board only when the host can navigate to one", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    expect(byLabel(el, "New board")).toBeNull();
  });

  it("does not resume after New board is pressed", async () => {
    // Otherwise the resume effect pulls the user straight back into the
    // board they just chose to leave.
    const onSessionChange = vi.fn();
    const onNewBoard = vi.fn();
    const { adapter } = makeAdapter({
      listSessions: vi.fn(async () => ({
        sessions: [{
          id: "board", status: "active", config: { surface: "whiteboard" },
        }],
        total: 1,
      })),
    });
    const el = await render(
      <AgentWhiteboard
        adapter={adapter}
        agentId="a1"
        sessionId="s1"
        onSessionChange={onSessionChange}
        onNewBoard={onNewBoard}
      />,
    );
    await act(async () => {
      byLabel(el, "New board")?.click();
    });
    expect(onNewBoard).toHaveBeenCalled();

    // The host now routes to the session-less board.
    await act(async () => {
      root!.render(
        <AgentWhiteboard
          adapter={adapter}
          agentId="a1"
          sessionId={null}
          onSessionChange={onSessionChange}
          onNewBoard={onNewBoard}
        />,
      );
      await Promise.resolve();
    });
    expect(onSessionChange).not.toHaveBeenCalled();
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

describe("returning to the board after an agent reply", () => {
  const agentDraw = {
    id: "m1", role: "assistant" as const, content: "",
    toolCalls: [{
      id: "call1", toolName: "whiteboard_draw",
      args: JSON.stringify({ commands: [{
        tool: "write_text", x: 600, y: 40, text: "4",
        fontSize: 48, maxWidth: 80,
      }] }),
      status: "complete",
    }],
  };

  /** A runtime holding one finished agent turn, as on a remount. */
  function stubRuntime(messages: unknown[]) {
    return {
      messages,
      isRunning: false,
      send: vi.fn(async () => undefined),
    } as never;
  }

  it("does not save the replayed board before the stored one loads", async () => {
    // The regression, from a real session: switching away and back
    // remounts from emptyDoc(), the fold effect immediately replays the
    // agent's objects onto it, and the autosave wrote that ink-less
    // board over the stored one -- which the load then read back. It
    // needed an agent reply to reproduce: with nothing to fold the
    // document stays empty and the autosave already declines to write
    // an empty one.
    let release!: (v: unknown) => void;
    const pending = new Promise((r) => { release = r; });
    const ink = applyCommands(emptyDoc(), [
      { tool: "write_text", x: 5, y: 5, text: "2 + 2 =", fontSize: 20,
        maxWidth: 200 },
    ], 1);
    const { adapter } = makeAdapter({
      getWorkspaceFile: vi.fn(async () => {
        await pending;
        return {
          path: "_whiteboard/canvas.json",
          content: JSON.stringify(ink),
          size: 1,
          encoding: "utf-8" as const,
          truncated: false,
        };
      }),
    });

    await render(
      <WhiteboardSurface
        adapter={adapter}
        sessionId="s1"
        runtime={stubRuntime([agentDraw])}
      />,
    );
    // Let the fold land and the autosave debounce elapse, with the load
    // still outstanding.
    await act(async () => {
      await new Promise((r) => setTimeout(r, SAVE_DEBOUNCE_MS * 2));
    });
    expect(adapter.uploadWorkspaceFile).not.toHaveBeenCalled();

    release(null);
    await act(async () => { await Promise.resolve(); });

    // And once it lands, what is on screen -- and so what any later save
    // writes -- is the stored board plus the agent's objects.
    await act(async () => {
      root!.unmount();
      root = null;
    });
    const calls = (adapter.uploadWorkspaceFile as unknown as {
      mock: { calls: { file: File }[][] };
    }).mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    const written = JSON.parse(await calls[calls.length - 1][0].file.text());
    expect(written.objects).toHaveLength(2);
  });
});

describe("the restoring indicator", () => {
  function deferredAdapter(doc: unknown) {
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const { adapter } = makeAdapter({
      getWorkspaceFile: vi.fn(async () => {
        await gate;
        return {
          path: "_whiteboard/canvas.json",
          content: JSON.stringify(doc),
          size: 1,
          encoding: "utf-8" as const,
          truncated: false,
        };
      }),
    });
    return { adapter, release };
  }

  const stored = () =>
    applyCommands(emptyDoc(), [
      { tool: "write_text", x: 5, y: 5, text: "mine", fontSize: 20,
        maxWidth: 100 },
    ], 1);

  it("refuses strokes while the stored board is still loading", async () => {
    // The point of the indicator: the load replaces the document
    // wholesale, so a stroke drawn now is discarded without trace.
    const { adapter } = deferredAdapter(stored());
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    const canvas = el.querySelector<HTMLElement>(
      '[aria-label="Whiteboard canvas"]',
    );
    expect(canvas?.className).toContain("pointer-events-none");
  });

  it("blocks Ask until the board is restored", async () => {
    const { adapter, release } = deferredAdapter(stored());
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    expect((byLabel(el, "Ask") as HTMLButtonElement).disabled).toBe(true);

    release();
    await act(async () => { await Promise.resolve(); });
    expect((byLabel(el, "Ask") as HTMLButtonElement).disabled).toBe(false);
  });

  it("says nothing when the load is quick", async () => {
    // A warm switch back resolves inside the delay; a spinner flashing
    // on every toggle would be worse than silence.
    const { adapter, release } = deferredAdapter(stored());
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    release();
    await act(async () => { await Promise.resolve(); });
    expect(el.textContent).not.toContain("Restoring board");
  });

  it("says so when the load drags", async () => {
    const { adapter, release } = deferredAdapter(stored());
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId="s1" />,
    );
    await act(async () => {
      await new Promise((r) => setTimeout(r, 400));
    });
    expect(el.textContent).toContain("Restoring board");

    release();
    await act(async () => { await Promise.resolve(); });
    expect(el.textContent).not.toContain("Restoring board");
  });

  it("does not veil a fresh board that has nothing to load", async () => {
    // No session means no stored canvas: drawing must work instantly.
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId={null} />,
    );
    const canvas = el.querySelector<HTMLElement>(
      '[aria-label="Whiteboard canvas"]',
    );
    expect(canvas?.className).not.toContain("pointer-events-none");
    expect(el.textContent).not.toContain("Restoring board");
  });
});

describe("what the board shows while it loads", () => {
  it("paints nothing until the stored board lands", async () => {
    // The agent's objects replay out of the message list instantly. Fold
    // them onto the empty document a remount starts from and the user
    // watches a board they never had -- the answer with none of their
    // working -- until the fetch replaces it.
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const ink = applyCommands(emptyDoc(), [
      { tool: "write_text", x: 5, y: 5, text: "1 + 2 =", fontSize: 20,
        maxWidth: 200 },
    ], 1);
    const { adapter } = makeAdapter({
      getWorkspaceFile: vi.fn(async () => {
        await gate;
        return {
          path: "_whiteboard/canvas.json",
          content: JSON.stringify(ink),
          size: 1,
          encoding: "utf-8" as const,
          truncated: false,
        };
      }),
    });

    sharedCalls.length = 0;
    await render(
      <WhiteboardSurface
        adapter={adapter}
        sessionId="s1"
        runtime={{
          messages: [{
            id: "m1", role: "assistant", content: "",
            toolCalls: [{
              id: "call1", toolName: "whiteboard_draw",
              // Not a bare digit: the grid overlay paints labels 0..15,
              // and the assertion below must distinguish the agent's
              // object from the scaffolding drawn around it.
              args: JSON.stringify({ commands: [{
                tool: "write_text", x: 600, y: 40, text: "ANSWER",
                fontSize: 48, maxWidth: 200,
              }] }),
              status: "complete",
            }],
          }],
          isRunning: false,
          send: vi.fn(async () => undefined),
        } as never}
      />,
    );
    await act(async () => { await Promise.resolve(); });

    const painted = () =>
      sharedCalls.filter((c) => c[0] === "fillText").map((c) => c[1]);
    expect(painted()).not.toContain("ANSWER");

    release();
    await act(async () => { await Promise.resolve(); });

    // And once it lands, both are on the board together.
    expect(painted()).toContain("ANSWER");
    expect(painted()).toContain("1 + 2 =");
  });
});

describe("panning with the middle button", () => {
  async function board() {
    const { adapter } = makeAdapter();
    const el = await render(
      <AgentWhiteboard adapter={adapter} sessionId={null} />,
    );
    const canvas = byLabel(el, "Whiteboard canvas") as HTMLCanvasElement;
    canvas.setPointerCapture = () => undefined;
    canvas.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 800, height: 600 }) as DOMRect;
    return { el, canvas };
  }

  const drag = (canvas: HTMLCanvasElement, button: number) =>
    act(async () => {
      canvas.dispatchEvent(
        new PointerEvent("pointerdown", {
          clientX: 400, clientY: 300, button, bubbles: true, cancelable: true,
        }),
      );
      canvas.dispatchEvent(
        new PointerEvent("pointermove", {
          clientX: 460, clientY: 340, bubbles: true,
        }),
      );
      canvas.dispatchEvent(new PointerEvent("pointerup", { bubbles: true }));
    });

  it("moves the board whatever tool is in hand", async () => {
    // The pen is the default tool: without this, panning means reaching
    // for the pan tool and back again mid-drawing.
    const { canvas } = await board();
    const before = lastViewTranslation();
    await drag(canvas, 1);
    const after = lastViewTranslation();

    expect(after).not.toEqual(before);
    // Dragged right and down, so the view origin moves left and up.
    expect(after!.x).toBeGreaterThan(before!.x);
    expect(after!.y).toBeGreaterThan(before!.y);
  });

  it("draws no ink while doing it", async () => {
    const { el, canvas } = await board();
    await drag(canvas, 1);
    // A stroke would be an undoable edit; panning is not one.
    expect(byLabel(el, "Undo")).toHaveProperty("disabled", true);
  });

  it("suppresses the browser's middle-click autoscroll", async () => {
    const { canvas } = await board();
    const down = new PointerEvent("pointerdown", {
      clientX: 400, clientY: 300, button: 1, bubbles: true, cancelable: true,
    });
    await act(async () => { canvas.dispatchEvent(down); });
    expect(down.defaultPrevented).toBe(true);
  });

  it("leaves the left button drawing", async () => {
    const { el, canvas } = await board();
    await drag(canvas, 0);
    expect(byLabel(el, "Undo")).toHaveProperty("disabled", false);
  });
});

describe("progress while the agent works", () => {
  const runtimeWith = (over: Record<string, unknown>) =>
    ({
      messages: [], isRunning: false, send: vi.fn(async () => undefined),
      ...over,
    }) as never;

  const summaryMessage = (summary: string) => ({
    id: "m1", role: "assistant" as const, content: "",
    iterationSummary: {
      iterationIndex: 0, summary, toolCallIds: [],
      startedAt: "", endedAt: "",
    },
  });

  it("says nothing when idle", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <WhiteboardSurface adapter={adapter} sessionId="s1"
        runtime={runtimeWith({})} />,
    );
    expect(el.textContent).not.toContain("Thinking");
  });

  it("shows it is working on an ordinary Ask", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <WhiteboardSurface adapter={adapter} sessionId="s1"
        runtime={runtimeWith({ isRunning: true })} />,
    );
    expect(el.textContent).toContain("Thinking…");
  });

  it("reports the agent's own step once it has one", async () => {
    // A deep turn is many round-trips; without its steps a minute of
    // real work is indistinguishable from a hang.
    const { adapter } = makeAdapter();
    const el = await render(
      <WhiteboardSurface adapter={adapter} sessionId="s1"
        runtime={runtimeWith({
          isRunning: true,
          messages: [summaryMessage("Searching for the integral rule")],
        })} />,
    );
    expect(el.textContent).toContain("Searching for the integral rule");
  });

  it("prefers the newest step", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <WhiteboardSurface adapter={adapter} sessionId="s1"
        runtime={runtimeWith({
          isRunning: true,
          messages: [summaryMessage("first"), summaryMessage("second")],
        })} />,
    );
    expect(el.textContent).toContain("second");
    expect(el.textContent).not.toContain("first");
  });

  it("holds the board while the agent works", async () => {
    // Ink added now is invisible to the agent: the atlas and the
    // occupied cells it is answering were captured at Ask.
    const { adapter } = makeAdapter();
    const el = await render(
      <WhiteboardSurface adapter={adapter} sessionId="s1"
        runtime={runtimeWith({ isRunning: true })} />,
    );
    // The overlay never takes the pointer itself...
    const overlay = Array.from(el.querySelectorAll("div")).find((d) =>
      d.className.includes("pointer-events-none") &&
      d.textContent?.includes("Thinking…"),
    );
    expect(overlay).toBeTruthy();

    const canvas = byLabel(el, "Whiteboard canvas") as HTMLCanvasElement;
    expect(canvas.className).toContain("pointer-events-none");
  });

  it("hands the board back once the turn ends", async () => {
    const { adapter } = makeAdapter();
    const el = await render(
      <WhiteboardSurface adapter={adapter} sessionId="s1"
        runtime={runtimeWith({})} />,
    );
    const canvas = byLabel(el, "Whiteboard canvas") as HTMLCanvasElement;
    expect(canvas.className).not.toContain("pointer-events-none");
  });
});
