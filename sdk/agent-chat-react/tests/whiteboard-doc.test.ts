import { describe, expect, it } from "vitest";
import {
  type WbDoc,
  applyCommands,
  applyReadings,
  correctReading,
  emptyDoc,
  foldToolCalls,
  makeSlotObject,
  readingKey,
  scaleObject,
  translateObject,
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


describe("width/height where the schema says w/h", () => {
  // From a real session: the model wrote a wrong correction, tried twice
  // to erase it with `width`/`height`, and was rejected. The validator
  // now accepts that spelling, so the client has to draw it — otherwise
  // the erase passes validation and rubs out nothing.
  const erase = (extent: Record<string, unknown>) =>
    message("e1", "whiteboard_draw", {
      commands: [{ tool: "erase", mode: "rect", x: 460, y: 300, ...extent }],
    });

  it("applies an erase spelled width/height", () => {
    const doc = foldToolCalls(emptyDoc(), [erase({ width: 460, height: 60 })]);
    const obj = doc.objects[0] as { w: number; h: number };
    expect([obj.w, obj.h]).toEqual([460, 60]);
  });

  it("prefers w/h when both are present", () => {
    const doc = foldToolCalls(emptyDoc(), [
      erase({ w: 10, h: 20, width: 999, height: 999 }),
    ]);
    const obj = doc.objects[0] as { w: number; h: number };
    expect([obj.w, obj.h]).toEqual([10, 20]);
  });

  it("applies a place_artifact spelled width/height", () => {
    const doc = foldToolCalls(emptyDoc(), [
      message("a1", "whiteboard_draw", {
        commands: [{
          tool: "place_artifact", artifact_id: "art", x: 0, y: 0,
          width: 100, height: 50,
        }],
      }),
    ]);
    const obj = doc.objects[0] as { w: number; h: number };
    expect([obj.w, obj.h]).toEqual([100, 50]);
  });
});

describe("superseding an earlier draw", () => {
  const at = (x: number, text: string) => ({
    tool: "write_text", x, y: 0, text, fontSize: 40, maxWidth: 100,
  });

  it("removes the objects the call supersedes", () => {
    // Revising an answer used to mean drawing over the old one: `erase`
    // paints white, it does not delete.
    const first = applyCommands(emptyDoc(), [at(0, "wrong")], 1, "callA");
    const next = applyCommands(
      first, [{ ...at(0, "right"), replaces: "callA" }], 2, "callB",
    );
    expect(next.objects).toHaveLength(1);
    expect((next.objects[0] as { text: string }).text).toBe("right");
  });

  it("leaves everything else alone", () => {
    let doc = applyCommands(emptyDoc(), [at(0, "keep")], 1, "callA");
    doc = applyCommands(doc, [at(50, "drop")], 2, "callB");
    const next = applyCommands(
      doc, [{ ...at(50, "new"), replaces: "callB" }], 3, "callC",
    );
    const texts = next.objects.map((o) => (o as { text: string }).text);
    expect(texts).toEqual(["keep", "new"]);
  });

  it("retracts even when the replacement itself is unusable", () => {
    // The intent to retract is independent of whether the new object
    // could be drawn; leaving the old one behind is the worse outcome.
    const first = applyCommands(emptyDoc(), [at(0, "wrong")], 1, "callA");
    const next = applyCommands(
      first, [{ tool: "nonsense", replaces: "callA" }], 2, "callB",
    );
    expect(next.objects).toHaveLength(0);
  });

  it("ignores a replaces that matches nothing", () => {
    const first = applyCommands(emptyDoc(), [at(0, "keep")], 1, "callA");
    const next = applyCommands(
      first, [{ ...at(50, "add"), replaces: "callZ" }], 2, "callB",
    );
    expect(next.objects).toHaveLength(2);
  });
});

describe("readings persist with the ink they describe", () => {
  const ink = (id: string, x: number) => ({
    id, origin: "local", selected: false, kind: "ink" as const,
    pts: [x, 0, x + 40, 60], width: 0, color: "#000",
  });
  const board = (...ids: string[]) =>
    ({ ...emptyDoc(), objects: ids.map((id, i) => ink(id, i * 50)) }) as WbDoc;
  const turn = (marks: unknown[]) => ({ whiteboard: { marks } });

  it("stores a reading against the mark's strokes", () => {
    const doc = applyReadings(
      board("s1", "s2"),
      [{ mark: "A1", text: "2x + 1 = 7" }],
      turn([{ id: "A1", kind: "ink", strokes: ["s1", "s2"] }]),
    );
    expect(doc.readings?.[readingKey(["s2", "s1"])]).toEqual({
      text: "2x + 1 = 7", source: "agent", strokeIds: ["s1", "s2"],
    });
  });

  it("keys by the exact strokes, so added ink reads as new", () => {
    // The meaning changed when the stroke was added; the old reading
    // must not be handed back as if it still applied.
    const doc = applyReadings(
      board("s1", "s2", "s3"),
      [{ mark: "A1", text: "2x + 1" }],
      turn([{ id: "A1", kind: "ink", strokes: ["s1", "s2"] }]),
    );
    expect(doc.readings?.[readingKey(["s1", "s2", "s3"])]).toBeUndefined();
  });

  it("never overwrites a reading the user corrected", () => {
    let doc = correctReading(board("s1"), ["s1"], "the user's version");
    doc = applyReadings(
      doc,
      [{ mark: "A1", text: "the agent's guess" }],
      turn([{ id: "A1", kind: "ink", strokes: ["s1"] }]),
    );
    expect(doc.readings?.[readingKey(["s1"])]?.text).toBe("the user's version");
    expect(doc.readings?.[readingKey(["s1"])]?.source).toBe("user");
  });

  it("drops readings whose ink is gone", () => {
    let doc = correctReading(board("s1", "s2"), ["s2"], "gone soon");
    doc = { ...doc, objects: doc.objects.filter((o) => o.id !== "s2") };
    doc = applyReadings(doc, [], turn([]));
    expect(doc.readings).toEqual({});
  });

  it("ignores readings for unknown marks and blank text", () => {
    const doc = applyReadings(
      board("s1"),
      [{ mark: "A9", text: "x" }, { mark: "A1", text: "   " }, "junk"],
      turn([{ id: "A1", kind: "ink", strokes: ["s1"] }]),
    );
    expect(doc.readings).toEqual({});
  });

  it("clearing a correction removes the reading", () => {
    let doc = correctReading(board("s1"), ["s1"], "typo");
    doc = correctReading(doc, ["s1"], "");
    expect(doc.readings).toEqual({});
  });

  it("folds readings out of a draw call using the turn's marks", () => {
    const messages = [
      {
        id: "u1", role: "user", content: "", createdAt: new Date(),
        status: "complete",
        metadata: turn([{ id: "A1", kind: "ink", strokes: ["s1"] }]),
      },
      message("c1", "whiteboard_draw", {
        commands: [{ tool: "draw_formula", latex: "3", x: 0, y: 0, fontSize: 40 }],
        readings: [{ mark: "A1", text: "2 + 1 =" }],
      }),
    ] as unknown as AgentChatMessage[];
    const doc = foldToolCalls(board("s1"), messages);
    expect(doc.readings?.[readingKey(["s1"])]?.text).toBe("2 + 1 =");
    expect(doc.objects.some((o) => o.kind === "formula")).toBe(true);
  });
});

describe("a draw the server rejected", () => {
  const call = (result?: string) =>
    ({
      ...message("r1", "whiteboard_draw", { commands: [text] }),
      toolCalls: [{
        id: "r1", toolName: "whiteboard_draw",
        args: JSON.stringify({ commands: [text] }), status: "complete",
        ...(result ? { result } : {}),
      }],
    }) as AgentChatMessage;

  it("is not drawn", () => {
    // Every validator retry used to double-draw: the rejected version
    // and the corrected one.
    const doc = foldToolCalls(emptyDoc(), [call("Error: command[0] wraps")]);
    expect(doc.objects).toHaveLength(0);
    expect(doc.folded).toContain("r1");
  });

  it("is removed again if it was folded before its result arrived", () => {
    // The call streams ahead of its result, so the first fold sees no
    // result and draws; the next fold sees the error and takes it back.
    const drawn = foldToolCalls(emptyDoc(), [call()]);
    expect(drawn.objects).toHaveLength(1);
    const taken = foldToolCalls(drawn, [call("Error: nope")]);
    expect(taken.objects).toHaveLength(0);
  });

  it("leaves an accepted call alone", () => {
    const doc = foldToolCalls(emptyDoc(), [call("Drew 1 object on the canvas")]);
    expect(doc.objects).toHaveLength(1);
  });
});

describe("slots", () => {
  it("moves and scales like any object", () => {
    const slot = makeSlotObject({ x: 10, y: 20, w: 30, h: 40 }, "the cat");
    const moved = translateObject(slot, 5, 5) as { x: number; y: number };
    expect([moved.x, moved.y]).toEqual([15, 25]);
    const scaled = scaleObject(slot, 2, 2, { x: 10, y: 20 }) as { w: number; h: number };
    expect([scaled.w, scaled.h]).toEqual([60, 80]);
    expect((slot as { hint?: string }).hint).toBe("the cat");
  });
});


describe("object ids", () => {
  it("never collide across strokes, slots and text", async () => {
    const { StrokeBuilder } = await import("@/components/whiteboard/input");
    const builder = new StrokeBuilder("#000", 4);
    builder.begin({ x: 0, y: 0 });
    builder.extend({ x: 10, y: 10 });
    builder.extend({ x: 20, y: 20 });
    const stroke = builder.finish();
    const slot = makeSlotObject({ x: 0, y: 0, w: 10, h: 10 });
    const ids = new Set([stroke?.id, slot.id]);
    expect(ids.size).toBe(2);
    expect(slot.id.startsWith("local:")).toBe(true);
  });
});
