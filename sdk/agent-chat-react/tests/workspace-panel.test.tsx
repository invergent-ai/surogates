/**
 * WorkspacePanel — each chat's waits, refusals, locks and tree are that chat's.
 *
 * A local-folder chat's upload or delete may wait for its user on the
 * computer its folder is on, and the adapter sends it again meanwhile: it
 * goes on once the panel is folded away or shows another chat, and its end
 * is said over its own chat alone. The tree's read is the shown panel's:
 * it stops as the panel goes.
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkspacePanel } from "../src/components/workspace/workspace-panel";
import { forgetChatFiles } from "../src/components/workspace/chat-files";
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
  forgetChatFiles();
});

describe("WorkspacePanel", () => {
  it("goes on sending an upload still waiting once it unmounts", async () => {
    const signals: Array<AbortSignal | undefined> = [];
    const adapter = {
      getWorkspaceTree: vi.fn().mockResolvedValue({ root: "r", entries: [], truncated: false }),
      // Waiting for the computer, as a change its user has not answered does.
      uploadWorkspaceFile: vi.fn(({ signal }: { signal?: AbortSignal }) => {
        signals.push(signal);
        return new Promise(() => {});
      }),
      deleteWorkspaceFile: vi.fn(),
    } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <TooltipProvider>
          <WorkspacePanel adapter={adapter} sessionId="s-1" selectedPath={null} onSelectedPathChange={() => {}} />
        </TooltipProvider>,
      );
    });
    const input = container.querySelector("input[type=file]") as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [new File(["draft"], "notes.txt")] });
    await act(async () => {
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(signals).toHaveLength(1);
    act(() => root?.unmount());
    root = null;
    // Nothing the panel hands it stops it.
    expect(signals[0]?.aborted ?? false).toBe(false);
  });

  it("shows a chat only its own tree under its wait, never the last chat's files", async () => {
    const adapter = {
      getWorkspaceTree: vi.fn(({ sessionId, onWaiting }: { sessionId: string; onWaiting?: (said: string) => void }) => {
        if (sessionId === "s-1") {
          return Promise.resolve({ root: "r", entries: [{ name: "notes.txt", path: "notes.txt", kind: "file" }], truncated: false });
        }
        onWaiting?.("Waiting for chat two's computer");
        return new Promise(() => {});
      }),
      uploadWorkspaceFile: vi.fn(),
      deleteWorkspaceFile: vi.fn(),
      getWorkspaceDownloadUrl: vi.fn(() => "#"),
    } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    const panel = (sessionId: string) => (
      <TooltipProvider>
        <WorkspacePanel adapter={adapter} sessionId={sessionId} selectedPath={null} onSelectedPathChange={() => {}} />
      </TooltipProvider>
    );
    await act(async () => {
      root?.render(panel("s-1"));
    });
    expect(container.textContent).toContain("notes.txt");
    await act(async () => {
      root?.render(panel("s-2"));
    });
    expect(container.querySelector('[data-testid="tree-waiting"]')?.textContent).toBe("Waiting for chat two's computer");
    expect(container.textContent).not.toContain("notes.txt");
  });

  it("says it waits for the computer while the tree does, then shows its files, and nothing of a wait told after them", async () => {
    let answer: (tree: unknown) => void = () => {};
    let told: ((said: string) => void) | undefined;
    const adapter = {
      getWorkspaceTree: vi.fn(({ onWaiting }: { onWaiting?: (said: string) => void }) => {
        told = onWaiting;
        onWaiting?.("Waiting for Flavius's ThinkPad");
        return new Promise((resolve) => {
          answer = resolve;
        });
      }),
      uploadWorkspaceFile: vi.fn(),
      deleteWorkspaceFile: vi.fn(),
      getWorkspaceDownloadUrl: vi.fn(() => "#"),
    } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <TooltipProvider>
          <WorkspacePanel adapter={adapter} sessionId="s-1" selectedPath={null} onSelectedPathChange={() => {}} />
        </TooltipProvider>,
      );
    });
    const waiting = () => container!.querySelector('[data-testid="tree-waiting"]');
    expect([waiting()?.getAttribute("role"), waiting()?.textContent]).toEqual(["status", "Waiting for Flavius's ThinkPad"]);
    await act(async () => {
      answer({ root: "r", entries: [{ name: "notes.txt", path: "notes.txt", kind: "file" }], truncated: false });
    });
    expect(waiting()).toBeNull();
    expect(container.textContent).toContain("notes.txt");
    // The host names the computer once it has asked for the chat, which can be after the files came.
    act(() => told?.("Waiting for Flavius's ThinkPad"));
    expect(waiting()).toBeNull();
  });

  it("stops waiting for the computer once it unmounts", async () => {
    const signals: Array<AbortSignal | undefined> = [];
    const adapter = {
      getWorkspaceTree: vi.fn(({ signal }: { signal?: AbortSignal }) => {
        signals.push(signal);
        return new Promise(() => {});
      }),
      uploadWorkspaceFile: vi.fn(),
      deleteWorkspaceFile: vi.fn(),
    } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <TooltipProvider>
          <WorkspacePanel adapter={adapter} sessionId="s-1" selectedPath={null} onSelectedPathChange={() => {}} />
        </TooltipProvider>,
      );
    });
    expect(signals[0]?.aborted).toBe(false);
    act(() => root?.unmount());
    root = null;
    expect(signals[0]?.aborted).toBe(true);
  });

  it("stops waiting for a chat's tree once it reads another chat's, and says nothing of the first's wait", async () => {
    const signals: Array<AbortSignal | undefined> = [];
    const adapter = {
      getWorkspaceTree: vi.fn(({ sessionId, signal, onWaiting }: { sessionId: string; signal?: AbortSignal; onWaiting?: (said: string) => void }) => {
        signals.push(signal);
        // The second chat's tree is on its way.
        if (sessionId === "s-2") return new Promise(() => {});
        return new Promise((_resolve, reject) => {
          signal?.addEventListener("abort", () => {
            // Waiting for the first chat's computer as the panel moves on, and told so late.
            onWaiting?.("Waiting for Flavius's ThinkPad");
            reject(signal.reason);
          });
        });
      }),
      uploadWorkspaceFile: vi.fn(),
      deleteWorkspaceFile: vi.fn(),
    } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    const panel = (sessionId: string) => (
      <TooltipProvider>
        <WorkspacePanel adapter={adapter} sessionId={sessionId} selectedPath={null} onSelectedPathChange={() => {}} />
      </TooltipProvider>
    );
    await act(async () => {
      root?.render(panel("s-1"));
    });
    await act(async () => {
      root?.render(panel("s-2"));
    });
    expect(signals[0]?.aborted).toBe(true);
    expect(container.querySelector('[data-testid="tree-waiting"]')).toBeNull();
  });

  it("leaves the shown chat's read alone as a change of a chat it has left ends, and says nothing of that change's wait", async () => {
    const reads: Array<[string, AbortSignal | undefined]> = [];
    let uploaded: () => void = () => {};
    let waiting: ((said: string) => void) | undefined;
    const adapter = {
      getWorkspaceTree: vi.fn(({ sessionId, signal, onWaiting }: { sessionId: string; signal?: AbortSignal; onWaiting?: (said: string) => void }) => {
        reads.push([sessionId, signal]);
        if (sessionId === "s-1") return Promise.resolve({ root: "r", entries: [], truncated: false });
        // The second chat's tree waits for its computer.
        onWaiting?.("Waiting for chat two's computer");
        return new Promise(() => {});
      }),
      uploadWorkspaceFile: vi.fn(({ onWaiting }: { onWaiting?: (said: string) => void }) => {
        waiting = onWaiting;
        return new Promise((resolve) => {
          uploaded = () => resolve({ path: "notes.txt" });
        });
      }),
      deleteWorkspaceFile: vi.fn(),
    } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    const panel = (sessionId: string) => (
      <TooltipProvider>
        <WorkspacePanel adapter={adapter} sessionId={sessionId} selectedPath={null} onSelectedPathChange={() => {}} />
      </TooltipProvider>
    );
    await act(async () => {
      root?.render(panel("s-1"));
    });
    const input = container.querySelector("input[type=file]") as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [new File(["draft"], "notes.txt")] });
    await act(async () => {
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => {
      root?.render(panel("s-2"));
    });
    act(() => waiting?.("Waiting for you to allow this on chat one's ThinkPad"));
    expect(container.querySelector('[data-testid="workspace-notice"]')).toBeNull();
    await act(async () => {
      uploaded();
    });
    expect(container.querySelector('[data-testid="workspace-notice"]')).toBeNull();
    expect(reads.map(([sessionId, signal]) => [sessionId, signal?.aborted])).toEqual([["s-1", false], ["s-2", false]]);
    expect(container.querySelector('[data-testid="tree-waiting"]')?.textContent).toBe("Waiting for chat two's computer");
  });

  it("keeps a chat's wait and its Upload lock to that chat: neither over the chat it moves to, both again on its way back", async () => {
    const adapter = {
      getWorkspaceTree: vi.fn().mockResolvedValue({ root: "r", entries: [], truncated: false }),
      uploadWorkspaceFile: vi.fn(({ onWaiting }: { onWaiting?: (said: string) => void }) => {
        onWaiting?.("Waiting for you to allow this on chat one's ThinkPad");
        return new Promise(() => {});
      }),
      deleteWorkspaceFile: vi.fn(),
    } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    const panel = (sessionId: string) => (
      <TooltipProvider>
        <WorkspacePanel adapter={adapter} sessionId={sessionId} selectedPath={null} onSelectedPathChange={() => {}} />
      </TooltipProvider>
    );
    const notice = () => container!.querySelector('[data-testid="workspace-notice"]')?.textContent ?? null;
    const locked = () => container!.querySelector<HTMLButtonElement>('button[aria-label="Upload files"]')?.disabled;
    await act(async () => {
      root?.render(panel("s-1"));
    });
    const input = container.querySelector("input[type=file]") as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [new File(["draft"], "notes.txt")] });
    await act(async () => {
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect([notice(), locked()]).toEqual(["Waiting for you to allow this on chat one's ThinkPad", true]);
    await act(async () => {
      root?.render(panel("s-2"));
    });
    expect([notice(), locked()]).toEqual([null, false]);
    await act(async () => {
      root?.render(panel("s-1"));
    });
    expect([notice(), locked()]).toEqual(["Waiting for you to allow this on chat one's ThinkPad", true]);
  });

  it("says how a change of a chat it has left ended only over that chat, once it is back there", async () => {
    let refuse: () => void = () => {};
    const adapter = {
      getWorkspaceTree: vi.fn().mockResolvedValue({ root: "r", entries: [], truncated: false }),
      uploadWorkspaceFile: vi.fn(() => new Promise((_resolve, reject) => {
        refuse = () => reject(new Error("Local access revoked"));
      })),
      deleteWorkspaceFile: vi.fn(),
    } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    const panel = (sessionId: string) => (
      <TooltipProvider>
        <WorkspacePanel adapter={adapter} sessionId={sessionId} selectedPath={null} onSelectedPathChange={() => {}} />
      </TooltipProvider>
    );
    const notice = () => container!.querySelector('[data-testid="workspace-notice"]')?.textContent ?? null;
    await act(async () => {
      root?.render(panel("s-1"));
    });
    const input = container.querySelector("input[type=file]") as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [new File(["draft"], "notes.txt")] });
    await act(async () => {
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => {
      root?.render(panel("s-2"));
    });
    await act(async () => {
      refuse();
    });
    expect(notice()).toBeNull();
    await act(async () => {
      root?.render(panel("s-1"));
    });
    expect(notice()).toBe("Local access revoked");
  });

  it("says nothing of a chat's tree wait over the tree of the chat it moves to, on its way, or over its own on the way back", async () => {
    let told = false;
    const adapter = {
      // Chat one's first read waits for its computer; its read on the way back is still on its way, and waits for nothing.
      getWorkspaceTree: vi.fn(({ sessionId, onWaiting }: { sessionId: string; onWaiting?: (said: string) => void }) => {
        if (sessionId === "s-1" && !told) {
          told = true;
          onWaiting?.("Waiting for chat one's ThinkPad");
        }
        return new Promise(() => {});
      }),
      uploadWorkspaceFile: vi.fn(),
      deleteWorkspaceFile: vi.fn(),
    } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    const panel = (sessionId: string) => (
      <TooltipProvider>
        <WorkspacePanel adapter={adapter} sessionId={sessionId} selectedPath={null} onSelectedPathChange={() => {}} />
      </TooltipProvider>
    );
    const waiting = () => container!.querySelector('[data-testid="tree-waiting"]')?.textContent ?? null;
    await act(async () => {
      root?.render(panel("s-1"));
    });
    expect(waiting()).toBe("Waiting for chat one's ThinkPad");
    await act(async () => {
      root?.render(panel("s-2"));
    });
    expect(waiting()).toBeNull();
    await act(async () => {
      root?.render(panel("s-1"));
    });
    expect(waiting()).toBeNull();
  });

  it("asks a delete only over its own chat, and its wait and its end leave the chat it moved to alone", async () => {
    let deleted: () => void = () => {};
    let deleteWaiting: ((said: string) => void) | undefined;
    const adapter = {
      getWorkspaceTree: vi.fn().mockResolvedValue({
        root: "r",
        entries: [{ name: "notes.txt", path: "notes.txt", kind: "file" }],
        truncated: false,
      }),
      uploadWorkspaceFile: vi.fn(),
      deleteWorkspaceFile: vi.fn(({ onWaiting }: { onWaiting?: (said: string) => void }) => {
        deleteWaiting ??= onWaiting;
        return new Promise<void>((resolve) => {
          deleted = resolve;
        });
      }),
      getWorkspaceDownloadUrl: vi.fn(() => "#"),
    } as unknown as AgentChatAdapter;
    const selected = vi.fn();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    // Each chat has its own notes.txt selected.
    const panel = (sessionId: string) => (
      <TooltipProvider>
        <WorkspacePanel adapter={adapter} sessionId={sessionId} selectedPath="notes.txt" onSelectedPathChange={selected} />
      </TooltipProvider>
    );
    const dialog = () => document.body.querySelector('[role="dialog"]');
    const button = (name: string) =>
      [...(dialog()?.querySelectorAll("button") ?? [])].find((each) => each.textContent?.trim() === name);
    await act(async () => {
      root?.render(panel("s-1"));
    });
    await act(async () => {
      container!.querySelector<HTMLButtonElement>('button[aria-label="Delete notes.txt"]')?.click();
    });
    await act(async () => {
      button("Delete")?.click();
    });
    expect(button("Delete")?.disabled).toBe(true);
    await act(async () => {
      root?.render(panel("s-2"));
    });
    expect(dialog()).toBeNull();
    // Chat two's own delete is asked, and not held behind chat one's.
    await act(async () => {
      container!.querySelector<HTMLButtonElement>('button[aria-label="Delete notes.txt"]')?.click();
    });
    expect(button("Delete")?.disabled).toBe(false);
    await act(async () => {
      button("Cancel")?.click();
    });
    // Chat one's delete waits for its computer, said over chat one alone.
    act(() => deleteWaiting?.("Waiting for you to allow this on chat one's ThinkPad"));
    expect(container.querySelector('[data-testid="workspace-notice"]')).toBeNull();
    await act(async () => {
      deleted();
    });
    expect(selected).not.toHaveBeenCalledWith(null);
    expect(container.querySelector('[data-testid="workspace-notice"]')).toBeNull();
    // Ended, chat one's dialog is gone with it, and holds nothing on the way back.
    await act(async () => {
      root?.render(panel("s-1"));
    });
    expect(dialog()).toBeNull();
  });

  it("drops a delete the user left unanswered as it leaves the chat, and keeps one under way to its end", async () => {
    let deleted: () => void = () => {};
    const adapter = {
      getWorkspaceTree: vi.fn().mockResolvedValue({
        root: "r",
        entries: [{ name: "notes.txt", path: "notes.txt", kind: "file" }],
        truncated: false,
      }),
      uploadWorkspaceFile: vi.fn(),
      deleteWorkspaceFile: vi.fn(() => new Promise<void>((resolve) => {
        deleted = resolve;
      })),
      getWorkspaceDownloadUrl: vi.fn(() => "#"),
    } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    const panel = (sessionId: string) => (
      <TooltipProvider>
        <WorkspacePanel adapter={adapter} sessionId={sessionId} selectedPath={null} onSelectedPathChange={() => {}} />
      </TooltipProvider>
    );
    const dialog = () => document.body.querySelector('[role="dialog"]');
    const button = (name: string) =>
      [...(dialog()?.querySelectorAll("button") ?? [])].find((each) => each.textContent?.trim() === name);
    const ask = () => act(async () => {
      container!.querySelector<HTMLButtonElement>('button[aria-label="Delete notes.txt"]')?.click();
    });
    // The panel shows *sessionId* again, made anew, as AgentChat unfolds Files on a chat.
    const remount = async (sessionId: string) => {
      act(() => root?.unmount());
      root = createRoot(container!);
      await act(async () => {
        root?.render(panel(sessionId));
      });
    };
    await act(async () => {
      root?.render(panel("s-1"));
    });
    // Asked, and left open as the panel shows another chat, and this one again.
    await ask();
    expect(dialog()).not.toBeNull();
    await act(async () => {
      root?.render(panel("s-2"));
    });
    await act(async () => {
      root?.render(panel("s-1"));
    });
    expect(dialog()).toBeNull();
    // Asked, and left open as the panel folds away with its chat, unfolds on another, and on this one again.
    await ask();
    expect(dialog()).not.toBeNull();
    await remount("s-2");
    await remount("s-1");
    expect(dialog()).toBeNull();
    // Under way, it is shown again, locked, until it ends.
    await ask();
    await act(async () => {
      button("Delete")?.click();
    });
    await remount("s-2");
    await remount("s-1");
    expect(button("Delete")?.disabled).toBe(true);
    await act(async () => {
      deleted();
    });
    expect(dialog()).toBeNull();
  });

  it("says what a change waits for on the computer while it is sent again", async () => {
    const adapter = {
      getWorkspaceTree: vi.fn().mockResolvedValue({ root: "r", entries: [], truncated: false }),
      uploadWorkspaceFile: vi.fn(({ onWaiting }: { onWaiting?: (said: string) => void }) => {
        onWaiting?.("Waiting for you to allow this on Flavius's ThinkPad");
        return new Promise(() => {});
      }),
      deleteWorkspaceFile: vi.fn(),
    } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <TooltipProvider>
          <WorkspacePanel adapter={adapter} sessionId="s-1" selectedPath={null} onSelectedPathChange={() => {}} />
        </TooltipProvider>,
      );
    });
    const input = container.querySelector("input[type=file]") as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [new File(["draft"], "notes.txt")] });
    await act(async () => {
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(container.querySelector('[data-testid="workspace-notice"]')?.textContent)
      .toBe("Waiting for you to allow this on Flavius's ThinkPad");
  });

  it("says when the tree shows only some of the files", async () => {
    const adapter = {
      getWorkspaceTree: vi.fn().mockResolvedValue({
        root: "r",
        entries: [{ name: "a.txt", path: "a.txt", kind: "file" }],
        truncated: true,
      }),
      uploadWorkspaceFile: vi.fn(),
      deleteWorkspaceFile: vi.fn(),
      getWorkspaceDownloadUrl: vi.fn(() => "#"),
    } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <TooltipProvider>
          <WorkspacePanel adapter={adapter} sessionId="s-1" selectedPath={null} onSelectedPathChange={() => {}} />
        </TooltipProvider>,
      );
    });
    expect(container.textContent).toContain("Some files are not shown.");
  });
});
