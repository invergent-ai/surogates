import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AgentChatAdapterProvider,
  NO_BROWSER_ADAPTER,
} from "../src/adapter-context";
import { ChatComposer } from "../src/components/chat/chat-composer";
import { TooltipProvider } from "../src/components/ui/tooltip";
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
  {
    locked = false,
    profileId = null,
  }: { locked?: boolean; profileId?: string | null } = {},
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
      <TooltipProvider>
        <AgentChatAdapterProvider value={{ adapter, sessionId: "s-1" }}>
          <ChatComposer
            onSend={vi.fn()}
            onStop={vi.fn()}
            isRunning={false}
            browserProfilesEnabled
            browserProfileLocked={locked}
            browserProfileId={profileId}
            onSelectBrowserProfile={onSelectBrowserProfile}
          />
        </AgentChatAdapterProvider>
      </TooltipProvider>,
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

  it("is shown without a live browser (so a profile can be picked first)", async () => {
    // The selector gates on browserProfilesEnabled — NOT canShowBrowser — so it
    // is available before a session/browser exists, the only point at which a
    // profile binds to the session.
    const node = await renderComposer(vi.fn());
    expect(
      node.querySelector('[aria-label="Select browser profile"]'),
    ).not.toBeNull();
  });

  it("locks the selector for an active session", async () => {
    const onSelect = vi.fn();
    const node = await renderComposer(onSelect, { locked: true });
    const trigger = node.querySelector(
      '[aria-label="Select browser profile"]',
    ) as HTMLElement;
    expect(trigger).not.toBeNull();
    expect(trigger.getAttribute("aria-disabled")).toBe("true");
    // Clicking the locked trigger opens no popover and selects nothing.
    await act(async () => trigger.click());
    await act(async () => {
      await Promise.resolve();
    });
    expect(onSelect).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain("Personal");
  });

  it("marks the selected profile with a check and names it on the trigger", async () => {
    const dom = await renderComposer(vi.fn(), { profileId: "p2" });

    // The trigger itself shows which profile is active — no need to
    // open the menu to find out.
    const trigger = dom.querySelector('[aria-label="Select browser profile"]')!;
    expect(trigger.textContent).toContain("Work");

    await act(async () => {
      (trigger as HTMLElement).click();
    });
    const checked = [...document.querySelectorAll("[data-checked]")];
    expect(checked).toHaveLength(1);
    expect(checked[0]?.textContent).toContain("Work");
    expect(checked[0]?.querySelector("svg")).not.toBeNull();
  });

  it("marks 'No profile' as selected when no profile is chosen", async () => {
    const dom = await renderComposer(vi.fn());
    const trigger = dom.querySelector('[aria-label="Select browser profile"]')!;
    await act(async () => {
      (trigger as HTMLElement).click();
    });
    const checked = [...document.querySelectorAll("[data-checked]")];
    expect(checked).toHaveLength(1);
    expect(checked[0]?.textContent).toContain("No profile");
  });

  it("names the bound profile on the locked trigger", async () => {
    const dom = await renderComposer(vi.fn(), { locked: true, profileId: "p1" });
    const trigger = dom.querySelector('[aria-label="Select browser profile"]')!;
    expect(trigger.textContent).toContain("Personal");
  });
});
