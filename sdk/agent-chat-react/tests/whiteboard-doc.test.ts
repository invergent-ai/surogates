import { describe, expect, it } from "vitest";
import {
  applyCommands,
  emptyDoc,
  foldToolCalls,
} from "@/components/whiteboard/doc";
import type { AgentChatMessage } from "@/types";

const text = {
  tool: "write_text",
  x: 10,
  y: 20,
  text: "5",
  fontSize: 32,
  maxWidth: 300,
};

function message(
  id: string,
  toolName: string,
  args: unknown,
): AgentChatMessage {
  return {
    id,
    role: "assistant",
    content: "",
    toolCalls: [
      { id, toolName, args: JSON.stringify(args), status: "complete" },
    ],
  } as unknown as AgentChatMessage;
}

describe("canvas document", () => {
  it("starts empty", () => {
    const doc = emptyDoc();
    expect(doc.objects).toEqual([]);
    expect(doc.lastEventId).toBe(0);
  });

  it("appends one object per command", () => {
    const doc = applyCommands(emptyDoc(), [text, text], 7);
    expect(doc.objects).toHaveLength(2);
    expect(doc.lastEventId).toBe(7);
  });

  it("gives every object a distinct id", () => {
    const doc = applyCommands(emptyDoc(), [text, text], 1);
    expect(doc.objects[0].id).not.toBe(doc.objects[1].id);
  });

  it("maps write_text onto a text object", () => {
    const [obj] = applyCommands(emptyDoc(), [text], 1).objects;
    expect(obj.kind).toBe("text");
    expect(obj).toMatchObject({ x: 10, y: 20, text: "5", maxWidth: 300 });
  });

  it("defaults lineHeight when the model omits it", () => {
    const [obj] = applyCommands(emptyDoc(), [text], 1).objects;
    expect((obj as { lineHeight: number }).lineHeight).toBe(1.35);
  });

  it("keeps an explicit lineHeight", () => {
    const [obj] = applyCommands(
      emptyDoc(),
      [{ ...text, lineHeight: 1.8 }],
      1,
    ).objects;
    expect((obj as { lineHeight: number }).lineHeight).toBe(1.8);
  });

  it("maps draw_formula onto a formula object", () => {
    const [obj] = applyCommands(emptyDoc(), [{
      tool: "draw_formula", x: 1, y: 2, latex: "x^2", fontSize: 40,
    }], 1).objects;
    expect(obj.kind).toBe("formula");
  });

  it("maps place_artifact onto an artifact object", () => {
    const [obj] = applyCommands(emptyDoc(), [{
      tool: "place_artifact", artifact_id: "a1", x: 0, y: 0, w: 100, h: 80,
    }], 1).objects;
    expect(obj).toMatchObject({ kind: "artifact", artifactId: "a1" });
  });

  it("maps a valid draw command onto a draw object", () => {
    const [obj] = applyCommands(emptyDoc(), [{
      tool: "draw", origin: [100, 100],
      types: ["rect"], items: [[0, 0, 50, 50]],
    }], 1).objects;
    expect(obj.kind).toBe("draw");
  });

  it("skips a command the draw validator rejects", () => {
    const doc = applyCommands(emptyDoc(), [
      text,
      {
        tool: "draw", origin: [0, 0],
        types: ["rect", "circle"], items: [[0, 0, 1, 1]],
      },
    ], 1);
    expect(doc.objects).toHaveLength(1);
  });

  it("skips an unknown command tool", () => {
    expect(
      applyCommands(emptyDoc(), [{ tool: "nope" }], 1).objects,
    ).toHaveLength(0);
  });

  it("skips place_artifact without an artifact id", () => {
    expect(
      applyCommands(emptyDoc(), [{
        tool: "place_artifact", x: 0, y: 0, w: 1, h: 1,
      }], 1).objects,
    ).toHaveLength(0);
  });

  it("still advances lastEventId when every command was skipped", () => {
    // Otherwise the persistence tail would replay the same dead call
    // on every load.
    const doc = applyCommands(emptyDoc(), [{ tool: "nope" }], 9);
    expect(doc.lastEventId).toBe(9);
  });

  it("marks objects from one call as the active selection", () => {
    const doc = applyCommands(emptyDoc(), [text, text], 1);
    expect(doc.objects.every((o) => o.selected)).toBe(true);
  });

  it("clears the previous selection when a new call lands", () => {
    const first = applyCommands(emptyDoc(), [text], 1);
    const second = applyCommands(first, [text], 2);
    expect(second.objects[0].selected).toBe(false);
    expect(second.objects[1].selected).toBe(true);
  });

  it("does not mutate the document it was given", () => {
    const first = applyCommands(emptyDoc(), [text], 1);
    applyCommands(first, [text], 2);
    expect(first.objects).toHaveLength(1);
    expect(first.objects[0].selected).toBe(true);
  });

  it("tolerates a non-array commands payload", () => {
    expect(applyCommands(emptyDoc(), "nope" as never, 1).objects).toEqual([]);
  });
});

