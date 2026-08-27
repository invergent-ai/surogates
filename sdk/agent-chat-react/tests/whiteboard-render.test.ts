import { describe, expect, it } from "vitest";
import { applyCommands, emptyDoc } from "@/components/whiteboard/doc";
import type { WbDoc, WbObject } from "@/components/whiteboard/doc";
import {
  hitTest,
  objectBounds,
  renderDoc,
} from "@/components/whiteboard/render";

/** happy-dom has no 2D context; record the calls instead. */
function recordingContext() {
  const calls: unknown[][] = [];
  return new Proxy({ calls } as Record<string, unknown>, {
    get(target, property) {
      if (property in target) return target[property as string];
      if (property === "measureText") {
        return (t: string) => ({ width: t.length * 8 });
      }
      return (...args: unknown[]) => calls.push([property, ...args]);
    },
    set(target, property, value) {
      target[property as string] = value;
      return true;
    },
  }) as unknown as CanvasRenderingContext2D & { calls: unknown[][] };
}

const measure = {
  formula: (latex: string, fontSize: number) => ({
    w: latex.length * fontSize * 0.5,
    h: fontSize * 1.6,
  }),
  formulaImage: () => null,
  createCanvas: (w: number, h: number) =>
    ({
      width: w,
      height: h,
      getContext: () => recordingContext(),
    }) as unknown as HTMLCanvasElement,
};

const view = { x: 0, y: 0, zoom: 1 };
const size = { w: 800, h: 600 };

const text = {
  tool: "write_text",
  x: 10,
  y: 20,
  text: "hello",
  fontSize: 32,
  maxWidth: 300,
};

function ink(overrides: Partial<WbObject> = {}): WbObject {
  return {
    id: "i1",
    origin: "local",
    selected: false,
    kind: "ink",
    pts: [0, 0, 10, 10, 20, 5],
    width: 4,
    color: "#111",
    ...overrides,
  } as WbObject;
}

function paint(doc: WbDoc, v = view) {
  const ctx = recordingContext();
  renderDoc(ctx, doc, v, size, measure);
  return ctx;
}

function called(ctx: { calls: unknown[][] }, name: string) {
  return ctx.calls.some((c) => c[0] === name);
}

describe("renderDoc", () => {
  it("clears before painting", () => {
    expect(called(paint(emptyDoc()), "clearRect")).toBe(true);
  });

  it("paints nothing else for an empty document", () => {
    expect(called(paint(emptyDoc()), "fillText")).toBe(false);
  });

  it("applies the view transform", () => {
    expect(called(paint(emptyDoc(), { x: 100, y: 50, zoom: 2 }), "setTransform"))
      .toBe(true);
  });

  it("paints a text object", () => {
    expect(called(paint(applyCommands(emptyDoc(), [text], 1)), "fillText"))
      .toBe(true);
  });

  it("wraps text at maxWidth", () => {
    // measureText returns 8px per char, so a 300px wrap width fits ~37
    // characters; 200 characters must therefore produce several lines.
    const long = { ...text, text: "a ".repeat(100) };
    const ctx = paint(applyCommands(emptyDoc(), [long], 1));
    const lines = ctx.calls.filter((c) => c[0] === "fillText");
    expect(lines.length).toBeGreaterThan(1);
  });

  it("strokes an ink object", () => {
    const doc = emptyDoc();
    doc.objects.push(ink());
    expect(called(paint(doc), "stroke")).toBe(true);
  });

  it("skips an ink object with fewer than two points", () => {
    const doc = emptyDoc();
    doc.objects.push(ink({ pts: [5, 5] } as Partial<WbObject>));
    expect(called(paint(doc), "stroke")).toBe(false);
  });

  it("draws a placeholder frame for an artifact", () => {
    // A canvas cannot host an iframe, so the real artifact renders as a
    // DOM overlay; the canvas carries a frame so the atlas shows the
    // model that something occupies that space.
    const doc = applyCommands(emptyDoc(), [{
      tool: "place_artifact", artifact_id: "a1", x: 0, y: 0, w: 200, h: 100,
    }], 1);
    expect(called(paint(doc), "strokeRect")).toBe(true);
  });

  it("composites an erase object out of what is beneath it", () => {
    const doc = emptyDoc();
    doc.objects.push(ink());
    doc.objects.push({
      id: "e1", origin: "local", selected: false, kind: "erase",
      mode: "rect", x: 0, y: 0, w: 10, h: 10,
    } as WbObject);
    const ctx = paint(doc);
    expect(ctx.calls.some((c) => c[0] === "save")).toBe(true);
    expect(ctx.calls.some((c) => c[0] === "restore")).toBe(true);
  });

  it("paints selection chrome for a selected object", () => {
    expect(called(paint(applyCommands(emptyDoc(), [text], 1)), "strokeRect"))
      .toBe(true);
  });

  it("paints no selection chrome when nothing is selected", () => {
    const doc = applyCommands(emptyDoc(), [text], 1);
    doc.objects[0].selected = false;
    expect(called(paint(doc), "strokeRect")).toBe(false);
  });

  it("paints objects in array order", () => {
    const doc = applyCommands(emptyDoc(), [
      { ...text, text: "first" },
      { ...text, text: "second" },
    ], 1);
    const ctx = paint(doc);
    const texts = ctx.calls
      .filter((c) => c[0] === "fillText")
      .map((c) => c[1]);
    expect(texts.indexOf("first")).toBeLessThan(texts.indexOf("second"));
  });
});

