import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider } from "../src/adapter-context";
import { TurnFeedback } from "../src/components/chat/turn-feedback";
import type { AgentChatAdapter, AgentChatMessage } from "../src/types";

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

function render(
  msg: AgentChatMessage,
  overrides: Partial<AgentChatAdapter> = {},
  sessionId: string | null = "sess-1",
): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  const adapter = {
    submitUserFeedback: vi.fn().mockResolvedValue({}),
    ...overrides,
  } as unknown as AgentChatAdapter;
  act(() => {
    root?.render(
      <AgentChatAdapterProvider value={{ adapter, sessionId }}>
        <TurnFeedback msg={msg} />
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

function assistant(overrides: Partial<AgentChatMessage> = {}): AgentChatMessage {
  return {
    id: "evt-50",
    role: "assistant",
    content: "answer",
    createdAt: new Date("2026-01-01T00:00:00Z"),
    status: "complete",
    llmResponseEventId: 50,
    ...overrides,
  };
}

describe("TurnFeedback", () => {
  it("renders nothing when llmResponseEventId is missing", () => {
    const node = render(assistant({ llmResponseEventId: undefined }));
    expect(node.querySelector('button[aria-label="Good response"]')).toBeNull();
  });

  it("renders nothing when status is streaming", () => {
    const node = render(assistant({ status: "streaming" }));
    expect(node.querySelector('button[aria-label="Good response"]')).toBeNull();
  });

  it("renders nothing when the adapter has no submitUserFeedback method", () => {
    const node = render(assistant(), { submitUserFeedback: undefined });
    expect(node.querySelector('button[aria-label="Good response"]')).toBeNull();
  });

  it("posts rating='up' immediately on thumbs-up click", async () => {
    const submit = vi.fn().mockResolvedValue({});
    const node = render(assistant(), { submitUserFeedback: submit });
    const up = node.querySelector(
      'button[aria-label="Good response"]',
    ) as HTMLButtonElement;
    expect(up).not.toBeNull();
    await act(async () => {
      up.click();
    });
    expect(submit).toHaveBeenCalledWith({
      sessionId: "sess-1",
      llmResponseEventId: 50,
      rating: "up",
    });
  });

  it("opens the reason form on thumbs-down and submits the trimmed reason", async () => {
    const submit = vi.fn().mockResolvedValue({});
    const node = render(assistant(), { submitUserFeedback: submit });
    const down = node.querySelector(
      'button[aria-label="Poor response"]',
    ) as HTMLButtonElement;
    await act(async () => {
      down.click();
    });
    const textarea = node.querySelector("textarea") as HTMLTextAreaElement;
    expect(textarea).not.toBeNull();
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype,
        "value",
      )?.set;
      setter?.call(textarea, "  wrong column  ");
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const send = Array.from(node.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Send feedback"),
    ) as HTMLButtonElement;
    await act(async () => {
      send.click();
    });
    expect(submit).toHaveBeenCalledWith({
      sessionId: "sess-1",
      llmResponseEventId: 50,
      rating: "down",
      reason: "wrong column",
    });
  });

  it("renders the persisted reason and disabled buttons when already rated", () => {
    const node = render(
      assistant({ userFeedback: { rating: "down", reason: "off by one" } }),
    );
    const up = node.querySelector(
      'button[aria-label="Good response"]',
    ) as HTMLButtonElement;
    const down = node.querySelector(
      'button[aria-label="Poor response"]',
    ) as HTMLButtonElement;
    expect(up.disabled).toBe(true);
    expect(down.disabled).toBe(true);
    expect(node.textContent).toContain("off by one");
  });
});
