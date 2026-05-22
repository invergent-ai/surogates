import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BrowserPane } from "../src/components/browser/browser-pane";
import { NO_BROWSER_ADAPTER } from "../src/adapter-context";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const liveAdapter = {
  ...NO_BROWSER_ADAPTER,
  async getBrowserState() {
    return {
      status: "live" as const,
      controlOwner: null,
      liveViewPath: "/v1/sessions/s/browser/live/",
    };
  },
  async acquireBrowserControl() {
    return { outcome: "granted" as const, ownerUserId: "u" };
  },
  async releaseBrowserControl() {},
  async getBrowserPreviewSnapshot() {
    return { src: "data:image/png;base64,cHJldmlldw==" };
  },
  browserLiveViewUrl() {
    return "about:blank#browser-live";
  },
};

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

function renderPane(element: React.ReactElement) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(element);
  });
  return container;
}

async function flushPreview() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("BrowserPane", () => {
  it("renders a passive screenshot preview without mounting the live-view iframe", async () => {
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={liveAdapter}
      />,
    );

    expect(node.querySelector('[data-testid="browser-iframe"]')).toBeNull();

    await flushPreview();

    const preview = node.querySelector<HTMLImageElement>(
      '[data-testid="browser-preview-image"]',
    );
    const iframe = node.querySelector<HTMLIFrameElement>(
      '[data-testid="browser-iframe"]',
    );
    expect(preview?.getAttribute("src")).toBe(
      "data:image/png;base64,cHJldmlldw==",
    );
    expect(preview?.className).toContain("object-contain");
    expect(iframe).toBeNull();
    expect(
      node.querySelector('button[aria-label="Open browser preview"]'),
    ).toBeNull();
  });

  it("opens passive preview in a full-page dialog without mounting live view", async () => {
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={liveAdapter}
      />,
    );
    await flushPreview();

    const maximizeButton = node.querySelector<HTMLButtonElement>(
      'button[aria-label="Maximize browser"]',
    );
    expect(maximizeButton).not.toBeNull();

    await act(async () => {
      maximizeButton?.click();
    });

    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]');
    const preview = document.body.querySelector<HTMLImageElement>(
      '[data-testid="browser-fullscreen-preview-image"]',
    );
    const iframe = document.body.querySelector<HTMLIFrameElement>(
      '[data-testid="browser-fullscreen-iframe"]',
    );

    expect(dialog).not.toBeNull();
    expect(dialog?.textContent).toContain("Browser");
    expect(preview?.getAttribute("src")).toBe(
      "data:image/png;base64,cHJldmlldw==",
    );
    expect(preview?.className).toContain("object-contain");
    expect(iframe).toBeNull();
  });

  it("does not mount live view from replayed user-control state after refresh", async () => {
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "user-control", controlOwner: "user-A" }}
        adapter={liveAdapter}
      />,
    );

    expect(node.textContent).toContain("user-A has control");
    expect(node.textContent).toContain("Take control");
    expect(
      node.querySelector<HTMLIFrameElement>('[data-testid="browser-iframe"]'),
    ).toBeNull();
    await flushPreview();
    expect(
      node.querySelector<HTMLImageElement>(
        '[data-testid="browser-preview-image"]',
      ),
    ).not.toBeNull();
  });

  it("shows Take control button in live state", async () => {
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={liveAdapter}
      />,
    );
    await flushPreview();

    expect(node.textContent).toContain("Take control");
  });

  it("opens the live-view dialog after this tab acquires control", async () => {
    let resolveAcquire:
      | ((value: { outcome: "granted"; ownerUserId: string }) => void)
      | null = null;
    const controlledAdapter = {
      ...liveAdapter,
      async acquireBrowserControl() {
        return await new Promise<{ outcome: "granted"; ownerUserId: string }>(
          (resolve) => {
            resolveAcquire = resolve;
          },
        );
      },
    };

    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={controlledAdapter}
      />,
    );

    const takeControlButton = Array.from(
      node.querySelectorAll<HTMLButtonElement>("button"),
    ).find((button) => button.textContent?.includes("Take control"));
    expect(takeControlButton).not.toBeNull();

    await act(async () => {
      takeControlButton?.click();
    });

    expect(document.body.querySelector<HTMLElement>('[role="dialog"]')).toBeNull();

    await act(async () => {
      resolveAcquire?.({ outcome: "granted", ownerUserId: "u" });
    });

    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(dialog?.textContent).toContain("Browser");
    expect(
      document.body.querySelector<HTMLIFrameElement>(
        '[data-testid="browser-fullscreen-iframe"]',
      )?.getAttribute("src"),
    ).toBe("about:blank#browser-live");
  });

  it("does NOT release browser control when the fullscreen dialog closes", async () => {
    // Control and fullscreen are orthogonal: the inline live view also
    // requires control, so closing the fullscreen dialog (Esc / click
    // outside / close button) must not tear down the held lease.
    // Releasing control is the user's job via "Return control".
    let releaseCount = 0;
    const controlledAdapter = {
      ...liveAdapter,
      async releaseBrowserControl() {
        releaseCount += 1;
      },
    };

    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={controlledAdapter}
      />,
    );

    const takeControlButton = Array.from(
      node.querySelectorAll<HTMLButtonElement>("button"),
    ).find((button) => button.textContent?.includes("Take control"));

    await act(async () => {
      takeControlButton?.click();
    });

    expect(document.body.querySelector<HTMLElement>('[role="dialog"]')).not.toBeNull();

    const closeButton = Array.from(
      document.body.querySelectorAll<HTMLButtonElement>("button"),
    ).find((button) => button.textContent?.includes("Close"));
    expect(closeButton).not.toBeNull();

    await act(async () => {
      closeButton?.click();
    });

    // Inline live view must still be mounted (default testid).
    expect(
      node.querySelector<HTMLIFrameElement>(
        '[data-testid="browser-iframe"]',
      ),
    ).not.toBeNull();
    // And no release was issued.
    expect(releaseCount).toBe(0);
  });

  it("refreshes browser control on a heartbeat while live view is open", async () => {
    vi.useFakeTimers();
    try {
      let acquireCount = 0;
      const controlledAdapter = {
        ...liveAdapter,
        async acquireBrowserControl() {
          acquireCount += 1;
          return { outcome: "granted" as const, ownerUserId: "u" };
        },
      };

      const node = renderPane(
        <BrowserPane
          sessionId="s"
          state={{ status: "live", controlOwner: null }}
          adapter={controlledAdapter}
        />,
      );

      const takeControlButton = Array.from(
        node.querySelectorAll<HTMLButtonElement>("button"),
      ).find((button) => button.textContent?.includes("Take control"));

      await act(async () => {
        takeControlButton?.click();
      });

      // After the initial "Take control" click, acquireCount === 1.
      expect(acquireCount).toBe(1);

      // Advance ~60s — heartbeat fires every 25s, so we expect two extra
      // refreshes at 25s and 50s.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });

      expect(acquireCount).toBeGreaterThanOrEqual(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders the Close button only when onClose is provided", async () => {
    const without = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={liveAdapter}
      />,
    );
    const closeButtons = Array.from(
      without.querySelectorAll<HTMLButtonElement>("button"),
    ).filter((button) => button.textContent?.trim() === "Close");
    expect(closeButtons.length).toBe(0);

    const onClose = vi.fn();
    const withProp = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={liveAdapter}
        onClose={onClose}
      />,
    );
    const closeButton = Array.from(
      withProp.querySelectorAll<HTMLButtonElement>("button"),
    ).find((button) => button.textContent?.trim() === "Close");
    expect(closeButton).not.toBeNull();
  });

  it("Close shows a confirmation dialog and does nothing on Cancel", async () => {
    const onClose = vi.fn();
    const closeBrowserSession = vi.fn(async () => {});
    const controlledAdapter = {
      ...liveAdapter,
      closeBrowserSession,
    };

    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={controlledAdapter}
        onClose={onClose}
      />,
    );

    const closeButton = Array.from(
      node.querySelectorAll<HTMLButtonElement>("button"),
    ).find((button) => button.textContent?.trim() === "Close");
    await act(async () => {
      closeButton?.click();
    });

    // Dialog appears.
    const dialog = document.body.querySelector<HTMLElement>(
      '[role="dialog"]',
    );
    expect(dialog).not.toBeNull();
    expect(dialog?.textContent).toContain("Close browser session?");

    // Click Cancel.
    const cancelButton = Array.from(
      document.body.querySelectorAll<HTMLButtonElement>("button"),
    ).find((button) => button.textContent?.trim() === "Cancel");
    await act(async () => {
      cancelButton?.click();
    });

    expect(closeBrowserSession).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("Close → Confirm calls closeBrowserSession, releases control, then onClose", async () => {
    const onClose = vi.fn();
    let releaseCount = 0;
    const closeBrowserSession = vi.fn(async () => {});
    const controlledAdapter = {
      ...liveAdapter,
      async releaseBrowserControl() {
        releaseCount += 1;
      },
      closeBrowserSession,
    };

    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={controlledAdapter}
        onClose={onClose}
      />,
    );

    // Acquire control first so the release-before-close branch runs.
    const takeControlButton = Array.from(
      node.querySelectorAll<HTMLButtonElement>("button"),
    ).find((button) => button.textContent?.includes("Take control"));
    await act(async () => {
      takeControlButton?.click();
    });

    // Click Close → open confirm.
    const closeButton = Array.from(
      node.querySelectorAll<HTMLButtonElement>("button"),
    ).find((button) => button.textContent?.trim() === "Close");
    await act(async () => {
      closeButton?.click();
    });

    // Click the destructive confirm in the dialog.
    const confirmButton = Array.from(
      document.body.querySelectorAll<HTMLButtonElement>("button"),
    ).find((button) => button.textContent?.trim() === "Close browser");
    expect(confirmButton).not.toBeNull();
    await act(async () => {
      confirmButton?.click();
    });

    expect(releaseCount).toBe(1);
    expect(closeBrowserSession).toHaveBeenCalledTimes(1);
    expect(closeBrowserSession).toHaveBeenCalledWith("s");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("Close → Confirm without closeBrowserSession adapter only signals close", async () => {
    const onClose = vi.fn();
    let releaseCount = 0;
    const controlledAdapter = {
      ...liveAdapter,
      async releaseBrowserControl() {
        releaseCount += 1;
      },
      // intentionally no closeBrowserSession
    };

    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={controlledAdapter}
        onClose={onClose}
      />,
    );

    const closeButton = Array.from(
      node.querySelectorAll<HTMLButtonElement>("button"),
    ).find((button) => button.textContent?.trim() === "Close");
    await act(async () => {
      closeButton?.click();
    });

    // Dialog text reflects the no-backend-close flavor.
    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]');
    expect(dialog?.textContent).toContain("hides the browser panel");

    const confirmButton = Array.from(
      document.body.querySelectorAll<HTMLButtonElement>("button"),
    ).find((button) => button.textContent?.trim() === "Close browser");
    await act(async () => {
      confirmButton?.click();
    });

    expect(releaseCount).toBe(0);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not show Return control for replayed user control", async () => {
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "user-control", controlOwner: "user-A" }}
        adapter={liveAdapter}
      />,
    );

    await flushPreview();

    expect(node.textContent).toContain("Take control");
    expect(node.textContent).not.toContain("Return control");
  });

  it("shows skeleton in provisioning state", () => {
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "provisioning", controlOwner: null }}
        adapter={liveAdapter}
      />,
    );

    expect(node.textContent).toMatch(/starting browser/i);
  });

  it("does not crash when an older adapter lacks browser view methods", () => {
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={{} as typeof liveAdapter}
      />,
    );

    expect(node.textContent).toMatch(/browser preview is unavailable/i);
    expect(node.querySelector('[data-testid="browser-iframe"]')).toBeNull();
    expect(
      node.querySelector('button[aria-label="Open browser preview"]'),
    ).toBeNull();
    expect(node.querySelector('button[aria-label="Maximize browser"]')).toBeNull();
    expect(node.textContent).not.toContain("Take control");
  });
});
