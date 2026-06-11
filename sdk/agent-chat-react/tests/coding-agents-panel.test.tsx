// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// CodingAgentsPanel: renders one card per provider from the adapter's
// listCodingAgentConnections, submits trimmed values, refreshes status,
// disconnects, and blocks an obviously-wrong OAuth token client-side.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CodingAgentsPanel } from "../src/components/connections/coding-agents-panel";
import type { AgentChatAdapter, CodingAgentConnection } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

function fakeAdapter(
  over: Partial<AgentChatAdapter> = {},
  connections: CodingAgentConnection[] = [],
): AgentChatAdapter {
  return {
    listCodingAgentConnections: vi.fn().mockResolvedValue({ connections }),
    submitCodingAgentCredential: vi
      .fn()
      .mockResolvedValue({ provider: "anthropic", connected: true, auth_mode: "oauth" }),
    disconnectCodingAgentProvider: vi.fn().mockResolvedValue(undefined),
    ...over,
  } as unknown as AgentChatAdapter;
}

let container: HTMLDivElement;
let root: Root;

async function render(node: React.ReactElement) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(node);
  });
  // Flush the initial refresh effect.
  await act(async () => {
    await Promise.resolve();
  });
}

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

function findTextarea(name: string): HTMLTextAreaElement {
  const el = Array.from(container.querySelectorAll("textarea")).find(
    (t) => t.getAttribute("aria-label") === name,
  );
  if (!el) throw new Error(`textarea ${name} not found`);
  return el as HTMLTextAreaElement;
}

function setTextareaValue(el: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  )?.set;
  setter?.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

function clickButton(label: string) {
  const btn = Array.from(container.querySelectorAll("button")).find((b) =>
    (b.textContent ?? "").trim().includes(label),
  );
  if (!btn) throw new Error(`button "${label}" not found`);
  btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
}

describe("CodingAgentsPanel", () => {
  it("renders a card per provider", async () => {
    await render(<CodingAgentsPanel adapter={fakeAdapter()} onBack={() => {}} />);
    expect(container.textContent).toContain("Claude Code");
    expect(container.textContent).toContain("Codex");
  });

  it("renders nothing when the adapter lacks the methods", async () => {
    await render(
      <CodingAgentsPanel
        adapter={{} as AgentChatAdapter}
        onBack={() => {}}
      />,
    );
    expect(container.textContent).toBe("");
  });

  it("submits a trimmed value and refreshes status", async () => {
    const adapter = fakeAdapter();
    await render(<CodingAgentsPanel adapter={adapter} onBack={() => {}} />);

    const ta = findTextarea("Claude Code credential");
    await act(async () => {
      setTextareaValue(ta, "  sk-ant-oat-abc  ");
    });
    await act(async () => {
      clickButton("Connect");
      await Promise.resolve();
    });

    expect(adapter.submitCodingAgentCredential).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: "anthropic",
        mode: "oauth",
        value: "sk-ant-oat-abc",
      }),
    );
    // Initial load + post-submit refresh.
    expect(
      (adapter.listCodingAgentConnections as ReturnType<typeof vi.fn>).mock.calls
        .length,
    ).toBeGreaterThanOrEqual(2);
  });

  it("blocks an obviously-wrong claude OAuth token client-side", async () => {
    const adapter = fakeAdapter();
    await render(<CodingAgentsPanel adapter={adapter} onBack={() => {}} />);

    const ta = findTextarea("Claude Code credential");
    await act(async () => {
      setTextareaValue(ta, "not-a-token");
    });
    await act(async () => {
      clickButton("Connect");
      await Promise.resolve();
    });

    expect(adapter.submitCodingAgentCredential).not.toHaveBeenCalled();
    expect(container.textContent).toContain("sk-ant-oat");
  });

  it("disconnects a connected provider", async () => {
    const adapter = fakeAdapter({}, [
      { provider: "anthropic", connected: true, auth_mode: "oauth", expires_at: null },
    ]);
    await render(<CodingAgentsPanel adapter={adapter} onBack={() => {}} />);

    await act(async () => {
      clickButton("Disconnect");
      await Promise.resolve();
    });

    expect(adapter.disconnectCodingAgentProvider).toHaveBeenCalledWith(
      expect.objectContaining({ provider: "anthropic" }),
    );
  });
});
