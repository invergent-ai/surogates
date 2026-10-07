/**
 * WorkspacePanel — a change it sent stops waiting once the panel unmounts.
 *
 * A local-folder chat's upload or delete may wait for its user on the
 * computer its folder is on, and the adapter sends it again meanwhile:
 * the panel hands it a signal it aborts on unmount, so nothing goes on
 * sending for a panel nobody sees.
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkspacePanel } from "../src/components/workspace/workspace-panel";
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
});

describe("WorkspacePanel", () => {
  it("aborts an upload still waiting once it unmounts", async () => {
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
    expect(signals[0]?.aborted).toBe(false);
    act(() => root?.unmount());
    root = null;
    expect(signals[0]?.aborted).toBe(true);
  });
});
