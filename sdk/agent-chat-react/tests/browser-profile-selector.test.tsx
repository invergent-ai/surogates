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
  // The profile picker used to be its own button in the composer tools row.
  // It is a group inside the one tools panel now (see ResponsivePanel), so
  // every case here opens that panel first. What is asserted is unchanged:
  // which profiles are offered, which is checked, and that a locked session
  // cannot change the binding.
  const openTools = async (node: HTMLElement) => {
    const trigger = node.querySelector(
      '[aria-label="Composer tools"]',
    ) as HTMLElement;
    expect(trigger).not.toBeNull();
    await act(async () => trigger.click());
    await act(async () => {
      await Promise.resolve();
    });
  };

  it("lists profiles and selects one", async () => {
    const onSelect = vi.fn();
    const node = await renderComposer(onSelect);
    await openTools(node);
    // The panel portals to document.body, so query the document.
    expect(document.body.textContent).toContain("Personal");
    expect(document.body.textContent).toContain("Work");
    const work = [...document.querySelectorAll("[cmdk-item]")].find((el) =>
      el.textContent?.includes("Work"),
    ) as HTMLElement;
    await act(async () => work.click());
    expect(onSelect).toHaveBeenCalledWith("p2");
  });

  it("is offered without a live browser (so a profile can be picked first)", async () => {
    // The group gates on browserProfilesEnabled — NOT canShowBrowser — so it
    // is available before a session/browser exists, the only point at which a
    // profile binds to the session.
    const node = await renderComposer(vi.fn());
    await openTools(node);
    expect(document.body.textContent).toContain("Browser profile");
    expect(document.body.textContent).toContain("Personal");
  });

  it("locks the selection for an active session", async () => {
    const onSelect = vi.fn();
    const node = await renderComposer(onSelect, {
      locked: true,
      profileId: "p1",
    });
    await openTools(node);
    // The bound profile is named and the reason is stated outright — in a
    // menu there is nothing to hover, so it cannot live in a tooltip.
    expect(document.body.textContent).toContain("Personal");
    expect(document.body.textContent).toContain("Locked for this session.");
    // The other profiles are not offered at all, so none can be picked.
    expect(document.body.textContent).not.toContain("Work");
    const items = [...document.querySelectorAll("[cmdk-item]")].filter((el) =>
      el.textContent?.includes("Personal"),
    );
    for (const item of items) {
      await act(async () => (item as HTMLElement).click());
    }
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("explains why nothing can be chosen when a locked session has no profile", async () => {
    const node = await renderComposer(vi.fn(), { locked: true });
    await openTools(node);
    expect(document.body.textContent).toContain(
      "A profile can only be chosen before the session starts.",
    );
  });

  it("marks the selected profile with a check", async () => {
    const node = await renderComposer(vi.fn(), { profileId: "p2" });
    await openTools(node);
    const checked = [...document.querySelectorAll("[data-checked]")];
    expect(checked).toHaveLength(1);
    expect(checked[0]?.textContent).toContain("Work");
    expect(checked[0]?.querySelector("svg")).not.toBeNull();
  });

  it("marks 'No profile' as selected when no profile is chosen", async () => {
    const node = await renderComposer(vi.fn());
    await openTools(node);
    const checked = [...document.querySelectorAll("[data-checked]")];
    expect(checked).toHaveLength(1);
    expect(checked[0]?.textContent).toContain("No profile");
  });
});
