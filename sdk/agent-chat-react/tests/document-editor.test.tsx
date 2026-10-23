import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { ThemeProvider, useTheme } from "next-themes";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DocumentEditor, type EditorOpening } from "../src/components/workspace/document-editor";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;
const destroyEditor = vi.fn();
const setUsers = vi.fn();
const DocEditor = vi.fn(() => ({ destroyEditor, setUsers }));
// The editor scripts the page asked for, captured instead of inserted: happy-dom would fetch their src.
let scripts: HTMLScriptElement[] = [];
// api.js loads once per document server, so each test opens from a server of its own.
let servers = 0;
let server = "";

beforeEach(() => {
  DocEditor.mockClear();
  destroyEditor.mockClear();
  setUsers.mockClear();
  (window as unknown as { DocsAPI: unknown }).DocsAPI = { DocEditor };
  server = `https://docs-${++servers}.example`;
  scripts = [];
  const append = document.head.appendChild.bind(document.head);
  vi.spyOn(document.head, "appendChild").mockImplementation(<T extends Node>(node: T) => {
    if (node instanceof HTMLScriptElement && node.src) {
      scripts.push(node);
      return node;
    }
    return append(node);
  });
});

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  vi.restoreAllMocks();
});

async function settle() {
  await act(async () => {});
  await act(async () => {});
}

async function render(node: React.ReactElement) {
  if (!root) {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  }
  await act(async () => {
    root?.render(node);
  });
  await settle();
  return container as HTMLDivElement;
}

