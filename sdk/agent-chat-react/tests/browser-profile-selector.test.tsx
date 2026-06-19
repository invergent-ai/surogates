import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AgentChatAdapterProvider,
  NO_BROWSER_ADAPTER,
} from "../src/adapter-context";
import { ChatComposer } from "../src/components/chat/chat-composer";
import type { AgentChatAdapter } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

async function renderComposer(
  onSelectBrowserProfile: (id: string | null) => void,
) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  const adapter = {
    ...NO_BROWSER_ADAPTER,
    async listBrowserProfiles() {
      return [
        {
          id: "p1",
          name: "Personal",
          cookieDomains: [],
          hasState: true,
          createdAt: "",
          lastUsedAt: null,
        },
        {
          id: "p2",
          name: "Work",
          cookieDomains: [],
          hasState: false,
          createdAt: "",
          lastUsedAt: null,
        },
      ];
    },
  } as unknown as AgentChatAdapter;
  await act(async () => {
    root?.render(
      <AgentChatAdapterProvider value={{ adapter, sessionId: "s-1" }}>
        <ChatComposer
          onSend={vi.fn()}
          onStop={vi.fn()}
          isRunning={false}
          canShowBrowser
          onSelectBrowserProfile={onSelectBrowserProfile}
        />
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

describe("browser profile selector", () => {
  it("lists profiles and selects one", async () => {
    const onSelect = vi.fn();
    const node = await renderComposer(onSelect);
    const trigger = node.querySelector(
      '[aria-label="Select browser profile"]',
    ) as HTMLElement;
    await act(async () => trigger.click());
    await act(async () => {
      await Promise.resolve();
    });
    // PopoverContent portals to document.body, so query the document.
    expect(document.body.textContent).toContain("Personal");
    expect(document.body.textContent).toContain("Work");
    const work = [...document.querySelectorAll("[cmdk-item]")].find((el) =>
      el.textContent?.includes("Work"),
    ) as HTMLElement;
    await act(async () => work.click());
    expect(onSelect).toHaveBeenCalledWith("p2");
  });
});
