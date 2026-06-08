import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IntegrationsBand } from "../src/components/connections/integrations-band";

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

function adapter(toolkits: unknown[]) {
  return {
    listComposioConnections: vi.fn().mockResolvedValue({ toolkits }),
    authorizeComposioToolkit: vi.fn(),
  };
}
async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}
function render(a: unknown, onOpen = vi.fn()) {
  act(() => {
    root?.render(
      <IntegrationsBand agentId="a1" adapter={a as never} onOpenIntegrations={onOpen} />,
    );
  });
  return onOpen;
}

describe("IntegrationsBand", () => {
  it("renders all toolkit logos and the prompt, and fires onOpenIntegrations", async () => {
    const onOpen = render(
      adapter([
        { toolkit: "github", connected: true, name: "GitHub", logo: "https://l/gh" },
        { toolkit: "gmail", connected: false, name: "Gmail", logo: "https://l/gm" },
      ]),
    );
    await flush();
    expect(container?.textContent).toContain("Connect your accounts");
    const imgs = Array.from(container?.querySelectorAll("img") ?? []);
    expect(imgs.length).toBe(2); // both shown — connected + unconnected
    container
      ?.querySelector("button")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(onOpen).toHaveBeenCalled();
  });

  it("caps logos at 10", async () => {
    const many = Array.from({ length: 14 }, (_, i) => ({
      toolkit: `t${i}`,
      connected: true,
      name: `T${i}`,
      logo: `https://l/${i}`,
    }));
    render(adapter(many));
    await flush();
    expect((container?.querySelectorAll("img") ?? []).length).toBe(10);
  });

  it("renders nothing when the agent has no toolkits", async () => {
    render(adapter([]));
    await flush();
    expect(container?.childNodes.length ?? 0).toBe(0);
  });

  it("renders nothing when the adapter lacks the methods", async () => {
    render({});
    await flush();
    expect(container?.childNodes.length ?? 0).toBe(0);
  });
});
