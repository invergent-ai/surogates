import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useBrowserPreview } from "../src/components/browser/use-browser-preview";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

/** Mount the hook and expose whatever it last returned. */
async function mount(options: {
  getBrowserPreviewSnapshot?: (id: string) => Promise<{ src: string } | null>;
  enabled?: boolean;
}): Promise<{ current: () => string | null }> {
  const seen = { value: null as string | null };
  function Probe() {
    seen.value = useBrowserPreview({
      adapter: {
        getBrowserPreviewSnapshot: options.getBrowserPreviewSnapshot,
      } as never,
      sessionId: "s-1",
      enabled: options.enabled ?? true,
    });
    return null;
  }
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(createElement(Probe));
  });
  return { current: () => seen.value };
}

describe("useBrowserPreview", () => {
  it("fetches a snapshot on mount and exposes its src", async () => {
    const get = vi.fn(async () => ({ src: "data:image/png;base64,AAA" }));
    const probe = await mount({ getBrowserPreviewSnapshot: get });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(get).toHaveBeenCalledTimes(1);
    expect(probe.current()).toBe("data:image/png;base64,AAA");
  });

  it("refreshes on the poll interval", async () => {
    const get = vi.fn(async () => ({ src: "x" }));
    await mount({ getBrowserPreviewSnapshot: get });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(get).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(31_000);
    });
    // 15s cadence: two more refreshes inside 31s.
    expect(get.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("stops polling once the endpoint says there is no browser", async () => {
    // An empty answer will not change on retry, and every retry writes an
    // error into the proxied server's log.
    const get = vi.fn(async () => null);
    await mount({ getBrowserPreviewSnapshot: get });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(get).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("gives up after repeated thrown requests", async () => {
    const get = vi.fn(async () => {
      throw new Error("502");
    });
    await mount({ getBrowserPreviewSnapshot: get });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    // First attempt plus two retries, then silence.
    expect(get).toHaveBeenCalledTimes(3);
  });

  it("keeps the last good frame when a refresh fails", async () => {
    let call = 0;
    const get = vi.fn(async () => {
      call += 1;
      if (call === 1) return { src: "first" };
      throw new Error("boom");
    });
    const probe = await mount({ getBrowserPreviewSnapshot: get });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(probe.current()).toBe("first");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(16_000);
    });
    // A transient failure must not blank the card.
    expect(probe.current()).toBe("first");
  });

  it("does not poll while the tab is hidden", async () => {
    // A card nobody is looking at should not keep screenshotting the agent's
    // browser. Defined and deleted by hand: vi.spyOn on this prototype getter
    // is not undone by restoreAllMocks, and a leaked "hidden" makes every
    // later test skip its refresh and fail for the wrong reason.
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => true,
    });
    try {
      const get = vi.fn(async () => ({ src: "x" }));
      await mount({ getBrowserPreviewSnapshot: get });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      expect(get).not.toHaveBeenCalled();
    } finally {
      delete (document as unknown as { hidden?: unknown }).hidden;
    }
  });

  it("does nothing when disabled", async () => {
    const get = vi.fn(async () => ({ src: "x" }));
    const probe = await mount({
      getBrowserPreviewSnapshot: get,
      enabled: false,
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(get).not.toHaveBeenCalled();
    expect(probe.current()).toBeNull();
  });

  it("survives an adapter that has no preview capability", async () => {
    const probe = await mount({ getBrowserPreviewSnapshot: undefined });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(probe.current()).toBeNull();
  });
});
