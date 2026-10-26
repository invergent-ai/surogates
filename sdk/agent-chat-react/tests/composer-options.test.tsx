// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// An embedder can trim the chat down: hide the Simple/Advanced switch and
// pick the mode the chat is in, hide the Files fold, and turn off adding
// files to a message. Left at their defaults, all four show and work as
// before.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { AgentChat, type AgentChatProps } from "../src/agent-chat";
import { useAgentChatRuntime } from "../src/runtime/use-agent-chat-runtime";
import type {
  AgentChatAdapter,
  AgentChatEventStream,
  AgentChatEventType,
  AgentChatRuntimeApi,
  AgentChatSseMessageEvent,
} from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const VIEW_MODE_KEY = "@invergent/agent-chat-react:viewMode";
// A system marker the thread draws in Advanced and leaves out of Simple.
const MARKER = "zebra-skill";

class FakeEventStream implements AgentChatEventStream {
  onerror: (() => void) | null = null;
  readonly listeners = new Map<
    AgentChatEventType,
    Array<(event: AgentChatSseMessageEvent) => void>
  >();

  addEventListener(
    type: AgentChatEventType,
    listener: (event: AgentChatSseMessageEvent) => void,
  ): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close(): void {}

  emit(type: AgentChatEventType, eventId: number, data: Record<string, unknown>) {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data: JSON.stringify(data), lastEventId: String(eventId) });
    }
  }
}

function adapterStub(
  stream: FakeEventStream,
  overrides: Partial<AgentChatAdapter> = {},
) {
  return {
    ...NO_BROWSER_ADAPTER,
    listSessions: vi.fn().mockResolvedValue({ sessions: [], total: 0 }),
    createSession: vi.fn(),
    getSession: vi.fn().mockResolvedValue({ id: "s-1", status: "active" }),
    sendMessage: vi.fn().mockResolvedValue({ eventId: 1, status: "accepted" }),
    openEventStream: vi.fn(() => stream),
    listSlashCommands: vi.fn().mockResolvedValue([]),
    getWorkspaceTree: vi.fn().mockResolvedValue({
      root: "workspace",
      entries: [
        { name: "main.py", path: "main.py", kind: "file" as const, size: 12 },
      ],
      truncated: false,
    }),
    ...overrides,
  } as unknown as AgentChatAdapter;
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  window.localStorage.clear();
});

async function mount(
  props: Partial<AgentChatProps> = {},
  overrides: Partial<AgentChatAdapter> = {},
): Promise<{
  dom: HTMLDivElement;
  stream: FakeEventStream;
  adapter: AgentChatAdapter;
}> {
  const stream = new FakeEventStream();
  const adapter = adapterStub(stream, overrides);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(<AgentChat adapter={adapter} sessionId="s-1" {...props} />);
    await Promise.resolve();
  });
  await settle();
  return { dom: container, stream, adapter };
}

