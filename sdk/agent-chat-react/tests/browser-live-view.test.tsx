import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BrowserLiveView } from "../src/components/browser/browser-live-view";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

// Stub noVNC (browser-only) and capture the listeners the component registers
// so a test can drive a disconnect. Hoisted so the vi.mock factory can use it.
const rfbMock = vi.hoisted(() => ({
  connect: vi.fn(),
  listeners: {} as Record<string, (event: Event) => void>,
}));

vi.mock("@novnc/novnc", () => ({
  default: vi.fn().mockImplementation((_el: HTMLElement, url: string) => {
    rfbMock.connect(url);
    rfbMock.listeners = {};
    return {
      disconnect: vi.fn(),
      addEventListener: (type: string, cb: (event: Event) => void) => {
        rfbMock.listeners[type] = cb;
      },
      removeEventListener: (type: string) => {
        delete rfbMock.listeners[type];
      },
      viewOnly: false,
      scaleViewport: false,
    };
  }),
}));

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  rfbMock.connect.mockClear();
});

async function renderView(element: ReactElement): Promise<HTMLDivElement> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(element);
  });
  // Flush the lazy import("@novnc/novnc") microtasks so the RFB is constructed.
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  return container;
}

const SRC = "https://ops.example/api/sessions/s1/browser/live/?token=t";

describe("BrowserLiveView", () => {
  it("connects RFB to a wss:// url derived from src", async () => {
    await renderView(<BrowserLiveView src={SRC} />);
    expect(rfbMock.connect).toHaveBeenCalledWith(
      "wss://ops.example/api/sessions/s1/browser/live/?token=t",
    );
  });

  it("passes an existing ws:// url through unchanged", async () => {
    await renderView(
      <BrowserLiveView src="ws://localhost:8888/api/sessions/s1/browser/live/?token=t" />,
    );
    expect(rfbMock.connect).toHaveBeenCalledWith(
      "ws://localhost:8888/api/sessions/s1/browser/live/?token=t",
    );
  });

  it("renders a canvas container with the rfb test id", async () => {
    const node = await renderView(<BrowserLiveView src={SRC} />);
    expect(node.querySelector('[data-testid="browser-rfb"]')).not.toBeNull();
  });

  it("zooms the viewport through noVNC scaling (sizing the target, not transforming it)", async () => {
    const node = await renderView(<BrowserLiveView src={SRC} />);
    // Zoom controls only appear once connected.
    await act(async () => {
      rfbMock.listeners.connect?.(new CustomEvent("connect"));
    });

    const target = node.querySelector(
      '[data-testid="browser-rfb"]',
    ) as HTMLElement;
    const zoomOut = node.querySelector(
      '[aria-label="Zoom out"]',
    ) as HTMLButtonElement;
    const zoomIn = node.querySelector(
      '[aria-label="Zoom in"]',
    ) as HTMLButtonElement;
    const reset = node.querySelector(
      '[aria-label="Reset zoom"]',
    ) as HTMLButtonElement;

    // Fit-to-pane floor: target is exactly the viewport and cannot zoom out.
    expect(target.style.width).toBe("100%");
    expect(target.style.height).toBe("100%");
    expect(zoomOut.disabled).toBe(true);
    expect(target.style.transform).toBe("");

    await act(async () => zoomIn.click());
    expect(target.style.width).toBe("150%");
    expect(target.style.height).toBe("150%");
    expect(zoomOut.disabled).toBe(false);

    // Clamp at the 3× ceiling: 150 → 200 → 250 → 300, then no further.
    await act(async () => zoomIn.click());
    await act(async () => zoomIn.click());
    await act(async () => zoomIn.click());
    expect(target.style.width).toBe("300%");
    expect(zoomIn.disabled).toBe(true);
    await act(async () => zoomIn.click());
    expect(target.style.width).toBe("300%");

    await act(async () => reset.click());
    expect(target.style.width).toBe("100%");
    expect(zoomOut.disabled).toBe(true);
  });

  it("calls onDisconnect and shows an overlay when the connection drops", async () => {
    const onDisconnect = vi.fn();
    const node = await renderView(
      <BrowserLiveView src={SRC} onDisconnect={onDisconnect} />,
    );

    await act(async () => {
      rfbMock.listeners.disconnect?.(
        new CustomEvent("disconnect", { detail: { clean: false } }),
      );
    });

    expect(onDisconnect).toHaveBeenCalledWith(false);
    expect(
      node.querySelector('[data-testid="browser-rfb-overlay"]')?.textContent,
    ).toContain("disconnected");
  });
});
