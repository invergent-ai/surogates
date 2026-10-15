// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// TurnDownloads — the turn's files under its last answer, laid out as
// Claude lists them: a card per file and nothing else.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { TurnDownloads } from "../src/components/chat/turn-downloads";
import type { AgentChatAdapter, AgentChatTurnArtifactRef } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function render(files: AgentChatTurnArtifactRef[], onFileSelect = vi.fn()): HTMLDivElement {
  const adapter = {
    ...NO_BROWSER_ADAPTER,
    getWorkspaceDownloadUrl: ({ path }: { path: string }) => `/download?path=${encodeURIComponent(path)}`,
  } as unknown as AgentChatAdapter;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <AgentChatAdapterProvider value={{ adapter, sessionId: "s-1", onFileSelect }}>
        <TurnDownloads sessionId="s-1" files={files} />
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

// The harness labels a file with its workspace path; the card shows its name.
const file = (ref: string): AgentChatTurnArtifactRef => ({ kind: "file", label: ref, ref });

describe("TurnDownloads", () => {
  it("names each file without its extension, over its kind and extension", () => {
    const dom = render([file("out/report.docx"), file("notes.txt")]);
    const cards = [...dom.querySelectorAll("[role='button']")];
    expect(cards.map((c) => c.textContent)).toEqual([
      "reportDocument · DOCXDownload",
      "notesTXTDownload",
    ]);
    expect(dom.textContent).not.toContain("Summary");
    expect(dom.textContent).not.toContain("Download all");
  });

  it("opens the file from its card and downloads it from its button", () => {
    const onFileSelect = vi.fn();
    const dom = render([file("out/report.docx")], onFileSelect);
    act(() => dom.querySelector<HTMLElement>("[role='button']")!.click());
    expect(onFileSelect).toHaveBeenCalledWith("out/report.docx");
    const link = dom.querySelector<HTMLAnchorElement>("a[download]")!;
    expect(link.getAttribute("download")).toBe("report.docx");
    expect(link.getAttribute("href")).toBe("/download?path=out%2Freport.docx");
  });
});