// Lets the adapter's async reads land and React render what they set.
async function settle(): Promise<void> {
  for (let i = 0; i < 3; i++) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

function viewModeGroup(dom: HTMLElement): Element | null {
  return dom.querySelector("[role='group'][aria-label='Chat view mode']");
}

function pressedView(dom: HTMLElement): string | undefined {
  return Array.from(
    viewModeGroup(dom)?.querySelectorAll("button") ?? [],
  )
    .find((b) => b.getAttribute("aria-pressed") === "true")
    ?.getAttribute("aria-label")
    ?.replace(" view", "");
}

// The thread's own mode, seen from what it draws, for when there is no
// switch to read it from.
async function renderedMode(
  dom: HTMLElement,
  stream: FakeEventStream,
): Promise<"Simple" | "Advanced"> {
  await act(async () => {
    stream.emit("skill.invoked", 1, { skill: MARKER });
    stream.emit("stream.live", 0, {});
    await Promise.resolve();
  });
  return dom.textContent?.includes(MARKER) ? "Advanced" : "Simple";
}

function plusButton(dom: HTMLElement): Element | null {
  return dom.querySelector("button[aria-label='Composer tools']");
}

function filesTab(dom: HTMLElement): Element | null {
  return dom.querySelector("[data-testid='composer-tray-files']");
}

function browserTab(dom: HTMLElement): Element | null {
  return dom.querySelector("[data-testid='composer-tray-browser']");
}

function removeButton(dom: HTMLElement, name: string): Element | null {
  return dom.querySelector(`button[aria-label='Remove ${name}']`);
}

function dropFile(dom: HTMLElement, file: File): Event {
  const form = dom.querySelector("form");
  if (!form) throw new Error("composer form not rendered");
  const event = new Event("drop", { bubbles: true, cancelable: true });
  Object.defineProperty(event, "dataTransfer", {
    value: { types: ["Files"], files: [file] },
  });
  act(() => {
    form.dispatchEvent(event);
  });
  return event;
}

function pasteFile(dom: HTMLElement, file: File): Event {
  const textarea = dom.querySelector("textarea");
  if (!textarea) throw new Error("composer textarea not rendered");
  const event = new Event("paste", { bubbles: true, cancelable: true });
  Object.defineProperty(event, "clipboardData", {
    value: { items: [{ kind: "file", getAsFile: () => file }] },
  });
  act(() => {
    textarea.dispatchEvent(event);
  });
  return event;
}

function note(): File {
  return new File(["hello"], "notes.txt", { type: "text/plain" });
}

// Over the 50 MB the composer allows for one file, without allocating it.
function hugeNote(): File {
  const file = note();
  Object.defineProperty(file, "size", { value: 50_000_001 });
  return file;
}

function dragOverFiles(dom: HTMLElement): { event: Event; dataTransfer: { dropEffect: string } } {
  const form = dom.querySelector("form");
  if (!form) throw new Error("composer form not rendered");
  const event = new Event("dragover", { bubbles: true, cancelable: true });
  const dataTransfer = { types: ["Files"], dropEffect: "copy" };
  Object.defineProperty(event, "dataTransfer", { value: dataTransfer });
  act(() => {
    form.dispatchEvent(event);
  });
  return { event, dataTransfer };
}

describe("composer options an embedder can set", () => {
  describe("defaults", () => {
    it("draw the view mode switch, the plus button and the Files fold", async () => {
      const { dom } = await mount();

      expect(viewModeGroup(dom)).not.toBeNull();
      expect(pressedView(dom)).toBe("Simple");
      expect(plusButton(dom)).not.toBeNull();
      expect(filesTab(dom)).not.toBeNull();
    });
  });

  describe("viewModeToggleEnabled={false}", () => {
    it("draws no view mode switch", async () => {
      const { dom } = await mount({ viewModeToggleEnabled: false });

      expect(viewModeGroup(dom)).toBeNull();
    });

    it("stays Simple though the user once chose Advanced", async () => {
      window.localStorage.setItem(VIEW_MODE_KEY, "expert");
      const { dom, stream } = await mount({ viewModeToggleEnabled: false });

      expect(await renderedMode(dom, stream)).toBe("Simple");
    });

    it("stays Simple though the adapter holds Advanced", async () => {
      const { dom, stream } = await mount(
        { viewModeToggleEnabled: false },
        { getChatViewMode: vi.fn().mockResolvedValue("expert") },
      );

      expect(await renderedMode(dom, stream)).toBe("Simple");
    });

    it("stays Advanced with defaultViewMode='expert', whatever the user chose before", async () => {
      window.localStorage.setItem(VIEW_MODE_KEY, "simple");
      const { dom, stream } = await mount(
        { viewModeToggleEnabled: false, defaultViewMode: "expert" },
        { getChatViewMode: vi.fn().mockResolvedValue("simple") },
      );

      expect(await renderedMode(dom, stream)).toBe("Advanced");
    });

    it("writes nothing to localStorage or the adapter", async () => {
      const setChatViewMode = vi.fn().mockResolvedValue(undefined);
      await mount(
        { viewModeToggleEnabled: false, defaultViewMode: "expert" },
        { setChatViewMode },
      );

      expect(window.localStorage.getItem(VIEW_MODE_KEY)).toBeNull();
      expect(setChatViewMode).not.toHaveBeenCalled();
    });

    it("never asks the adapter which mode it holds", async () => {
      const getChatViewMode = vi.fn().mockResolvedValue("expert");
      await mount({ viewModeToggleEnabled: false }, { getChatViewMode });

      expect(getChatViewMode).not.toHaveBeenCalled();
    });

    it("lets the runtime's setViewMode change nothing and write nothing", async () => {
      const ref: { current: AgentChatRuntimeApi | null } = { current: null };
      const setChatViewMode = vi.fn().mockResolvedValue(undefined);
      const adapter = adapterStub(new FakeEventStream(), { setChatViewMode });
      function Consumer() {
        ref.current = useAgentChatRuntime({
          adapter,
          sessionId: null,
          viewModeLocked: true,
          defaultViewMode: "expert",
        });
        return null;
      }
      container = document.createElement("div");
      document.body.appendChild(container);
      root = createRoot(container);
      await act(async () => {
        root?.render(<Consumer />);
      });

      expect(ref.current?.viewMode).toBe("expert");
      await act(async () => {
        ref.current?.setViewMode("simple");
      });

      expect(ref.current?.viewMode).toBe("expert");
      expect(window.localStorage.getItem(VIEW_MODE_KEY)).toBeNull();
      expect(setChatViewMode).not.toHaveBeenCalled();
    });
  });

  describe("defaultViewMode with the switch shown", () => {
    it("opens Advanced, and persists nothing", async () => {
      const { dom } = await mount({ defaultViewMode: "expert" });

      expect(pressedView(dom)).toBe("Advanced");
      expect(window.localStorage.getItem(VIEW_MODE_KEY)).toBeNull();
    });

    it("gives way to what the user chose before", async () => {
      window.localStorage.setItem(VIEW_MODE_KEY, "simple");
      const { dom } = await mount({ defaultViewMode: "expert" });

      expect(pressedView(dom)).toBe("Simple");
    });

    it("gives way to what the adapter holds", async () => {
      const { dom } = await mount(
        { defaultViewMode: "expert" },
        { getChatViewMode: vi.fn().mockResolvedValue("simple") },
      );

      expect(pressedView(dom)).toBe("Simple");
    });

    it("stays in the mode it opened in when the chat gets its session", async () => {
      const { dom, adapter } = await mount({
        sessionId: null,
        defaultViewMode: "expert",
      });
      expect(pressedView(dom)).toBe("Advanced");

      await act(async () => {
        root?.render(
          <AgentChat
            adapter={adapter}
            sessionId="s-1"
            defaultViewMode="expert"
          />,
        );
        await Promise.resolve();
      });
      await settle();

      expect(pressedView(dom)).toBe("Advanced");
    });

    it("opens Simple when left out", async () => {
      const { dom } = await mount();

      expect(pressedView(dom)).toBe("Simple");
    });

    it("still changes mode, and remembers it, when the user picks one", async () => {
      const { dom } = await mount({ defaultViewMode: "expert" });
      const simple = viewModeGroup(dom)?.querySelector<HTMLButtonElement>(
        "button[aria-label='Simple view']",
      );
      await act(async () => {
        simple?.click();
      });

      expect(pressedView(dom)).toBe("Simple");
      expect(window.localStorage.getItem(VIEW_MODE_KEY)).toBe("simple");
    });
  });

  describe("filesEnabled={false}", () => {
    it("draws no Files entry, and no tray with nothing else in it", async () => {
      const { dom } = await mount({ filesEnabled: false });

      expect(filesTab(dom)).toBeNull();
      expect(dom.querySelector("[data-testid='composer-tray']")).toBeNull();
    });

    it("keeps the Browser entry", async () => {
      const { dom, stream } = await mount(
        { filesEnabled: false },
        {
          getBrowserState: vi
            .fn()
            .mockResolvedValue({ status: "live", controlOwner: null, liveViewPath: "" }),
        },
      );
      await act(async () => {
        stream.emit("browser.provisioned", 10, { session_id: "s-1" });
        await Promise.resolve();
      });

      expect(filesTab(dom)).toBeNull();
      expect(browserTab(dom)).not.toBeNull();
    });
  });

  describe("attachmentsEnabled={false}", () => {
    it("draws no plus button", async () => {
      const { dom } = await mount({ attachmentsEnabled: false });

      expect(plusButton(dom)).toBeNull();
    });

    it("keeps the plus button for the browser profile picker, without the attach items", async () => {
      const { dom } = await mount(
        {
          attachmentsEnabled: false,
          browserProfilesEnabled: true,
          onSelectBrowserProfile: vi.fn(),
        },
        { listBrowserProfiles: vi.fn().mockResolvedValue([]) },
      );

      const plus = plusButton(dom);
      expect(plus).not.toBeNull();
      await act(async () => {
        (plus as HTMLButtonElement).click();
        await Promise.resolve();
      });

      expect(document.body.textContent).toContain("Browser profile");
      expect(document.body.textContent).not.toContain("Add local files");
    });

    it("ignores a file dropped on the composer, yet keeps the browser from opening it", async () => {
      const { dom } = await mount({ attachmentsEnabled: false });

      const event = dropFile(dom, note());
      await settle();

      expect(event.defaultPrevented).toBe(true);
      expect(removeButton(dom, "notes.txt")).toBeNull();
      expect(dom.textContent).not.toContain("notes.txt");
    });

    it("ignores a file pasted into the composer", async () => {
      const { dom } = await mount({ attachmentsEnabled: false });

      pasteFile(dom, note());
      await settle();

      expect(removeButton(dom, "notes.txt")).toBeNull();
      expect(dom.textContent).not.toContain("notes.txt");
    });

    it("reports no error for the file it ignores", async () => {
      const onComposerError = vi.fn();
      const { dom } = await mount({ attachmentsEnabled: false, onComposerError });

      dropFile(dom, hugeNote());
      await settle();

      expect(onComposerError).not.toHaveBeenCalled();
    });

    it("tells the browser a drag over the composer will not be accepted, yet keeps it from opening the file", async () => {
      const { dom } = await mount({ attachmentsEnabled: false });

      const { event, dataTransfer } = dragOverFiles(dom);

      expect(event.defaultPrevented).toBe(true);
      expect(dataTransfer.dropEffect).toBe("none");
    });
  });

  describe("attachmentsEnabled left out", () => {
    it("reports the error for a file over the size limit", async () => {
      const onComposerError = vi.fn();
      const { dom } = await mount({ onComposerError });

      dropFile(dom, hugeNote());
      await settle();

      expect(onComposerError).toHaveBeenCalledWith(
        expect.objectContaining({ code: "max_file_size" }),
      );
      expect(removeButton(dom, "notes.txt")).toBeNull();
    });

    it("leaves the drag cursor alone", async () => {
      const { dom } = await mount();

      const { event, dataTransfer } = dragOverFiles(dom);

      expect(event.defaultPrevented).toBe(true);
      expect(dataTransfer.dropEffect).toBe("copy");
    });

    it("attaches a file dropped on the composer", async () => {
      const { dom } = await mount();

      const event = dropFile(dom, note());
      await settle();

      expect(event.defaultPrevented).toBe(true);
      expect(removeButton(dom, "notes.txt")).not.toBeNull();
    });

    it("attaches a file pasted into the composer", async () => {
      const { dom } = await mount();

      pasteFile(dom, note());
      await settle();

      expect(removeButton(dom, "notes.txt")).not.toBeNull();
    });
  });
});