/** The document server's api.js arrives. */
async function loadEditorScript() {
  await act(async () => {
    for (const script of scripts.splice(0)) script.onload?.(new Event("load"));
  });
  await settle();
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("a document editor without a session", () => {
  it("the document editor opens the config it is given", async () => {
    const config = { document: { key: "k" }, token: "t" };
    const open = vi.fn().mockResolvedValue({ document_server_url: server, config });
    const view = await render(
      <ThemeProvider defaultTheme="dark" enableSystem={false}>
        <DocumentEditor open={open} fileName="Plan.docx" />
      </ThemeProvider>,
    );
    expect(open).toHaveBeenCalledWith("dark");
    expect(view.textContent).toContain("Opening Plan.docx…");
    expect(scripts.map((script) => script.src)).toEqual([`${server}/web-apps/apps/api/documents/api.js`]);
    expect(DocEditor).not.toHaveBeenCalled();

    await loadEditorScript();
    expect(DocEditor).toHaveBeenCalledTimes(1);
    const [placeholderId, passed] = DocEditor.mock.calls[0] as unknown as [string, unknown];
    expect(passed).toBe(config);
    expect(view.querySelector(`#${placeholderId}`)).not.toBeNull();
    expect(view.textContent).not.toContain("Opening Plan.docx…");
    expect(view.querySelector('[role="alert"]')).toBeNull();
  });

  it("a failed open shows its message", async () => {
    const open = vi.fn().mockRejectedValue(new Error("The document is still being saved; try again in a moment."));
    const view = await render(<DocumentEditor open={open} fileName="Plan.docx" />);
    expect(open).toHaveBeenCalledWith("light");
    expect(view.querySelector('[role="alert"]')?.textContent).toBe("The document is still being saved; try again in a moment.");
    expect(view.textContent).not.toContain("Opening Plan.docx…");
    expect(scripts).toHaveLength(0);
    expect(DocEditor).not.toHaveBeenCalled();
  });

  it("unmounting destroys the editor", async () => {
    const open = vi.fn().mockResolvedValue({ document_server_url: server, config: {} });
    await render(<DocumentEditor open={open} fileName="Plan.docx" />);
    await loadEditorScript();
    expect(DocEditor).toHaveBeenCalledTimes(1);
    act(() => root?.unmount());
    root = null;
    expect(destroyEditor).toHaveBeenCalledTimes(1);
  });

  it("a new reopen key opens it again", async () => {
    const first = { document: { key: "first" } };
    const second = { document: { key: "second" } };
    const open = vi.fn()
      .mockResolvedValueOnce({ document_server_url: server, config: first })
      .mockResolvedValueOnce({ document_server_url: server, config: second });
    await render(<DocumentEditor open={open} fileName="Plan.docx" reopenKey={1} />);
    await loadEditorScript();
    expect(DocEditor).toHaveBeenCalledTimes(1);

    await render(<DocumentEditor open={open} fileName="Plan.docx" reopenKey={2} />);
    expect(destroyEditor).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledTimes(2);
    expect(destroyEditor.mock.invocationCallOrder[0]).toBeLessThan(open.mock.invocationCallOrder[1]!);
    expect(DocEditor).toHaveBeenCalledTimes(2);
    expect((DocEditor.mock.calls[1] as unknown as [string, unknown])[1]).toBe(second);
  });

  it("a new reopen key while the first open is pending never mounts the first", async () => {
    const first = deferred<EditorOpening>();
    const second = deferred<EditorOpening>();
    const open = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    await render(<DocumentEditor open={open} fileName="Plan.docx" reopenKey={1} />);
    await render(<DocumentEditor open={open} fileName="Plan.docx" reopenKey={2} />);
    expect(open).toHaveBeenCalledTimes(2);

    await act(async () => first.resolve({ document_server_url: server, config: { document: { key: "first" } } }));
    await loadEditorScript();
    expect(DocEditor).not.toHaveBeenCalled();

    const config = { document: { key: "second" } };
    await act(async () => second.resolve({ document_server_url: server, config }));
    await settle();
    await loadEditorScript();
    expect(DocEditor).toHaveBeenCalledTimes(1);
    expect((DocEditor.mock.calls[0] as unknown as [string, unknown])[1]).toBe(config);
    expect(destroyEditor).not.toHaveBeenCalled();
  });

  it("a theme change opens it again in the new theme", async () => {
    let setTheme: (theme: string) => void = () => {};
    function ThemeSwitch() {
      setTheme = useTheme().setTheme;
      return null;
    }
    const open = vi.fn().mockResolvedValue({ document_server_url: server, config: {} });
    await render(
      <ThemeProvider defaultTheme="light" enableSystem={false} storageKey="document-editor-test-theme">
        <ThemeSwitch />
        <DocumentEditor open={open} fileName="Plan.docx" />
      </ThemeProvider>,
    );
    await loadEditorScript();
    expect(open).toHaveBeenLastCalledWith("light");

    await act(async () => setTheme("dark"));
    await settle();
    expect(destroyEditor).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledTimes(2);
    expect(open).toHaveBeenLastCalledWith("dark");
    expect(DocEditor).toHaveBeenCalledTimes(2);
  });

  it("the events it builds go beside the config, and see the editor only while it is open", async () => {
    const config = { document: { key: "k" }, token: "t" };
    const opening = { document_server_url: server, config, mentionable_users: [{ id: "agent:a", name: "A", email: "a@agent.invalid" }] };
    let editor: () => unknown = () => undefined;
    const onRequestUsers = vi.fn();
    const events = vi.fn((current: () => unknown, opened: EditorOpening) => {
      editor = current;
      return opened.mentionable_users?.length ? { onRequestUsers } : null;
    });
    await render(<DocumentEditor open={() => Promise.resolve(opening)} fileName="Plan.docx" events={events} />);
    await loadEditorScript();
    expect(events).toHaveBeenCalledWith(expect.any(Function), opening);
    const passed = (DocEditor.mock.calls[0] as unknown as [string, Record<string, unknown>])[1];
    expect(passed).toEqual({ ...config, events: { onRequestUsers } });
    expect(editor()).toBe(DocEditor.mock.results[0]!.value);

    act(() => root?.unmount());
    root = null;
    expect(editor()).toBeNull();
  });
});
