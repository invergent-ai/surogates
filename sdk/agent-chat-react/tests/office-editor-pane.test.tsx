import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { WorkspaceFileDrawer } from "../src/components/workspace/workspace-file-drawer";
import type { AgentChatAdapter } from "../src/types";

vi.mock("../src/lib/office", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/office")>()),
  loadDocsApi: vi.fn().mockResolvedValue(undefined),
}));

import { loadDocsApi } from "../src/lib/office";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;
const destroyEditor = vi.fn();
const DocEditor = vi.fn(() => ({ destroyEditor }));

beforeEach(() => {
  DocEditor.mockClear();
  destroyEditor.mockClear();
  (window as unknown as { DocsAPI: unknown }).DocsAPI = { DocEditor };
});

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function adapter(over: Partial<AgentChatAdapter> = {}): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    getWorkspaceDownloadUrl: ({ path }: { path: string }) => `/download?path=${path}`,
    getWorkspaceFile: vi.fn().mockResolvedValue({ path: "scan.pdf", content: "JVBERi0=", size: 6, mime_type: "application/pdf", encoding: "base64", truncated: false }),
    ...over,
  } as unknown as AgentChatAdapter;
}

async function mount(node: React.ReactElement) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(node);
  });
  await act(async () => {});
  return container;
}

describe("office documents in the file pane", () => {
  it("opens the editor with the signed config", async () => {
    const config = { document: { key: "k" }, token: "t" };
    const getWorkspaceEditorConfig = vi.fn().mockResolvedValue({ document_server_url: "https://office.example", config });
    const view = await mount(
      <WorkspaceFileDrawer adapter={adapter({ getWorkspaceEditorConfig })} sessionId="s-1" path="docs/Plan.docx" onClose={() => {}} />,
    );
    expect(getWorkspaceEditorConfig).toHaveBeenCalledWith({ sessionId: "s-1", path: "docs/Plan.docx", theme: "light", view: false });
    expect(loadDocsApi).toHaveBeenCalledWith("https://office.example");
    expect(DocEditor).toHaveBeenCalledTimes(1);
    const [placeholderId, passed] = DocEditor.mock.calls[0] as unknown as [string, unknown];
    expect(passed).toBe(config);
    expect(view.querySelector(`#${placeholderId}`)).not.toBeNull();
    expect(view.querySelector('a[aria-label="Download Plan.docx"]')?.getAttribute("href")).toBe("/download?path=docs/Plan.docx");
  });

  it("asks for view mode in a read-only chat", async () => {
    const getWorkspaceEditorConfig = vi.fn().mockResolvedValue({ document_server_url: "https://o", config: {} });
    await mount(<WorkspaceFileDrawer adapter={adapter({ getWorkspaceEditorConfig })} sessionId="s-1" path="a.xlsx" view onClose={() => {}} />);
    expect(getWorkspaceEditorConfig).toHaveBeenCalledWith(expect.objectContaining({ view: true }));
  });

  it("destroys the editor when the pane closes", async () => {
    const getWorkspaceEditorConfig = vi.fn().mockResolvedValue({ document_server_url: "https://o", config: {} });
    await mount(<WorkspaceFileDrawer adapter={adapter({ getWorkspaceEditorConfig })} sessionId="s-1" path="a.pptx" onClose={() => {}} />);
    act(() => root?.unmount());
    root = null;
    expect(destroyEditor).toHaveBeenCalledTimes(1);
  });

  it("shows why the editor cannot open, with the download link", async () => {
    const getWorkspaceEditorConfig = vi.fn().mockRejectedValue(new Error("No document server is configured."));
    const view = await mount(<WorkspaceFileDrawer adapter={adapter({ getWorkspaceEditorConfig })} sessionId="s-1" path="a.docx" onClose={() => {}} />);
    expect(view.querySelector('[role="alert"]')?.textContent).toContain("No document server is configured.");
    expect(view.querySelector('a[aria-label="Download a.docx"]')).not.toBeNull();
    expect(DocEditor).not.toHaveBeenCalled();
  });

  it("shows an error when the editor script cannot load", async () => {
    vi.mocked(loadDocsApi).mockRejectedValueOnce(new Error("The document editor could not be loaded."));
    const getWorkspaceEditorConfig = vi.fn().mockResolvedValue({ document_server_url: "https://down", config: {} });
    const view = await mount(<WorkspaceFileDrawer adapter={adapter({ getWorkspaceEditorConfig })} sessionId="s-1" path="a.docx" onClose={() => {}} />);
    expect(view.querySelector('[role="alert"]')?.textContent).toContain("could not be loaded");
  });

  it("without the editor method, a PDF says preview is not available", async () => {
    const view = await mount(<WorkspaceFileDrawer adapter={adapter()} sessionId="s-1" path="scan.pdf" onClose={() => {}} />);
    expect(view.textContent).toContain("Preview not available");
    expect(view.querySelector("img")).toBeNull();
  });
});
