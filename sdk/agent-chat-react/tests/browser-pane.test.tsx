import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
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

describe("BrowserPane", () => {
  it("renders an iframe in live state", () => {
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={liveAdapter}
      />,
    );

    const iframe = node.querySelector<HTMLIFrameElement>(
      '[data-testid="browser-iframe"]',
    );
    expect(iframe?.getAttribute("src")).toBe("about:blank#browser-live");
  });

  it("opens the browser live view in a full-page dialog", async () => {
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={liveAdapter}
      />,
    );

    const maximizeButton = node.querySelector<HTMLButtonElement>(
      'button[aria-label="Maximize browser"]',
    );
    expect(maximizeButton).not.toBeNull();

    await act(async () => {
      maximizeButton?.click();
    });

    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]');
    const iframe = document.body.querySelector<HTMLIFrameElement>(
      '[data-testid="browser-fullscreen-iframe"]',
    );

    expect(dialog).not.toBeNull();
    expect(dialog?.textContent).toContain("Browser");
    expect(iframe?.getAttribute("src")).toBe("about:blank#browser-live");
  });

  it("shows Take control button in live state", () => {
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "live", controlOwner: null }}
        adapter={liveAdapter}
      />,
    );

    expect(node.textContent).toContain("Take control");
  });

  it("shows Return control button when user has control", () => {
    const node = renderPane(
      <BrowserPane
        sessionId="s"
        state={{ status: "user-control", controlOwner: "user-A" }}
        adapter={liveAdapter}
      />,
    );

    expect(node.textContent).toContain("Return control");
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

    expect(node.textContent).toMatch(/browser live view is unavailable/i);
    expect(node.querySelector('[data-testid="browser-iframe"]')).toBeNull();
    expect(node.querySelector('button[aria-label="Maximize browser"]')).toBeNull();
    expect(node.textContent).not.toContain("Take control");
  });
});
