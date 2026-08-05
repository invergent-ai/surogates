/**
 * Session context in the composer tools row.
 *
 * The trigger names its own state rather than relying on a tooltip (there is
 * no hover on a phone), opening it shows the usage breakdown, and Compress
 * both sends the command and closes the panel — a plain button inside the
 * panel does not dismiss it the way selecting a menu item does.
 */
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AgentChatAdapterProvider,
  NO_BROWSER_ADAPTER,
} from "../src/adapter-context";
import { ChatComposer } from "../src/components/chat/chat-composer";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type { AgentChatAdapter, TokenUsage } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const tokenUsage: TokenUsage = {
  inputTokens: 3000,
  outputTokens: 1000,
  reasoningTokens: 0,
  cachedInputTokens: 0,
  totalTokens: 4000,
  contextWindow: 16000,
  model: "gpt-4o-mini",
};

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function mount(node: ReactElement): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <AgentChatAdapterProvider
        value={{ adapter: NO_BROWSER_ADAPTER as AgentChatAdapter, sessionId: "s-1" }}
      >
        <TooltipProvider>{node}</TooltipProvider>
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

function contextTrigger(dom: HTMLElement): HTMLElement {
  const el = dom.querySelector('[aria-label^="Session context"]');
  if (!el) throw new Error("context trigger not rendered");
  return el as HTMLElement;
}

describe("composer session context", () => {
  it("states the share of the window used in its accessible name", () => {
    const dom = mount(
      <ChatComposer
        onSend={vi.fn()}
        onStop={vi.fn()}
        isRunning={false}
        tokenUsage={tokenUsage}
      />,
    );
    // 4000 of 16000.
    expect(contextTrigger(dom).getAttribute("aria-label")).toContain("25%");
  });

  it("opens a panel with the usage breakdown", async () => {
    const dom = mount(
      <ChatComposer
        onSend={vi.fn()}
        onStop={vi.fn()}
        isRunning={false}
        tokenUsage={tokenUsage}
      />,
    );
    await act(async () => {
      contextTrigger(dom).click();
      await Promise.resolve();
    });
    // The panel portals out of the composer, so query the document.
    expect(document.body.textContent).toContain("Input");
    expect(document.body.textContent).toContain("Output");
  });

  it("sends /compress and closes the panel", async () => {
    const onSend = vi.fn();
    const dom = mount(
      <ChatComposer
        onSend={onSend}
        onStop={vi.fn()}
        isRunning={false}
        tokenUsage={tokenUsage}
      />,
    );
    await act(async () => {
      contextTrigger(dom).click();
      await Promise.resolve();
    });
    const compress = [...document.querySelectorAll("button")].find(
      (b) => b.textContent?.trim() === "Compress",
    );
    expect(compress).toBeDefined();
    await act(async () => {
      compress?.click();
      await Promise.resolve();
    });
    expect(onSend).toHaveBeenCalledWith("/compress");
    expect(document.body.textContent).not.toContain("Input");
  });

  it("renders nothing without a known context window", () => {
    const dom = mount(
      <ChatComposer onSend={vi.fn()} onStop={vi.fn()} isRunning={false} />,
    );
    expect(dom.querySelector('[aria-label^="Session context"]')).toBeNull();
  });
});
