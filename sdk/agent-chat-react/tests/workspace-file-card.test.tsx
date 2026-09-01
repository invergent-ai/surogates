/**
 * WorkspaceFileCard — Claude-style downloadable file card.
 *
 * Verifies the icon + label + type label render, the Download button
 * carries the download URL and does NOT trigger the preview dialog,
 * and a card-body click opens the preview dialog (which fetches the
 * file via the adapter).
 */
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { WorkspaceFileCard } from "../src/components/chat/workspace-file-card";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type { AgentChatAdapter } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;


interface AdapterOverrides {
  getWorkspaceDownloadUrl?: AgentChatAdapter["getWorkspaceDownloadUrl"];
  getWorkspaceFile?: AgentChatAdapter["getWorkspaceFile"];
}

function adapterStub(over: AdapterOverrides = {}): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    listSessions: vi.fn().mockResolvedValue({ sessions: [], total: 0 }),
    createSession: vi.fn(),
    getSession: vi.fn(),
    sendMessage: vi.fn(),
    openEventStream: vi.fn(() => ({
      addEventListener: vi.fn(),
      close: vi.fn(),
      onerror: null,
    })),
    getWorkspaceDownloadUrl:
      over.getWorkspaceDownloadUrl
      ?? (({ sessionId, path }) =>
        `/api/v1/sessions/${sessionId}/workspace/download?path=${encodeURIComponent(path)}`),
    getWorkspaceFile:
      over.getWorkspaceFile
      ?? vi.fn().mockResolvedValue({
        path: "doc.docx",
        content: "binary",
        size: 1024,
        encoding: "utf-8",
        truncated: false,
      }),
  } as unknown as AgentChatAdapter;
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function mount(
  node: ReactElement,
  adapter: AgentChatAdapter,
  onFileSelect?: (path: string) => void,
): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <AgentChatAdapterProvider
        value={{ adapter, sessionId: "s-1", onFileSelect }}
      >
        <TooltipProvider>{node}</TooltipProvider>
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}


describe("WorkspaceFileCard", () => {
  it("renders the file label and a human type label derived from the extension", () => {
    const adapter = adapterStub();
    const dom = mount(
      <WorkspaceFileCard
        sessionId="s-1"
        path="reports/Financial_Analysis.docx"
        label="Financial Analysis"
      />,
      adapter,
    );
    expect(dom.textContent).toContain("Financial Analysis");
    expect(dom.textContent).toContain("Word document");
  });

  it("renders an anchor whose href + download attributes come from the adapter", () => {
    const getWorkspaceDownloadUrl = vi.fn(
      ({ sessionId, path }) => `https://srv/${sessionId}/dl?p=${path}`,
    );
    const adapter = adapterStub({ getWorkspaceDownloadUrl });
    const dom = mount(
      <WorkspaceFileCard
        sessionId="s-1"
        path="reports/Report.pdf"
        label="Report"
      />,
      adapter,
    );
    const anchor = dom.querySelector("a");
    expect(anchor).not.toBeNull();
    expect(anchor!.getAttribute("href")).toBe("https://srv/s-1/dl?p=reports/Report.pdf");
    expect(anchor!.getAttribute("download")).toBe("Report.pdf");
    expect(getWorkspaceDownloadUrl).toHaveBeenCalledWith({
      sessionId: "s-1",
      path: "reports/Report.pdf",
    });
  });

  it("clicking the Download anchor does NOT open the pane viewer", () => {
    const adapter = adapterStub();
    const onFileSelect = vi.fn();
    const dom = mount(
      <WorkspaceFileCard
        sessionId="s-1"
        path="x.docx"
        label="x"
      />,
      adapter,
      onFileSelect,
    );
    const anchor = dom.querySelector("a")!;
    // Synthesize a click that bubbles — the card body's onClick would
    // fire too if the anchor didn't stopPropagation.
    act(() => {
      anchor.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(onFileSelect).not.toHaveBeenCalled();
  });

  it("clicking the card body opens the pane viewer, never a dialog", async () => {
    // One viewer in the product: the right-column drawer the workspace
    // uses. The card's old private Dialog viewer duplicated it.
    const getWorkspaceFile = vi.fn();
    const adapter = adapterStub({ getWorkspaceFile });
    const onFileSelect = vi.fn();
    const dom = mount(
      <WorkspaceFileCard
        sessionId="s-1"
        path="x.txt"
        label="x"
      />,
      adapter,
      onFileSelect,
    );
    const card = dom.querySelector("[role='button']")!;
    act(() => {
      (card as HTMLElement).click();
    });
    expect(onFileSelect).toHaveBeenCalledWith("x.txt");
    // No dialog portal, and no fetch of its own — the drawer owns loading.
    expect(document.querySelector("[role='dialog']")).toBeNull();
    expect(getWorkspaceFile).not.toHaveBeenCalled();
  });

  it("falls back to the generic 'File' type label for unknown extensions", () => {
    const adapter = adapterStub();
    const dom = mount(
      <WorkspaceFileCard
        sessionId="s-1"
        path="weird.xyz"
        label="weird"
      />,
      adapter,
    );
    expect(dom.textContent).toContain("File");
  });

  it("uses the basename when the label is missing", () => {
    const adapter = adapterStub();
    const dom = mount(
      <WorkspaceFileCard
        sessionId="s-1"
        path="dir/sub/page.html"
        label=""
      />,
      adapter,
    );
    const anchor = dom.querySelector("a")!;
    expect(anchor.getAttribute("download")).toBe("page.html");
  });
});
