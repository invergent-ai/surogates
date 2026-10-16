import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { WorkspaceFileDrawer } from "../src/components/workspace/workspace-file-drawer";
import type { AgentChatAdapter, AgentChatOfficeEvent } from "../src/types";

vi.mock("../src/lib/office", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/office")>()),
  loadDocsApi: vi.fn().mockResolvedValue(undefined),
}));

import { loadDocsApi, officeEventFor } from "../src/lib/office";

const { loadDocsApi: realLoadDocsApi } = await vi.importActual<typeof import("../src/lib/office")>("../src/lib/office");

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;
const destroyEditor = vi.fn();
const setUsers = vi.fn();
const DocEditor = vi.fn(() => ({ destroyEditor, setUsers }));

beforeEach(() => {
  DocEditor.mockClear();
  destroyEditor.mockClear();
  setUsers.mockClear();
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

  it("without the editor method, an office file offers only the download", async () => {
    const getWorkspaceFile = vi.fn();
    const view = await mount(
      <WorkspaceFileDrawer adapter={adapter({ getWorkspaceFile })} sessionId="s-1" path="docs/Plan.docx" onClose={() => {}} />,
    );
    expect(view.querySelector('[role="alert"]')?.textContent).toContain("Preview not available. Download the file to open it.");
    expect(view.querySelector('a[aria-label="Download Plan.docx"]')?.getAttribute("href")).toBe("/download?path=docs/Plan.docx");
    expect(getWorkspaceFile).not.toHaveBeenCalled();
    expect(DocEditor).not.toHaveBeenCalled();
  });
});

describe("save notices in the editor pane", () => {
  const conflict = { type: "office.conflict" as const, eventId: 12, conflictPath: "docs/Plan (edited 2026-10-13 09.05 UTC).docx" };

  async function rerender(node: React.ReactElement) {
    await act(async () => {
      root?.render(node);
    });
    await act(async () => {});
  }

  it("reopens the document with a fresh config after a conflict and links the copy", async () => {
    const getWorkspaceEditorConfig = vi.fn().mockResolvedValue({ document_server_url: "https://o", config: {} });
    const host = adapter({ getWorkspaceEditorConfig });
    const pane = (officeEvent?: typeof conflict) => (
      <WorkspaceFileDrawer adapter={host} sessionId="s-1" path="docs/Plan.docx"
        officeEvent={officeEvent} onClose={() => {}} />
    );
    const view = await mount(pane());
    expect(getWorkspaceEditorConfig).toHaveBeenCalledTimes(1);
    await rerender(pane(conflict));
    expect(getWorkspaceEditorConfig).toHaveBeenCalledTimes(2);
    expect(destroyEditor).toHaveBeenCalledTimes(1);
    expect(DocEditor).toHaveBeenCalledTimes(2);
    expect(view.querySelector('[role="status"]')?.textContent).toBe(
      "This document changed outside the editor. Your edits were saved to docs/Plan (edited 2026-10-13 09.05 UTC).docx.",
    );
    expect(view.querySelector('[role="alert"]')).toBeNull();
  });

  it("says a save failed without reopening the document", async () => {
    const getWorkspaceEditorConfig = vi.fn().mockResolvedValue({ document_server_url: "https://o", config: {} });
    const host = adapter({ getWorkspaceEditorConfig });
    const pane = (officeEvent?: { type: "office.save_failed"; eventId: number }) => (
      <WorkspaceFileDrawer adapter={host} sessionId="s-1" path="a.docx"
        officeEvent={officeEvent} onClose={() => {}} />
    );
    const view = await mount(pane());
    await rerender(pane({ type: "office.save_failed", eventId: 13 }));
    expect(getWorkspaceEditorConfig).toHaveBeenCalledTimes(1);
    expect(view.querySelector('[role="status"]')?.textContent).toBe("Your latest changes could not be saved yet.");
    expect(view.querySelector('[role="alert"]')).toBeNull();
  });

  it("does not show a notice from before the pane opened", async () => {
    const getWorkspaceEditorConfig = vi.fn().mockResolvedValue({ document_server_url: "https://o", config: {} });
    const view = await mount(
      <WorkspaceFileDrawer adapter={adapter({ getWorkspaceEditorConfig })} sessionId="s-1" path="docs/Plan.docx"
        officeEvent={conflict} onClose={() => {}} />,
    );
    expect(getWorkspaceEditorConfig).toHaveBeenCalledTimes(1);
    expect(view.querySelector('[role="status"]')).toBeNull();
  });

  it.each([
    [{ type: "office.save_waiting", eventId: 13, computer: "Mac", reason: "offline" }, "Saving to Mac once it is back online."],
    [{ type: "office.save_waiting", eventId: 13, computer: "Mac", reason: "waiting" }, "Saving to Mac… If it asks, allow the save there."],
    [{ type: "office.save_failed", eventId: 13, computer: "Mac", reason: "declined" },
      "Mac declined the save. Your changes are still here; the next save asks again."],
  ] as const)("says where a save on the computer is: %o", async (officeEvent, said) => {
    const getWorkspaceEditorConfig = vi.fn().mockResolvedValue({ document_server_url: "https://o", config: {} });
    const host = adapter({ getWorkspaceEditorConfig });
    const pane = (event?: AgentChatOfficeEvent) => (
      <WorkspaceFileDrawer adapter={host} sessionId="s-1" path="a.docx" officeEvent={event} onClose={() => {}} />
    );
    const view = await mount(pane());
    await rerender(pane(officeEvent as AgentChatOfficeEvent));
    expect(view.querySelector('[role="status"]')?.textContent).toBe(said);
    await rerender(pane({ type: "office.saved", eventId: 14 }));
    expect(view.querySelector('[role="status"]')).toBeNull();
    expect(getWorkspaceEditorConfig).toHaveBeenCalledTimes(1);
  });
});

