import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  BrowserShell,
  normalizePoint,
} from "../src/components/browser/browser-shell";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

/** Captures what the component sends and lets a test push messages back. */
class FakeSocket {
  static last: FakeSocket | null = null;
  static OPEN = 1;

  readyState = 1;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number; wasClean: boolean }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(public url: string) {
    FakeSocket.last = this;
  }

  send(payload: string): void {
    this.sent.push(payload);
  }

  close(): void {
    this.closed = true;
  }

  messages(): Array<Record<string, unknown>> {
    return this.sent.map((raw) => JSON.parse(raw));
  }
}

vi.stubGlobal("WebSocket", FakeSocket);
// Stub only the statics: replacing URL wholesale would take the constructor
// with it, and the component builds its socket URL with `new URL(...)`.
URL.createObjectURL = vi.fn(() => "blob:frame") as typeof URL.createObjectURL;
URL.revokeObjectURL = vi.fn() as typeof URL.revokeObjectURL;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  FakeSocket.last = null;
});

async function render(element: ReactElement): Promise<HTMLDivElement> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(element);
  });
  await act(async () => {
    FakeSocket.last?.onopen?.();
  });
  return container;
}

function push(message: Record<string, unknown>): Promise<void> {
  return act(async () => {
    FakeSocket.last?.onmessage?.({ data: JSON.stringify(message) });
  });
}

const TABS = [
  { id: "t1", title: "Wikipedia", url: "https://en.wikipedia.org/", active: true },
  { id: "t2", title: "Stripe Docs", url: "https://docs.stripe.com/", active: false },
];

describe("normalizePoint", () => {
  // object-fit: contain letterboxes, so the rendered image is not the element
  // box. Normalizing against the element would drift by the letterbox.
  const rect = { left: 0, top: 0, width: 400, height: 400 };

  it("maps the centre of a letterboxed image to 0.5, 0.5", () => {
    // A 2:1 frame in a square box renders 400x200, offset 100px from the top.
    expect(normalizePoint(200, 200, rect, 800, 400)).toEqual({ x: 0.5, y: 0.5 });
  });

  it("maps the image's top-left corner to 0, 0", () => {
    expect(normalizePoint(0, 100, rect, 800, 400)).toEqual({ x: 0, y: 0 });
  });

  it("returns null for a point in the letterbox, not a clamped one", () => {
    // Clamping would silently turn a click on the background into a click on
    // the page's top edge.
    expect(normalizePoint(200, 20, rect, 800, 400)).toBeNull();
  });

  it("handles an unknown intrinsic size by using the whole box", () => {
    expect(normalizePoint(200, 200, rect, 0, 0)).toEqual({ x: 0.5, y: 0.5 });
  });
});

