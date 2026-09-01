import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BrowserPane } from "../src/components/browser/browser-pane";
import { NO_BROWSER_ADAPTER } from "../src/adapter-context";

vi.mock("@novnc/novnc", () => ({
  default: vi.fn().mockImplementation(() => ({
    disconnect: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    viewOnly: false,
    scaleViewport: false,
  })),
}));

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
  browserShellUrl() {
    return "ws://browser.test/shell";
  },
};

// The shell opens a socket on mount; happy-dom would try to dial it.
class StubSocket {
  static OPEN = 1;
  readyState = 1;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { wasClean: boolean }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  constructor(public url: string) {}
  send(payload: string) {
    this.sent.push(payload);
  }
  close() {}
}
vi.stubGlobal("WebSocket", StubSocket);
URL.createObjectURL = vi.fn(() => "blob:frame") as typeof URL.createObjectURL;
URL.revokeObjectURL = vi.fn() as typeof URL.revokeObjectURL;

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

/** The take/return-control toggle: one icon button, state in aria-pressed. */
function controlToggle(node: ParentNode): HTMLButtonElement | null {
  return node.querySelector<HTMLButtonElement>(
    '[data-testid="browser-shell-control"]',
  );
}

/** Close and Maximize live behind the shell's overflow menu now. */
async function openOverflow(node: ParentNode): Promise<void> {
  const more = node.querySelector<HTMLButtonElement>('button[aria-label="More"]');
  await act(async () => {
    more?.click();
  });
}

function menuItem(node: ParentNode, label: string): HTMLButtonElement | undefined {
  return Array.from(node.querySelectorAll<HTMLButtonElement>("button")).find(
    (button) => button.textContent?.trim() === label,
  );
}

function renderPane(element: React.ReactElement) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(element);
  });
  return container;
}

describe("BrowserPane", () => {
  it("streams the shell even when this viewer holds no control", async () => {
    // Replaces the passive-preview test: the shell is live for anyone who can
    // see the session, so there is no still-image fallback to fall back to.
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={liveAdapter}
      />,
    );

    expect(node.querySelector('[data-testid="browser-shell"]')).not.toBeNull();
    expect(controlToggle(node)?.getAttribute("aria-pressed")).toBe("false");
    expect(
      node.querySelector('[data-testid="browser-preview-image"]'),
    ).toBeNull();
  });

  it("maximizes into a full-page dialog carrying the shell", async () => {
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={liveAdapter}
      />,
    );

    await openOverflow(node);
    const maximize = menuItem(node, "Maximize");
    expect(maximize).not.toBeUndefined();
    await act(async () => {
      maximize?.click();
    });

    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(
      document.body.querySelector('[data-testid="browser-fullscreen-shell"]'),
    ).not.toBeNull();
  });

  it("does not mount live view from replayed user-control state after refresh", async () => {
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "user-control", controlOwner: "user-A" }}
        adapter={liveAdapter}
      />,
    );

    // Server-reported control belongs to someone else; this tab must not
    // think it holds the lease just because the session says someone does.
    expect(controlToggle(node)?.getAttribute("aria-pressed")).toBe("false");
    expect(controlToggle(node)?.getAttribute("aria-label")).toBe("Take control");
    expect(node.querySelector('[data-testid="browser-shell"]')).not.toBeNull();
  });

  it("offers the take-control toggle in live state", async () => {
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={liveAdapter}
      />,
    );
    expect(controlToggle(node)).not.toBeNull();
    expect(controlToggle(node)?.getAttribute("aria-label")).toBe("Take control");
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

    const takeControlButton = controlToggle(node);
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
      document.body.querySelector('[data-testid="browser-fullscreen-shell"]'),
    ).not.toBeNull();
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

    const takeControlButton = controlToggle(node);

    await act(async () => {
      takeControlButton?.click();
    });

    expect(document.body.querySelector<HTMLElement>('[role="dialog"]')).not.toBeNull();

    const closeButton = document.body.querySelector<HTMLButtonElement>(
      '[role="dialog"] [data-slot="dialog-close"]',
    );
    expect(closeButton).not.toBeNull();

    await act(async () => {
      closeButton?.click();
    });

    // Inline shell must still be mounted.
    expect(node.querySelector('[data-testid="browser-shell"]')).not.toBeNull();
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

      const takeControlButton = controlToggle(node);

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

    await openOverflow(node);
    const closeButton = menuItem(node, "Close browser");
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
    const takeControlButton = controlToggle(node);
    await act(async () => {
      takeControlButton?.click();
    });

    // Click Close → open confirm.
    await openOverflow(node);
    const closeButton = menuItem(node, "Close browser");
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

    await openOverflow(node);
    const closeButton = menuItem(node, "Close browser");
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

    // Replayed control belongs to user-A, so this tab is still offered
    // "Take control" -- never "Return control", which would imply it holds a
    // lease it does not have.
    expect(controlToggle(node)?.getAttribute("aria-label")).toBe("Take control");
    expect(controlToggle(node)?.getAttribute("aria-label")).not.toBe(
      "Return control",
    );
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

    expect(node.textContent).toMatch(/browser view is unavailable/i);
    expect(node.querySelector('[data-testid="browser-shell"]')).toBeNull();
    expect(node.textContent).not.toContain("Take control");
  });
});
