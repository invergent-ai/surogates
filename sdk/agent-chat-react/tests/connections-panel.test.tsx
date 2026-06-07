import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionsPanel } from "../src/components/connections/connections-panel";

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

function adapter(over: Record<string, unknown> = {}) {
  return {
    listComposioConnections: vi.fn().mockResolvedValue({
      toolkits: [
        { toolkit: "github", connected: true },
        { toolkit: "gmail", connected: false },
      ],
    }),
    authorizeComposioToolkit: vi.fn().mockResolvedValue({
      redirectUrl: "https://p/oauth",
      status: "INITIATED",
    }),
    ...over,
  };
}

function renderPanel(adapterImpl: unknown) {
  act(() => {
    root?.render(
      <ConnectionsPanel agentId="a1" adapter={adapterImpl as never} />,
    );
  });
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("ConnectionsPanel", () => {
  it("renders toolkits with status", async () => {
    renderPanel(adapter());
    await flush();
    const text = container?.textContent ?? "";
    expect(text).toContain("github");
    expect(text).toContain("gmail");
    expect(text).toContain("connected");
  });

  it("connect calls authorize and opens the redirect", async () => {
    const a = adapter();
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    renderPanel(a);
    await flush();

    // github is connected (no button); the only Connect button is gmail's.
    const btn = container?.querySelector("button");
    expect(btn?.textContent).toContain("Connect");
    await act(async () => {
      btn?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    expect(a.authorizeComposioToolkit).toHaveBeenCalledWith({
      agentId: "a1",
      toolkit: "gmail",
    });
    expect(openSpy).toHaveBeenCalledWith(
      "https://p/oauth",
      expect.anything(),
      expect.anything(),
    );
    openSpy.mockRestore();
  });

  it("renders nothing when adapter lacks the methods", async () => {
    renderPanel({});
    await flush();
    expect(container?.childNodes.length ?? 0).toBe(0);
  });
});
