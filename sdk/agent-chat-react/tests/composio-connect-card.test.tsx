import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ComposioConnectCard } from "../src/components/connections/composio-connect-card";
import { isComposioConnectUrl } from "../src/lib/oauth-popup";

let container: HTMLDivElement | null = null;
let root: Root | null = null;
beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  container = null;
  root = null;
});

describe("isComposioConnectUrl", () => {
  it("matches the hosted connect domain only", () => {
    expect(isComposioConnectUrl("https://connect.composio.dev/link/lk_1")).toBe(true);
    expect(isComposioConnectUrl("https://example.com/link/lk_1")).toBe(false);
    expect(isComposioConnectUrl(undefined)).toBe(false);
    expect(isComposioConnectUrl("not a url")).toBe(false);
  });
});

describe("ComposioConnectCard", () => {
  it("shows the toolkit name and opens the popup on Connect", () => {
    const openSpy = vi.spyOn(window, "open").mockReturnValue({} as Window);
    act(() => {
      root?.render(
        <ComposioConnectCard
          url="https://connect.composio.dev/link/lk_-KRBPIvYRx14"
          label="Connect Jira"
        />,
      );
    });
    // "Connect Jira" → name "Jira"
    expect(container?.textContent).toContain("Connect Jira");
    const btn = container?.querySelector("button");
    expect(btn?.textContent).toBe("Connect");
    btn?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(openSpy).toHaveBeenCalledWith(
      "https://connect.composio.dev/link/lk_-KRBPIvYRx14",
      "composio-oauth",
      expect.stringContaining("popup=yes"),
    );
    openSpy.mockRestore();
  });
});