describe("BrowserShell", () => {
  it("connects to the given url", async () => {
    await render(<BrowserShell src="wss://x/shell" hasControl={false} />);
    expect(FakeSocket.last?.url).toBe("wss://x/shell");
  });

  it("renders a binary frame", async () => {
    const node = await render(<BrowserShell src="wss://x/shell" hasControl />);
    await act(async () => {
      FakeSocket.last?.onmessage?.({ data: new Blob([new Uint8Array([1, 2])]) });
    });
    const image = node.querySelector<HTMLImageElement>(
      "[data-testid='browser-shell-frame']",
    );
    expect(image?.getAttribute("src")).toBe("blob:frame");
  });

  it("lists tabs and switches on click", async () => {
    const node = await render(<BrowserShell src="wss://x/shell" hasControl />);
    await push({ t: "tabs", tabs: TABS });

    const tabs = node.querySelectorAll("[data-testid='browser-shell-tab']");
    expect(tabs).toHaveLength(2);

    await act(async () => {
      (tabs[1] as HTMLButtonElement).click();
    });
    expect(FakeSocket.last?.messages()).toContainEqual({
      t: "switch_tab",
      id: "t2",
    });
  });

  it("hides the tab strip when there is only one tab", async () => {
    // The common case: 44px of chrome instead of 78px.
    const node = await render(<BrowserShell src="wss://x/shell" hasControl />);
    await push({ t: "tabs", tabs: [TABS[0]] });
    expect(
      node.querySelector("[data-testid='browser-shell-tabs']"),
    ).toBeNull();
  });

  it("seeds the address bar from the active tab", async () => {
    // On connect the server sends `tabs`, not `nav` — a nav message only
    // arrives on the next navigation. The bar must not sit empty until then,
    // nor keep the previous tab's address after a switch.
    const node = await render(<BrowserShell src="wss://x/shell" hasControl />);
    await push({ t: "tabs", tabs: TABS });
    const field = node.querySelector("[data-testid='browser-shell-url']");
    expect(field?.textContent).toContain("en.wikipedia.org");
  });

  it("shows the url from a nav message", async () => {
    const node = await render(<BrowserShell src="wss://x/shell" hasControl />);
    await push({ t: "nav", url: "https://example.com/page", title: "Example" });
    const field = node.querySelector("[data-testid='browser-shell-url']");
    expect(field?.textContent).toContain("example.com");
  });

  it("sends normalized coordinates, never pixels", async () => {
    const node = await render(<BrowserShell src="wss://x/shell" hasControl />);
    const image = node.querySelector<HTMLImageElement>(
      "[data-testid='browser-shell-frame']",
    )!;
    image.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 200, height: 100 }) as DOMRect;
    Object.defineProperty(image, "naturalWidth", { value: 400, writable: true });
    Object.defineProperty(image, "naturalHeight", { value: 200, writable: true });

    await act(async () => {
      image.dispatchEvent(
        new MouseEvent("mousedown", { clientX: 100, clientY: 50, bubbles: true }),
      );
    });

    const click = FakeSocket.last
      ?.messages()
      .find((message) => message.t === "click");
    expect(click).toMatchObject({ t: "click", x: 0.5, y: 0.5 });
  });

  it("sends nothing without control", async () => {
    const node = await render(
      <BrowserShell src="wss://x/shell" hasControl={false} />,
    );
    const image = node.querySelector<HTMLImageElement>(
      "[data-testid='browser-shell-frame']",
    )!;
    await act(async () => {
      image.dispatchEvent(
        new MouseEvent("mousedown", { clientX: 10, clientY: 10, bubbles: true }),
      );
    });
    expect(
      FakeSocket.last?.messages().filter((m) => m.t === "click"),
    ).toHaveLength(0);
  });

  it("marks the take-control button held, keeping one glyph", async () => {
    const node = await render(
      <BrowserShell src="wss://x/shell" hasControl onToggleControl={vi.fn()} />,
    );
    const button = node.querySelector(
      "[data-testid='browser-shell-control']",
    ) as HTMLButtonElement;
    expect(button.getAttribute("aria-pressed")).toBe("true");
    // Colour carries the mode; a second glyph beside Reload would read as
    // another refresh button.
    const held = button.innerHTML;

    await act(async () => {
      root?.render(<BrowserShell src="wss://x/shell" hasControl={false} />);
    });
    const idle = node.querySelector(
      "[data-testid='browser-shell-control']",
    ) as HTMLButtonElement;
    expect(idle.getAttribute("aria-pressed")).toBe("false");
    expect(idle.innerHTML).toBe(held);
  });

  it("reports an unexpected close", async () => {
    const onDisconnect = vi.fn();
    await render(
      <BrowserShell
        src="wss://x/shell"
        hasControl
        onDisconnect={onDisconnect}
      />,
    );
    await act(async () => {
      FakeSocket.last?.onclose?.({ code: 1006, wasClean: false });
    });
    expect(onDisconnect).toHaveBeenCalledWith(false);
  });

  it("does not report a close caused by unmounting", async () => {
    const onDisconnect = vi.fn();
    await render(
      <BrowserShell
        src="wss://x/shell"
        hasControl
        onDisconnect={onDisconnect}
      />,
    );
    await act(async () => root?.unmount());
    root = null;
    expect(onDisconnect).not.toHaveBeenCalled();
  });
});