describe("folding tool calls", () => {
  it("folds whiteboard_draw calls out of the message list", () => {
    const doc = foldToolCalls(emptyDoc(), [
      message("m1", "whiteboard_draw", { commands: [text] }),
      message("m2", "web_search", { query: "x" }),
      message("m3", "whiteboard_draw", { commands: [text, text] }),
    ]);
    expect(doc.objects).toHaveLength(3);
  });

  it("ignores other tools", () => {
    const doc = foldToolCalls(emptyDoc(), [
      message("m1", "web_search", { query: "x" }),
    ]);
    expect(doc.objects).toHaveLength(0);
  });

  it("ignores a tool call whose args are not valid JSON", () => {
    const broken = {
      id: "m1",
      role: "assistant",
      content: "",
      toolCalls: [
        { id: "m1", toolName: "whiteboard_draw", args: "{", status: "complete" },
      ],
    } as unknown as AgentChatMessage;
    expect(foldToolCalls(emptyDoc(), [broken]).objects).toHaveLength(0);
  });

  it("ignores a call with no commands array", () => {
    const doc = foldToolCalls(emptyDoc(), [
      message("m1", "whiteboard_draw", { notCommands: 1 }),
    ]);
    expect(doc.objects).toHaveLength(0);
  });

  it("ignores a message with no tool calls", () => {
    const plain = {
      id: "m1", role: "assistant", content: "hello",
    } as unknown as AgentChatMessage;
    expect(foldToolCalls(emptyDoc(), [plain]).objects).toHaveLength(0);
  });

  it("is idempotent when folded twice over the same messages", () => {
    // The SSE stream and the reconciliation poll both deliver the same
    // events, so a re-fold must not duplicate objects.
    const messages = [message("m1", "whiteboard_draw", { commands: [text] })];
    const once = foldToolCalls(emptyDoc(), messages);
    const twice = foldToolCalls(once, messages);
    expect(twice.objects).toHaveLength(1);
  });

  it("appends only the new call when the list grows", () => {
    const first = [message("m1", "whiteboard_draw", { commands: [text] })];
    const grown = [
      ...first,
      message("m2", "whiteboard_draw", { commands: [text] }),
    ];
    const doc = foldToolCalls(foldToolCalls(emptyDoc(), first), grown);
    expect(doc.objects).toHaveLength(2);
  });

  it("leaves only the newest call's objects selected", () => {
    const doc = foldToolCalls(emptyDoc(), [
      message("m1", "whiteboard_draw", { commands: [text] }),
      message("m2", "whiteboard_draw", { commands: [text] }),
    ]);
    expect(doc.objects[0].selected).toBe(false);
    expect(doc.objects[1].selected).toBe(true);
  });
});

describe("deleting an agent's object", () => {
  const call = message("m1", "whiteboard_draw", { commands: [text] });

  it("does not resurrect it on the next fold", () => {
    // The seen-set used to be derived from surviving object origins, so
    // deleting an object erased the only record that its tool call had
    // been consumed — and the next load drew it again.
    const folded = foldToolCalls(emptyDoc(), [call]);
    expect(folded.objects).toHaveLength(1);

    const afterDelete = { ...folded, objects: [] };
    expect(foldToolCalls(afterDelete, [call]).objects).toHaveLength(0);
  });

  it("records the consumed call on the document", () => {
    // Persisted, or the record dies with the page and the object comes
    // back on refresh.
    expect(foldToolCalls(emptyDoc(), [call]).folded).toContain("m1");
  });

  it("still applies a genuinely new call after a deletion", () => {
    const folded = foldToolCalls(emptyDoc(), [call]);
    const afterDelete = { ...folded, objects: [] };
    const next = foldToolCalls(afterDelete, [
      call,
      message("m2", "whiteboard_draw", { commands: [text] }),
    ]);
    expect(next.objects).toHaveLength(1);
    expect(next.folded).toEqual(["m1", "m2"]);
  });

  it("keeps the record for a call whose commands were all invalid", () => {
    // Otherwise the dead call is retried on every single load.
    const bad = message("m9", "whiteboard_draw", { commands: [{ tool: "no" }] });
    expect(foldToolCalls(emptyDoc(), [bad]).folded).toContain("m9");
  });
});