describe("objectBounds", () => {
  it("computes bounds for a text object", () => {
    const [obj] = applyCommands(emptyDoc(), [text], 1).objects;
    const bounds = objectBounds(obj, measure);
    expect(bounds).not.toBeNull();
    expect(bounds!.w).toBe(300);
  });

  it("computes bounds for an ink object from its points", () => {
    const bounds = objectBounds(
      ink({ pts: [0, 0, 100, 40] } as Partial<WbObject>),
      measure,
    );
    expect(bounds!.w).toBeGreaterThanOrEqual(100);
    expect(bounds!.h).toBeGreaterThanOrEqual(40);
  });

  it("includes stroke width in ink bounds", () => {
    const thin = objectBounds(
      ink({ pts: [0, 0, 100, 0], width: 2 } as Partial<WbObject>),
      measure,
    );
    const thick = objectBounds(
      ink({ pts: [0, 0, 100, 0], width: 40 } as Partial<WbObject>),
      measure,
    );
    expect(thick!.h).toBeGreaterThan(thin!.h);
  });

  it("computes bounds for a draw object from the draw normalizer", () => {
    const [obj] = applyCommands(emptyDoc(), [{
      tool: "draw", origin: [100, 100],
      types: ["rect"], items: [[0, 0, 50, 50]],
    }], 1).objects;
    const bounds = objectBounds(obj, measure);
    expect(bounds).not.toBeNull();
    expect(bounds!.w).toBeGreaterThan(0);
  });

  it("computes bounds for an artifact from its declared box", () => {
    const [obj] = applyCommands(emptyDoc(), [{
      tool: "place_artifact", artifact_id: "a1", x: 5, y: 6, w: 200, h: 100,
    }], 1).objects;
    expect(objectBounds(obj, measure)).toEqual({ x: 5, y: 6, w: 200, h: 100 });
  });

  it("returns null bounds for an erase object", () => {
    // Erase is a clipping instruction, not a hittable object.
    expect(objectBounds({
      id: "e1", origin: "local", selected: false, kind: "erase",
      mode: "rect", x: 0, y: 0, w: 10, h: 10,
    } as WbObject, measure)).toBeNull();
  });
});

describe("hitTest", () => {
  it("hits a point inside an object", () => {
    const doc = applyCommands(emptyDoc(), [text], 1);
    expect(hitTest(doc, { x: 20, y: 30 }, measure)).not.toBeNull();
  });

  it("returns null for a point over empty canvas", () => {
    const doc = applyCommands(emptyDoc(), [text], 1);
    expect(hitTest(doc, { x: 9000, y: 9000 }, measure)).toBeNull();
  });

  it("hits the topmost object first", () => {
    let doc = applyCommands(emptyDoc(), [text], 1);
    doc = applyCommands(doc, [{ ...text, text: "second" }], 2);
    expect(hitTest(doc, { x: 20, y: 30 }, measure)!.id)
      .toBe(doc.objects[1].id);
  });

  it("never hits an erase object", () => {
    const doc = emptyDoc();
    doc.objects.push({
      id: "e1", origin: "local", selected: false, kind: "erase",
      mode: "rect", x: 0, y: 0, w: 100, h: 100,
    } as WbObject);
    expect(hitTest(doc, { x: 50, y: 50 }, measure)).toBeNull();
  });

  it("returns null on an empty document", () => {
    expect(hitTest(emptyDoc(), { x: 0, y: 0 }, measure)).toBeNull();
  });
});