describe("officeEventFor", () => {
  it("finds a document's notice under the spelling its saves are named by", () => {
    const waiting: AgentChatOfficeEvent = { type: "office.save_waiting", eventId: 13, computer: "Mac", reason: "offline" };
    const events = { "Plan.docx": waiting };
    expect(officeEventFor(events, "Plan.docx")).toBe(waiting);
    expect(officeEventFor(events, "plan.docx")).toBe(waiting);
    expect(officeEventFor(events, "other.docx")).toBeUndefined();
  });

  it("prefers the notice named exactly as the pane names its document", () => {
    const exact: AgentChatOfficeEvent = { type: "office.saved", eventId: 14 };
    const other: AgentChatOfficeEvent = { type: "office.save_failed", eventId: 15, reason: "declined" };
    expect(officeEventFor({ "PLAN.docx": other, "plan.docx": exact }, "plan.docx")).toBe(exact);
  });

  it("ignores the Unicode form a name is written in", () => {
    const waiting: AgentChatOfficeEvent = { type: "office.save_waiting", eventId: 16, computer: "Mac", reason: "waiting" };
    expect(officeEventFor({ "R\u00e9sum\u00e9.docx": waiting }, "re\u0301sume\u0301.docx")).toBe(waiting);
  });
});

describe("loadDocsApi", () => {
  let scripts: HTMLScriptElement[];

  beforeEach(() => {
    // Captured, never inserted: happy-dom would fetch the src.
    scripts = [];
    vi.spyOn(document.head, "appendChild").mockImplementation(<T extends Node>(node: T) => {
      scripts.push(node as unknown as HTMLScriptElement);
      return node;
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("retries when the script loads without defining DocsAPI", async () => {
    delete (window as { DocsAPI?: unknown }).DocsAPI;
    const first = realLoadDocsApi("https://no-api.example");
    const removed = vi.spyOn(scripts[0]!, "remove");
    scripts[0]!.onload?.(new Event("load"));
    await expect(first).rejects.toThrow("The document editor could not be loaded.");
    expect(removed).toHaveBeenCalled();

    const second = realLoadDocsApi("https://no-api.example");
    expect(scripts).toHaveLength(2);
    (window as unknown as { DocsAPI: unknown }).DocsAPI = { DocEditor };
    scripts[1]!.onload?.(new Event("load"));
    await expect(second).resolves.toBeUndefined();
  });

  it("gives up after 30 seconds without a load or error, and retries", async () => {
    vi.useFakeTimers();
    const first = realLoadDocsApi("https://slow.example");
    const rejected = expect(first).rejects.toThrow("The document editor could not be loaded.");
    vi.advanceTimersByTime(30_000);
    await rejected;
    realLoadDocsApi("https://slow.example");
    expect(scripts).toHaveLength(2);
  });
});

describe("comments that mention the agent", () => {
  const AGENT = { id: "agent:writer", name: "Writer", email: "writer@agent.invalid" };
  const notice = (emails: string[]) => ({
    data: { emails, actionId: "15689582", actionLink: { action: { type: "comment", data: "15689582" } }, message: "@writer@agent.invalid is Q1 right?" },
  });

  async function opened(over: Partial<AgentChatAdapter> = {}) {
    const config = { document: { key: "k" }, token: "t" };
    const getWorkspaceEditorConfig = vi.fn().mockResolvedValue({ document_server_url: "https://o", config, mentionable_users: [AGENT] });
    const view = await mount(
      <WorkspaceFileDrawer adapter={adapter({ getWorkspaceEditorConfig, ...over })} sessionId="s-1" path="docs/Plan.docx" onClose={() => {}} />,
    );
    const passed = (DocEditor.mock.calls[0] as unknown as [string, Record<string, unknown>])[1];
    return { view, config, passed, events: passed.events as Record<string, (event: { data?: unknown }) => void> };
  }

  it("offers the agent to mention, and sends a comment that mentions it to the chat", async () => {
    const notifyWorkspaceEditorMention = vi.fn().mockResolvedValue(undefined);
    const { config, passed, events } = await opened({ notifyWorkspaceEditorMention });
    expect(passed).toMatchObject(config);
    events.onRequestUsers({ data: { c: "info" } });
    expect(setUsers).toHaveBeenCalledWith({ c: "info", users: [AGENT] });
    act(() => events.onRequestSendNotify(notice(["Writer@Agent.invalid"])));
    expect(notifyWorkspaceEditorMention).toHaveBeenCalledWith({
      sessionId: "s-1", path: "docs/Plan.docx", commentId: "15689582", message: "@writer@agent.invalid is Q1 right?", emails: ["Writer@Agent.invalid"],
    });
  });

  it("sends nothing for a comment that mentions someone else", async () => {
    const notifyWorkspaceEditorMention = vi.fn().mockResolvedValue(undefined);
    const { events } = await opened({ notifyWorkspaceEditorMention });
    events.onRequestSendNotify(notice(["ana@example.com"]));
    expect(notifyWorkspaceEditorMention).not.toHaveBeenCalled();
  });

  it("says when the chat could not be told", async () => {
    const said = "docs/Plan.docx is not open in this chat any more: open it here again to mention the agent.";
    const notifyWorkspaceEditorMention = vi.fn().mockRejectedValue(new Error(said));
    const { view, events } = await opened({ notifyWorkspaceEditorMention });
    await act(async () => {
      events.onRequestSendNotify(notice(["writer@agent.invalid"]));
    });
    await act(async () => {});
    expect([...view.querySelectorAll('[role="status"]')].map((node) => node.textContent)).toContain(`The agent was not told about your comment: ${said}`);
  });

  it("without the adapter method, passes the config as signed", async () => {
    const { config, passed } = await opened();
    expect(passed).toBe(config);
  });
});
