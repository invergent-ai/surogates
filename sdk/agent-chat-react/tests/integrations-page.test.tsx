import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IntegrationsPage } from "../src/components/connections/integrations-page";

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
async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}
function adapter(over: Record<string, unknown> = {}) {
  return {
    listComposioConnections: vi.fn().mockResolvedValue({
      toolkits: [
        {
          toolkit: "github",
          connected: true,
          name: "GitHub",
          logo: "https://l/gh",
          category: "Developer Tools",
          description: "Code.",
        },
        {
          toolkit: "gmail",
          connected: false,
          name: "Gmail",
          logo: "https://l/gm",
          category: "Email",
          description: "Mail.",
        },
      ],
    }),
    authorizeComposioToolkit: vi.fn().mockResolvedValue({
      redirectUrl: "https://p/oauth",
      status: "INITIATED",
    }),
    disconnectComposioToolkit: vi.fn().mockResolvedValue(undefined),
    ...over,
  };
}
function render(a: unknown, onBack = vi.fn()) {
  act(() => {
    root?.render(
      <IntegrationsPage agentId="a1" adapter={a as never} onBack={onBack} />,
    );
  });
  return onBack;
}

describe("IntegrationsPage", () => {
  it("groups by category and shows connect/disconnect", async () => {
    render(adapter());
    await flush();
    const text = container?.textContent ?? "";
    expect(text).toContain("Developer Tools");
    expect(text).toContain("Email");
    expect(text).toContain("GitHub");
    expect(text).toContain("Disconnect"); // github connected
    expect(text).toContain("Connect"); // gmail not connected
  });

  it("connect opens the redirect", async () => {
    const a = adapter();
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    render(a);
    await flush();
    const btn = Array.from(container?.querySelectorAll("button") ?? []).find(
      (b) => b.textContent?.trim() === "Connect",
    );
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

  it("disconnect calls the adapter and refreshes", async () => {
    const a = adapter();
    render(a);
    await flush();
    const btn = Array.from(container?.querySelectorAll("button") ?? []).find(
      (b) => b.textContent?.trim() === "Disconnect",
    );
    await act(async () => {
      btn?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();
    expect(a.disconnectComposioToolkit).toHaveBeenCalledWith({
      agentId: "a1",
      toolkit: "github",
    });
    // refreshed: listComposioConnections called twice (mount + after disconnect)
    expect(a.listComposioConnections).toHaveBeenCalledTimes(2);
  });

  it("search filters rows by name", async () => {
    render(adapter());
    await flush();
    const input = container?.querySelector("input");
    await act(async () => {
      if (input) {
        // React tracks the value via a property descriptor; set through the
        // native setter so the dispatched input event triggers onChange.
        const setter = Object.getOwnPropertyDescriptor(
          HTMLInputElement.prototype,
          "value",
        )?.set;
        setter?.call(input, "git");
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
    });
    await flush();
    const text = container?.textContent ?? "";
    expect(text).toContain("GitHub");
    expect(text).not.toContain("Gmail");
  });

  it("back button fires onBack", async () => {
    const onBack = render(adapter());
    await flush();
    const back = Array.from(container?.querySelectorAll("button") ?? []).find(
      (b) => b.textContent?.includes("Back"),
    );
    back?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(onBack).toHaveBeenCalled();
  });
});
