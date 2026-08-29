import { describe, expect, it } from "vitest";
import {
  applyCommands,
  emptyDoc,
  mapSelected,
  scaleObject,
  translateObject,
} from "@/components/whiteboard/doc";
import type { WbObject } from "@/components/whiteboard/doc";

const text = {
  tool: "write_text", x: 100, y: 200, text: "hi",
  fontSize: 32, maxWidth: 300,
};

function ink(): WbObject {
  return {
    id: "i1", origin: "local", selected: true, kind: "ink",
    pts: [0, 0, 10, 20], width: 4, color: "#111",
  } as WbObject;
}

describe("translateObject", () => {
  it("shifts every ink point", () => {
    const moved = translateObject(ink(), 5, -3) as { pts: number[] };
    expect(moved.pts).toEqual([5, -3, 15, 17]);
  });

  it("shifts a text object's position without touching its size", () => {
    const [obj] = applyCommands(emptyDoc(), [text], 1).objects;
    const moved = translateObject(obj, 10, 10);
    expect(moved).toMatchObject({ x: 110, y: 210, maxWidth: 300 });
  });

  it("moves a draw object by its origin alone", () => {
    // Item coordinates are offsets, so they must not shift too or the
    // primitive moves twice as far as the cursor.
    const [obj] = applyCommands(emptyDoc(), [{
      tool: "draw", origin: [100, 100],
      types: ["rect"], items: [[0, 0, 50, 50]],
    }], 1).objects;
    const moved = translateObject(obj, 10, 20) as {
      origin_: number[]; items: number[][];
    };
    expect(moved.origin_).toEqual([110, 120]);
    expect(moved.items).toEqual([[0, 0, 50, 50]]);
  });

  it("moves an erase path", () => {
    const erase = {
      id: "e1", origin: "local", selected: true, kind: "erase",
      mode: "path", points: [[0, 0], [10, 10]], size: 20,
    } as unknown as WbObject;
    expect((translateObject(erase, 5, 5) as { points: number[][] }).points)
      .toEqual([[5, 5], [15, 15]]);
  });

  it("moves into negative space", () => {
    const moved = translateObject(ink(), -100, -100) as { pts: number[] };
    expect(moved.pts[0]).toBe(-100);
  });

  it("does not mutate the original", () => {
    const original = ink();
    translateObject(original, 5, 5);
    expect((original as { pts: number[] }).pts).toEqual([0, 0, 10, 20]);
  });
});

describe("scaleObject", () => {
  const anchor = { x: 0, y: 0 };

  it("scales ink geometry and stroke width together", () => {
    const scaled = scaleObject(ink(), 2, 2, anchor) as {
      pts: number[]; width: number;
    };
    expect(scaled.pts).toEqual([0, 0, 20, 40]);
    expect(scaled.width).toBe(8);
  });

  it("scales about the anchor, not the origin", () => {
    const scaled = scaleObject(ink(), 2, 2, { x: 10, y: 20 }) as {
      pts: number[];
    };
    // The anchor point itself must not move.
    expect(scaled.pts[2]).toBe(10);
    expect(scaled.pts[3]).toBe(20);
  });

  it("takes the wrap width from the horizontal factor", () => {
    const [obj] = applyCommands(emptyDoc(), [text], 1).objects;
    const scaled = scaleObject(obj, 2, 1, anchor) as { maxWidth: number };
    expect(scaled.maxWidth).toBe(600);
  });

  it("takes the text's font from the vertical factor", () => {
    const [obj] = applyCommands(emptyDoc(), [text], 1).objects;
    // A horizontal pull reflows; it does not stretch the glyphs.
    expect((scaleObject(obj, 4, 1, anchor) as { fontSize: number }).fontSize)
      .toBe(32);
    // A vertical pull is what makes the type bigger.
    expect((scaleObject(obj, 1, 2, anchor) as { fontSize: number }).fontSize)
      .toBe(64);
  });

  it("resizes a formula along whichever axis moved most", () => {
    const [obj] = applyCommands(emptyDoc(), [{
      tool: "draw_formula", x: 0, y: 0, latex: "x^2", fontSize: 40,
    }], 1).objects;
    const size = (sx: number, sy: number) =>
      (scaleObject(obj, sx, sy, anchor) as { fontSize: number }).fontSize;
    // A one-axis pull used to be min(sx, sy) = 1: nothing happened.
    expect(size(2, 1)).toBe(80);
    expect(size(1, 2)).toBe(80);
    expect(size(0.5, 1.1)).toBe(20);
  });

  it("resizes an artifact box on both axes", () => {
    const [obj] = applyCommands(emptyDoc(), [{
      tool: "place_artifact", artifact_id: "a1", x: 0, y: 0, w: 100, h: 50,
    }], 1).objects;
    expect(scaleObject(obj, 2, 3, anchor)).toMatchObject({ w: 200, h: 150 });
  });

  it("keeps a minimum size so an object cannot be scaled to nothing", () => {
    const [obj] = applyCommands(emptyDoc(), [{
      tool: "place_artifact", artifact_id: "a1", x: 0, y: 0, w: 100, h: 50,
    }], 1).objects;
    const tiny = scaleObject(obj, 0.0001, 0.0001, anchor) as {
      w: number; h: number;
    };
    expect(tiny.w).toBeGreaterThan(0);
    expect(tiny.h).toBeGreaterThan(0);
  });

  it("moves a formula by the same factor it grows by", () => {
    const [obj] = applyCommands(emptyDoc(), [{
      tool: "draw_formula", x: 100, y: 100, latex: "x^2", fontSize: 40,
    }], 1).objects;
    // Dragging the NW handle: the SE corner is the anchor and must not
    // move. Scaled 1.5x with the position taking sy=1, the box grew
    // past the anchor and the selection rectangle breathed with it.
    const scaled = scaleObject(obj, 1.5, 1, { x: 200, y: 150 }) as {
      x: number; y: number; fontSize: number;
    };
    expect(scaled.fontSize).toBe(60);
    expect(scaled.x).toBe(50);
    expect(scaled.y).toBe(75);
  });

  it("keeps a formula's font readable at any factor", () => {
    const [obj] = applyCommands(emptyDoc(), [{
      tool: "draw_formula", x: 0, y: 0, latex: "x^2", fontSize: 40,
    }], 1).objects;
    expect((scaleObject(obj, 0.0001, 0.0001, anchor) as { fontSize: number })
      .fontSize).toBeGreaterThanOrEqual(4);
  });
});

describe("mapSelected", () => {
  it("edits only the selected objects", () => {
    let doc = applyCommands(emptyDoc(), [text], 1);
    doc = applyCommands(doc, [{ ...text, x: 500 }], 2);
    // applyCommands leaves only the newest call selected.
    const moved = mapSelected(doc, (o) => translateObject(o, 10, 0));
    expect((moved.objects[0] as { x: number }).x).toBe(100);
    expect((moved.objects[1] as { x: number }).x).toBe(510);
  });

  it("is a no-op when nothing is selected", () => {
    const doc = applyCommands(emptyDoc(), [text], 1);
    doc.objects[0].selected = false;
    const moved = mapSelected(doc, (o) => translateObject(o, 99, 99));
    expect((moved.objects[0] as { x: number }).x).toBe(100);
  });
});
