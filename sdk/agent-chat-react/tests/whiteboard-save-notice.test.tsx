/**
 * useDebouncedSave — a save that fails is said, and the next save retries it.
 *
 * A local-folder chat's canvas save can be refused while its computer is
 * away, or while the chat's changes wait their turn there. The board then
 * says its last change was not saved, and its next save writes the whole
 * document again.
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { applyCommands, emptyDoc, type WbDoc } from "../src/components/whiteboard/doc";
import { useDebouncedSave } from "../src/components/whiteboard/persist";
import type { AgentChatAdapter } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const text = { tool: "write_text", x: 1, y: 2, text: "a", fontSize: 20, maxWidth: 100 };

function Board({ adapter, doc }: { adapter: AgentChatAdapter; doc: WbDoc }) {
  const unsaved = useDebouncedSave(adapter, "s1", doc, 0);
  return <span>{unsaved ? "unsaved" : "saved"}</span>;
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

describe("useDebouncedSave", () => {
  it("says the last change was not saved, until a later save lands", async () => {
    const upload = vi.fn()
      .mockRejectedValueOnce(new Error("The files are on Flavius's ThinkPad, which is offline"))
      .mockResolvedValue({ path: "_whiteboard/canvas.json", size: 1 });
    const adapter = { uploadWorkspaceFile: upload } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    const settle = () => act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    await act(async () => {
      root?.render(<Board adapter={adapter} doc={applyCommands(emptyDoc(), [text], 1)} />);
    });
    await settle();
    expect(container.textContent).toBe("unsaved");
    await act(async () => {
      root?.render(<Board adapter={adapter} doc={applyCommands(emptyDoc(), [text, text], 2)} />);
    });
    await settle();
    expect(upload).toHaveBeenCalledTimes(2);
    expect(container.textContent).toBe("saved");
  });
});
