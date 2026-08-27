import { describe, expect, it, vi } from "vitest";
import { applyCommands, emptyDoc } from "@/components/whiteboard/doc";
import {
  CANVAS_DIR,
  CANVAS_PATH,
  loadDoc,
  saveDoc,
} from "@/components/whiteboard/persist";
import type { AgentChatAdapter, AgentChatMessage } from "@/types";

const text = {
  tool: "write_text", x: 1, y: 2, text: "a", fontSize: 20, maxWidth: 100,
};

function adapterWith(file: unknown, upload = vi.fn()) {
  return {
    getWorkspaceFile: vi.fn(async () => {
      if (file === null) throw new Error("404");
      return {
        path: CANVAS_PATH,
        content: typeof file === "string" ? file : JSON.stringify(file),
        size: 1,
        encoding: "utf-8" as const,
        truncated: false,
      };
    }),
    uploadWorkspaceFile: upload,
  } as unknown as AgentChatAdapter;
}

function drawMessage(id: string, commands: unknown[]): AgentChatMessage {
  return {
    id, role: "assistant", content: "",
    toolCalls: [{
      id, toolName: "whiteboard_draw",
      args: JSON.stringify({ commands }), status: "complete",
    }],
  } as unknown as AgentChatMessage;
}

describe("loading", () => {
  it("returns an empty document when no file exists", async () => {
    expect((await loadDoc(adapterWith(null), "s1", [])).objects).toEqual([]);
  });

  it("returns an empty document when the file is corrupt", async () => {
    expect((await loadDoc(adapterWith("{not json"), "s1", [])).objects)
      .toEqual([]);
  });

  it("restores a saved document", async () => {
    const saved = applyCommands(emptyDoc(), [text], 5);
    expect((await loadDoc(adapterWith(saved), "s1", [])).objects)
      .toHaveLength(1);
  });

  it("replays draw calls newer than the saved document", async () => {
    // The recovery tail: a tab closed between an agent reply and the
    // next debounced save must not lose the agent's objects.
    const saved = applyCommands(emptyDoc(), [text], 5);
    const doc = await loadDoc(adapterWith(saved), "s1", [
      drawMessage("newer", [text, text]),
    ]);
    expect(doc.objects).toHaveLength(3);
  });

  it("does not replay a call already folded into the file", async () => {
    const saved = applyCommands(emptyDoc(), [text], 5, "already");
    const doc = await loadDoc(adapterWith(saved), "s1", [
      drawMessage("already", [text]),
    ]);
    expect(doc.objects).toHaveLength(1);
  });

  it("rejects a document with an unknown version", async () => {
    const doc = await loadDoc(
      adapterWith({ version: 99, objects: [{}], lastEventId: 0 }), "s1", [],
    );
    expect(doc.objects).toEqual([]);
  });

  it("rejects a document whose objects are not an array", async () => {
    const doc = await loadDoc(
      adapterWith({ version: 1, objects: "nope", lastEventId: 0 }), "s1", [],
    );
    expect(doc.objects).toEqual([]);
  });

  it("decodes a base64-encoded file", async () => {
    const saved = applyCommands(emptyDoc(), [text], 5);
    const adapter = {
      getWorkspaceFile: vi.fn(async () => ({
        path: CANVAS_PATH,
        content: btoa(JSON.stringify(saved)),
        size: 1,
        encoding: "base64" as const,
        truncated: false,
      })),
      uploadWorkspaceFile: vi.fn(),
    } as unknown as AgentChatAdapter;
    expect((await loadDoc(adapter, "s1", [])).objects).toHaveLength(1);
  });

  it("still replays the event tail when the file is missing", async () => {
    const doc = await loadDoc(adapterWith(null), "s1", [
      drawMessage("m1", [text]),
    ]);
    expect(doc.objects).toHaveLength(1);
  });
});

describe("saving", () => {
  it("uploads into the internal canvas directory", async () => {
    const upload = vi.fn();
    await saveDoc(adapterWith(null, upload), "s1", emptyDoc());
    expect(upload).toHaveBeenCalledTimes(1);
    expect(upload.mock.calls[0][0].directory).toBe(CANVAS_DIR);
  });

  it("writes an underscore-prefixed path", () => {
    // The prefix keeps it out of the workspace file browser, so nobody
    // deletes their own board by tidying up.
    expect(CANVAS_PATH.startsWith("_")).toBe(true);
  });

  it("serialises a round-trippable document", async () => {
    const upload = vi.fn();
    const doc = applyCommands(emptyDoc(), [text], 3);
    await saveDoc(adapterWith(null, upload), "s1", doc);
    const file = upload.mock.calls[0][0].file as File;
    expect(JSON.parse(await file.text()).objects).toHaveLength(1);
  });

  it("preserves lastEventId across a round-trip", async () => {
    const upload = vi.fn();
    await saveDoc(
      adapterWith(null, upload), "s1", applyCommands(emptyDoc(), [text], 42),
    );
    const file = upload.mock.calls[0][0].file as File;
    expect(JSON.parse(await file.text()).lastEventId).toBe(42);
  });

  it("swallows an upload failure", async () => {
    // Persistence is best-effort: the event log is the recovery tail, so
    // a failed save must never surface in front of someone drawing.
    const upload = vi.fn(async () => {
      throw new Error("offline");
    });
    await expect(saveDoc(adapterWith(null, upload), "s1", emptyDoc()))
      .resolves.toBeUndefined();
  });
});
