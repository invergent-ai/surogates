import { describe, expect, it } from "vitest";
import {
  MAX_ATLAS_HEIGHT,
  MAX_ATLAS_WIDTH,
  atlasMetadata,
  buildAtlas,
  AGENT_OBJECT_LIMIT,
  MAX_CROPS,
  MAX_MARKS,
  OCCUPANCY_GRID,
  agentObjectReport,
  type BoardMark,
  type Rect,
  boardMarks,
  buildRegionCrop,
  cropRegions,
  inkClusters,
  contentBeyond,
  contentBounds,
  inkHeight,
  planAtlas,
} from "@/components/whiteboard/atlas";
import { applyCommands, emptyDoc } from "@/components/whiteboard/doc";

const viewport = { w: 800, h: 600 };
const view = { x: 0, y: 0, zoom: 1 };

const text = {
  tool: "write_text", x: 1000, y: 1000, text: "hello",
  fontSize: 32, maxWidth: 300,
};

const services = {
  formula: (latex: string, fontSize: number) => ({
    w: latex.length * fontSize * 0.5,
    h: fontSize * 1.6,
  }),
  formulaImage: () => null,
  createCanvas: (w: number, h: number) =>
    ({ width: w, height: h, getContext: () => null }) as unknown as
      HTMLCanvasElement,
};

describe("content bounds", () => {
  it("is null for an empty document", () => {
    expect(contentBounds(emptyDoc(), services)).toBeNull();
  });

  it("covers a single object", () => {
    const b = contentBounds(applyCommands(emptyDoc(), [text], 1), services);
    expect(b!.x).toBeLessThanOrEqual(1000);
  });

  it("unions several objects", () => {
    let doc = applyCommands(emptyDoc(), [text], 1);
    doc = applyCommands(doc, [{ ...text, x: 5000, y: 5000 }], 2);
    const b = contentBounds(doc, services)!;
    expect(b.x + b.w).toBeGreaterThanOrEqual(5000);
  });
});

describe("atlas planning", () => {
  it("never exceeds the image caps", () => {
    const plan = planAtlas(
      emptyDoc(), { x: 0, y: 0, w: 19000, h: 18000 }, view, viewport, services,
    );
    expect(plan.imageSize.w).toBeLessThanOrEqual(MAX_ATLAS_WIDTH);
    expect(plan.imageSize.h).toBeLessThanOrEqual(MAX_ATLAS_HEIGHT);
  });

  it("never upscales past 1:1", () => {
    const plan = planAtlas(
      emptyDoc(), { x: 0, y: 0, w: 10, h: 10 }, view, viewport, services,
    );
    expect(plan.imageScale).toBeLessThanOrEqual(1);
  });

  it("covers the latest input rectangle", () => {
    const latest = { x: 1000, y: 1000, w: 200, h: 100 };
    const { sourceRect } = planAtlas(
      emptyDoc(), latest, view, viewport, services,
    );
    expect(sourceRect.x).toBeLessThanOrEqual(latest.x);
    expect(sourceRect.y).toBeLessThanOrEqual(latest.y);
    expect(sourceRect.x + sourceRect.w)
      .toBeGreaterThanOrEqual(latest.x + latest.w);
    expect(sourceRect.y + sourceRect.h)
      .toBeGreaterThanOrEqual(latest.y + latest.h);
  });

  it("covers existing content when there is no latest input", () => {
    const doc = applyCommands(emptyDoc(), [text], 1);
    const { sourceRect } = planAtlas(doc, null, view, viewport, services);
    expect(sourceRect.x).toBeLessThanOrEqual(1000);
  });

  it("falls back to the viewport for an empty canvas", () => {
    const { sourceRect } = planAtlas(
      emptyDoc(), null, view, viewport, services,
    );
    expect(sourceRect.w).toBeGreaterThan(0);
    expect(sourceRect.h).toBeGreaterThan(0);
  });

  it("produces a positive image size for an empty canvas", () => {
    const { imageSize } = planAtlas(
      emptyDoc(), null, view, viewport, services,
    );
    expect(imageSize.w).toBeGreaterThan(0);
    expect(imageSize.h).toBeGreaterThan(0);
  });

  it("captures negative regions without clamping them away", () => {
    // The canvas has no edges: a board drawn up and to the left of the
    // origin is ordinary, and clamping to 0 would crop it out entirely.
    // Framed from the viewport, so the user is looking at it.
    const far = { x: -8000, y: -6000, zoom: 1 };
    const latest = { x: -7900, y: -5900, w: 400, h: 300 };
    const { sourceRect } = planAtlas(
      emptyDoc(), latest, far, viewport, services,
    );
    expect(sourceRect.x).toBeLessThanOrEqual(latest.x);
    expect(sourceRect.y).toBeLessThanOrEqual(latest.y);
    expect(sourceRect.x + sourceRect.w)
      .toBeGreaterThanOrEqual(latest.x + latest.w);
  });

  it("pads a tiny latest input so the model gets surrounding context", () => {
    const tiny = { x: 5000, y: 5000, w: 4, h: 4 };
    const { sourceRect } = planAtlas(
      emptyDoc(), tiny, view, viewport, services,
    );
    expect(sourceRect.w).toBeGreaterThan(tiny.w * 4);
  });
});


describe("atlas metadata", () => {
  const plan = planAtlas(
    emptyDoc(), { x: 0, y: 0, w: 100, h: 100 }, view, viewport, services,
  );

  it("carries the geometry the prompt reads", () => {
    const meta = atlasMetadata(plan, { x: 0, y: 0, w: 100, h: 100 }, {});
    expect(meta).toHaveProperty("sourceRect");
    expect(meta).toHaveProperty("imageScale");
    expect(meta).toHaveProperty("latestInput");
    expect(meta).toHaveProperty("infinite");
    expect(meta).toHaveProperty("viewport");
  });

  it("reports the user's viewport, not the image size", () => {
    // These differ whenever the capture is scaled, and labelling the
    // image size as the viewport would tell the model the user is
    // looking at a region they are not.
    const meta = atlasMetadata(plan, null, {});
    expect(meta.viewport).toEqual({ x: 0, y: 0, w: 800, h: 600 });
  });

  it("shapes rectangles as x/y/w/h", () => {
    // The Python note builder reads exactly these keys and renders a
    // shorter note if any is missing.
    const meta = atlasMetadata(plan, { x: 1, y: 2, w: 3, h: 4 }, {});
    expect(meta.latestInput).toEqual({ x: 1, y: 2, w: 3, h: 4 });
    expect(Object.keys(meta.sourceRect as object).sort())
      .toEqual(["h", "w", "x", "y"]);
  });

  it("defaults mode to sketch", () => {
    expect(atlasMetadata(plan, null, {}).mode).toBe("sketch");
  });

  it("carries an explicit deep mode", () => {
    expect(atlasMetadata(plan, null, { mode: "deep" }).mode).toBe("deep");
  });

  it("omits latestInput when there is none", () => {
    expect(atlasMetadata(plan, null, {}))
      .not.toHaveProperty("latestInput");
  });

  it("carries an optional selection and typed input", () => {
    const meta = atlasMetadata(plan, null, {
      selection: { x: 5, y: 6, w: 7, h: 8 },
      typedInput: "integral of x^2",
    });
    expect(meta.selection).toEqual({ x: 5, y: 6, w: 7, h: 8 });
    expect(meta.typedInput).toBe("integral of x^2");
  });

  it("omits selection and typedInput when absent", () => {
    const meta = atlasMetadata(plan, null, {});
    expect(meta).not.toHaveProperty("selection");
    expect(meta).not.toHaveProperty("typedInput");
  });

  it("stays under the server's 64KB metadata cap", () => {
    const size = new TextEncoder().encode(
      JSON.stringify(atlasMetadata(plan, null, {
        typedInput: "x".repeat(2000),
      })),
    ).length;
    expect(size).toBeLessThan(65_536);
  });
});

describe("keeping the board in shot", () => {
  // From a real session: a finished sum, then `=` added to the right of
  // it. The capture framed on the new stroke alone, so the model got two
  // bare horizontal lines and answered "menu / list icon?".
  const sum = {
    tool: "write_text", x: 0, y: 0, text: "1 + 2",
    fontSize: 32, maxWidth: 300,
  };

  function covers(rect: { x: number; y: number; w: number; h: number },
                  inner: { x: number; y: number; w: number; h: number }) {
    return (
      rect.x <= inner.x &&
      rect.y <= inner.y &&
      rect.x + rect.w >= inner.x + inner.w &&
      rect.y + rect.h >= inner.y + inner.h
    );
  }

  it("keeps earlier content in frame when new ink lands far away", () => {
    const doc = applyCommands(emptyDoc(), [sum], 1);
    const latest = { x: 1200, y: 40, w: 16, h: 33 };
    const { sourceRect } = planAtlas(doc, latest, view, viewport, services);

    expect(covers(sourceRect, latest)).toBe(true);
    // The sum itself must still be in the picture the model answers.
    expect(covers(sourceRect, { x: 0, y: 0, w: 1, h: 1 })).toBe(true);
  });

  it("still covers a dirty region where content was erased", () => {
    // Rubbing something out leaves a dirty rectangle with nothing in it,
    // and the model should see where. The user erases what they are
    // looking at, so it sits inside the viewport.
    const doc = applyCommands(emptyDoc(), [sum], 1);
    const erased = { x: 40, y: 30, w: 20, h: 20 };
    const { sourceRect } = planAtlas(doc, erased, view, viewport, services);

    expect(covers(sourceRect, erased)).toBe(true);
  });

  it("still falls back to the viewport on an empty canvas", () => {
    const { sourceRect } = planAtlas(
      emptyDoc(), null, view, viewport, services,
    );
    expect(sourceRect.w).toBeGreaterThan(0);
    expect(sourceRect.h).toBeGreaterThan(0);
  });
});

describe("telling the model how big the writing is", () => {
  // Without it the model picks a font size blind: on a board of ~250-unit
  // digits it chose 90, and the answer came out a quarter the size.
  function inked(heights: number[]) {
    return {
      ...emptyDoc(),
      objects: heights.map((h, i) => ({
        id: `i${i}`, origin: "local", selected: false,
        kind: "ink" as const,
        pts: [0, 0, 10, h],
        // Zero width so the bounds are exactly the point extent --
        // inkBounds pads by the stroke width otherwise.
        width: 0, color: "#000",
      })),
    };
  }

  it("is null for a board with no ink", () => {
    expect(inkHeight(emptyDoc(), services)).toBeNull();
    expect(inkHeight(applyCommands(emptyDoc(), [text], 1), services))
      .toBeNull();
  });

  it("ignores the flat marks that carry no height", () => {
    // The bars of an `=` are ~4 units tall; averaging them in drags the
    // figure toward zero and the model writes too small again.
    expect(inkHeight(inked([252, 4, 116, 198, 1, 4]), services)).toBe(198);
  });

  it("resists a single outsized stroke", () => {
    // A max would follow a stray divider; the median does not.
    expect(inkHeight(inked([200, 210, 220, 2000]), services)).toBe(215);
  });

  it("rides on the turn metadata, rounded", () => {
    const plan = planAtlas(emptyDoc(), null, view, viewport, services);
    const meta = atlasMetadata(plan, null, { inkHeight: 251.7 });
    expect(meta.inkHeight).toBe(252);
  });

  it("is omitted when there is no ink to measure", () => {
    const plan = planAtlas(emptyDoc(), null, view, viewport, services);
    expect(atlasMetadata(plan, null, { inkHeight: null }))
      .not.toHaveProperty("inkHeight");
  });
});

describe("a bounded view of an unbounded board", () => {
  const wide = { w: 1600, h: 900 };
  const at = (x: number, y: number) => ({ x, y, zoom: 1 });

  it("stops growing instead of shrinking the board to fit", () => {
    // A 6800-unit board arrived at imageScale 0.30, where the model's own
    // 55-unit answers render 16px tall and it places new work on top of
    // old by squinting. The capture stops; the board does not.
    const zoomedOut = { x: 0, y: 0, zoom: 0.05 };
    const { sourceRect, imageScale } = planAtlas(
      emptyDoc(), null, zoomedOut, wide, services,
    );
    expect(sourceRect.w).toBeLessThanOrEqual(MAX_ATLAS_WIDTH);
    expect(sourceRect.h).toBeLessThanOrEqual(MAX_ATLAS_HEIGHT);
    // Capped span means no downscale: the picture is always 1:1.
    expect(imageScale).toBe(1);
  });

  it("frames what the user is looking at", () => {
    const { sourceRect } = planAtlas(
      emptyDoc(), null, at(5000, 4000), wide, services,
    );
    expect(sourceRect.x).toBeLessThanOrEqual(5000);
    expect(sourceRect.x + sourceRect.w).toBeGreaterThanOrEqual(5000 + wide.w);
  });

  it("says which way the board continues past the frame", () => {
    // Content too big to show whole, so the frame is a window onto it.
    const doc = applyCommands(emptyDoc(), [
      { tool: "write_text", x: 0, y: 0, text: "here",
        fontSize: 32, maxWidth: 100 },
      { tool: "write_text", x: 9000, y: 9000, text: "far",
        fontSize: 32, maxWidth: 100 },
    ], 1);
    const { sourceRect } = planAtlas(doc, null, at(0, 0), wide, services);
    expect(contentBeyond(doc, sourceRect, services)).toEqual(["right", "below"]);
  });

  it("says nothing when the frame holds everything", () => {
    const doc = applyCommands(emptyDoc(), [
      { tool: "write_text", x: 10, y: 10, text: "near",
        fontSize: 32, maxWidth: 100 },
    ], 1);
    const { sourceRect } = planAtlas(doc, null, at(0, 0), wide, services);
    expect(contentBeyond(doc, sourceRect, services)).toEqual([]);
  });
});


describe("the grid drawn on the picture", () => {
  /** A canvas that records what was drawn into it. */
  function recording() {
    const calls: unknown[][] = [];
    const ctx = new Proxy({} as Record<string, unknown>, {
      get: (_t, p) => {
        if (p === "calls") return calls;
        return (...args: unknown[]) => {
          calls.push([p, ...args]);
        };
      },
      set: (t, p, v) => {
        calls.push(["set", p, v]);
        t[p as string] = v;
        return true;
      },
    });
    return {
      calls,
      services: {
        ...services,
        createCanvas: (w: number, h: number) =>
          ({ width: w, height: h, getContext: () => ctx }) as unknown as
            HTMLCanvasElement,
      },
    };
  }

  it("draws the cell lines and the edge labels", () => {
    // Reading `[col, row]` pairs means cross-referencing a list against a
    // picture; drawn on, the model can see which cells are free.
    const { calls, services: rec } = recording();
    const plan = planAtlas(emptyDoc(), null, view, viewport, services);
    buildAtlas(emptyDoc(), plan, rec);

    const labels = calls.filter((c) => c[0] === "fillText").map((c) => c[1]);
    expect(labels).toContain("0");
    expect(labels).toContain(String(OCCUPANCY_GRID - 1));
    expect(calls.some((c) => c[0] === "stroke")).toBe(true);
  });

  it("keeps the overlay off the board's own coordinate space", () => {
    // renderDoc leaves a board-space transform behind. The grid belongs
    // to the picture, so it resets first -- otherwise the lines land
    // wherever the board happens to be panned to.
    const { calls, services: rec } = recording();
    const plan = planAtlas(emptyDoc(), null, view, viewport, services);
    buildAtlas(emptyDoc(), plan, rec);

    const firstLabel = calls.findIndex((c) => c[0] === "fillText");
    const identity = calls.findIndex(
      (c, i) =>
        i < firstLabel && c[0] === "setTransform" &&
        c[1] === 1 && c[2] === 0 && c[3] === 0 && c[4] === 1 &&
        c[5] === 0 && c[6] === 0,
    );
    expect(identity).toBeGreaterThan(-1);
  });
});

describe("never cropping an expression in half", () => {
  const wide = { w: 1600, h: 900 };

  it("shows the whole board when it fits, wherever the user scrolled", () => {
    // The regression this replaces: working at the right-hand end of an
    // integral, the capture began mid-`e^x` with the integral sign off
    // the left edge, and the model answered the fragment it could see.
    const doc = applyCommands(emptyDoc(), [
      { tool: "write_text", x: -150, y: 0, text: "integral",
        fontSize: 40, maxWidth: 200 },
      { tool: "write_text", x: 700, y: 0, text: "tail",
        fontSize: 40, maxWidth: 100 },
    ], 1);
    // Scrolled right, so the left end is off screen.
    const scrolled = { x: 214, y: -329, zoom: 1 };
    const { sourceRect } = planAtlas(doc, null, scrolled, wide, services);

    expect(sourceRect.x).toBeLessThanOrEqual(-150);
    // "tail" is four glyphs at 40: ~96 units of ink, not its 100 maxWidth.
    expect(sourceRect.x + sourceRect.w).toBeGreaterThanOrEqual(700 + 96);
  });

  it("falls back to the viewport once the board outgrows the frame", () => {
    // Something has to be left out; the least bad thing to keep is what
    // the user is looking at.
    const doc = applyCommands(emptyDoc(), [
      { tool: "write_text", x: 0, y: 0, text: "a", fontSize: 40, maxWidth: 100 },
      { tool: "write_text", x: 9000, y: 0, text: "b", fontSize: 40, maxWidth: 100 },
    ], 1);
    const scrolled = { x: 8500, y: 0, zoom: 1 };
    const { sourceRect } = planAtlas(doc, null, scrolled, wide, services);

    expect(sourceRect.w).toBeLessThanOrEqual(MAX_ATLAS_WIDTH);
    expect(sourceRect.x).toBeGreaterThan(1000);
    expect(contentBeyond(doc, sourceRect, services)).toContain("left");
  });
});


describe("what became of the agent's own work", () => {
  const drawn = (origin: string, x: number, y: number, text: string) => ({
    id: `${origin}:1`, origin, selected: false, kind: "text" as const,
    x, y, text, fontSize: 40, maxWidth: 200, lineHeight: 1.35,
  });
  const ink = (id: string, x: number, y: number, h = 60) => ({
    id, origin: "local", selected: false, kind: "ink" as const,
    pts: [x, y, x + 10, y + h], width: 0, color: "#000",
  });
  const board = (objects: unknown[], folded: string[]) =>
    ({ ...emptyDoc(), folded, objects }) as never;

  it("reports where an object sits now", () => {
    const doc = board([drawn("callA", 480, 390, "e^x + C")], ["callA"]);
    const [entry] = agentObjectReport(doc, services);
    expect(entry.origin).toBe("callA");
    expect(entry.label).toBe("e^x + C");
    expect(entry.bounds?.x).toBe(480);
  });

  it("reports a deleted object as gone", () => {
    // The call is folded but nothing survives from it.
    const [entry] = agentObjectReport(board([], ["callA"]), services);
    expect(entry.bounds).toBeNull();
  });

  it("flags ink the user drew around it", () => {
    // The real case: an answer wrapped in hand-drawn brackets and
    // squared. The object never moved; what it means changed.
    const doc = board(
      [drawn("callA", 480, 390, "e^x + C"), ink("new1", 465, 380)],
      ["callA"],
    );
    const [entry] = agentObjectReport(doc, services, {
      newLocalIds: new Set(["new1"]),
    });
    expect(entry.touched).toBe(true);
  });

  it("does not flag ink that was already there", () => {
    const doc = board(
      [drawn("callA", 480, 390, "e^x + C"), ink("old1", 465, 380)],
      ["callA"],
    );
    const [entry] = agentObjectReport(doc, services, {
      newLocalIds: new Set(),
    });
    expect(entry.touched).toBe(false);
  });

  it("does not flag new ink drawn far away", () => {
    const doc = board(
      [drawn("callA", 480, 390, "e^x + C"), ink("new1", 5000, 5000)],
      ["callA"],
    );
    const [entry] = agentObjectReport(doc, services, {
      newLocalIds: new Set(["new1"]),
    });
    expect(entry.touched).toBe(false);
  });

  it("keeps the newest and drops the oldest past the cap", () => {
    // These notes are permanent, so the list has to be bounded.
    const folded = Array.from({ length: 20 }, (_, i) => `call${i}`);
    const doc = board(
      folded.map((o, i) => drawn(o, i * 10, 0, `t${i}`)),
      folded,
    );
    const report = agentObjectReport(doc, services);
    expect(report.length).toBe(AGENT_OBJECT_LIMIT);
    expect(report[0].origin).toBe("call19");
  });

  it("is empty when the agent has drawn nothing", () => {
    expect(agentObjectReport(emptyDoc(), services)).toEqual([]);
  });

  it("rides on the turn metadata", () => {
    const plan = planAtlas(emptyDoc(), null, view, viewport, services);
    const meta = atlasMetadata(plan, null, {
      agentObjects: [
        { origin: "c1", label: "five", bounds: null },
        { origin: "c2", label: "six",
          bounds: { x: 1, y: 2, w: 3, h: 4 }, touched: true },
      ],
    });
    const rows = meta.agentObjects as Record<string, unknown>[];
    expect(rows[0]).toMatchObject({ origin: "c1", removed: true });
    expect(rows[1]).toMatchObject({ origin: "c2", x: 1, touched: true });
  });
});

describe("clustering the user's ink into the things they wrote", () => {
  const stroke = (id: string, x: number, y: number, w: number, h: number) => ({
    id, origin: "local", selected: false, kind: "ink" as const,
    pts: [x, y, x + w, y + h], width: 0, color: "#000",
  });
  const board = (objects: unknown[]) =>
    ({ ...emptyDoc(), objects }) as never;
  const unit = 60;

  it("joins the symbols of one expression", () => {
    // `2x + 1 = 7`: symbols a fraction of a unit apart, one line.
    const doc = board([
      stroke("2", 0, 0, 50, 60), stroke("x", 70, 10, 40, 50),
      stroke("+", 130, 20, 40, 40), stroke("1", 200, 0, 15, 60),
      stroke("=", 250, 25, 50, 15), stroke("7", 340, 0, 45, 60),
    ]);
    const clusters = inkClusters(doc, services, unit);
    expect(clusters).toHaveLength(1);
    expect(clusters[0].strokeIds).toHaveLength(6);
  });

  it("separates two lines", () => {
    // A second line a full line height below is a second thing.
    const doc = board([
      stroke("a", 0, 0, 200, 60),
      stroke("b", 0, 180, 200, 60),
    ]);
    expect(inkClusters(doc, services, unit)).toHaveLength(2);
  });

  it("labels in reading order: top to bottom, then left to right", () => {
    const doc = board([
      stroke("bottom-left", 0, 300, 50, 50),
      stroke("top-right", 500, 0, 50, 50),
      stroke("top-left", 0, 0, 50, 50),
    ]);
    const marks = boardMarks(doc, services, { unit });
    expect(marks.map((m) => m.id)).toEqual(["A1", "A2", "A3"]);
    expect(marks[0].rect?.x).toBe(0);
    expect(marks[0].rect?.y).toBe(0);
    expect(marks[1].rect?.x).toBe(500);
    expect(marks[2].rect?.y).toBe(300);
  });

  it("ignores the agent's objects when clustering ink", () => {
    const doc = applyCommands(board([stroke("a", 0, 0, 50, 50)]), [
      { tool: "write_text", x: 60, y: 0, text: "hi", fontSize: 40, maxWidth: 100 },
    ], 1, "callA");
    expect(inkClusters(doc, services, unit)).toHaveLength(1);
  });

  it("flags the cluster holding ink new since the last Ask", () => {
    const doc = board([
      stroke("old", 0, 0, 50, 50),
      stroke("new", 0, 300, 50, 50),
    ]);
    const marks = boardMarks(doc, services, {
      unit, newLocalIds: new Set(["new"]),
    });
    expect(marks.find((m) => m.rect?.y === 300)?.fresh).toBe(true);
    expect(marks.find((m) => m.rect?.y === 0)?.fresh).toBeUndefined();
  });

  it("labels the agent's objects B1, B2 with their call ids", () => {
    // Built the way agent objects really arrive: the fold records the
    // call in `folded`, which is what the report walks.
    const doc = {
      ...applyCommands(emptyDoc(), [
        { tool: "draw_formula", x: 0, y: 0, latex: "e^x", fontSize: 40 },
      ], 1, "toolu_01A"),
      folded: ["toolu_01A"],
    };
    const marks = boardMarks(doc, services, { unit });
    expect(marks).toEqual([
      expect.objectContaining({ id: "B1", kind: "agent", origin: "toolu_01A", label: "e^x" }),
    ]);
  });

  it("bounds the number of marks, keeping what is new", () => {
    // The note is permanent; a board of hundreds of doodles cannot be
    // allowed to list them all forever.
    const many = Array.from({ length: 40 }, (_, i) =>
      stroke(`s${i}`, 0, i * 200, 50, 50));
    const doc = board(many);
    const marks = boardMarks(doc, services, {
      unit, newLocalIds: new Set(["s39"]),
    });
    expect(marks.length).toBeLessThanOrEqual(MAX_MARKS);
    expect(marks.some((m) => m.fresh)).toBe(true);
  });

  it("rides on the turn metadata", () => {
    const plan = planAtlas(emptyDoc(), null, view, viewport, services);
    const meta = atlasMetadata(plan, null, {
      marks: [
        { id: "A1", kind: "ink", rect: { x: 1, y: 2, w: 3, h: 4 }, fresh: true },
        { id: "B1", kind: "agent", rect: null, origin: "c1", label: "gone" },
      ],
    });
    const rows = meta.marks as Record<string, unknown>[];
    expect(rows[0]).toMatchObject({ id: "A1", x: 1, fresh: true });
    expect(rows[1]).toMatchObject({ id: "B1", removed: true, origin: "c1" });
  });

  it("paints each mark's label onto the atlas", () => {
    const calls: unknown[][] = [];
    const ctx = new Proxy({} as Record<string, unknown>, {
      get: (_t, p) =>
        p === "measureText"
          ? () => ({ width: 20 })
          : (...args: unknown[]) => { calls.push([p, ...args]); },
      set: () => true,
    });
    const rec = {
      ...services,
      createCanvas: (w: number, h: number) =>
        ({ width: w, height: h, getContext: () => ctx }) as unknown as
          HTMLCanvasElement,
    };
    const plan = planAtlas(emptyDoc(), null, view, viewport, services);
    buildAtlas(emptyDoc(), plan, rec, [
      { id: "A1", kind: "ink", rect: { x: 10, y: 10, w: 100, h: 50 } },
    ]);
    const labels = calls.filter((c) => c[0] === "fillText").map((c) => c[1]);
    expect(labels).toContain("A1");
  });
});

describe("close-ups of new ink", () => {
  const stroke = (id: string, x: number, y: number, w: number, h: number) => ({
    id, origin: "local", selected: false, kind: "ink" as const,
    pts: [x, y, x + w, y + h], width: 0, color: "#000",
  });
  const board = (...objects: unknown[]) =>
    ({ ...emptyDoc(), objects }) as never;
  const drawing = () => {
    const calls: unknown[][] = [];
    const ctx = new Proxy({} as Record<string, unknown>, {
      get: (_t, p) =>
        p === "measureText"
          ? () => ({ width: 20 })
          : (...args: unknown[]) => { calls.push([p, ...args]); },
      set: () => true,
    });
    let made: { w: number; h: number } | null = null;
    return {
      calls,
      size: () => made,
      services: {
        ...services,
        createCanvas: (w: number, h: number) => {
          made = { w, h };
          return { width: w, height: h, getContext: () => ctx } as unknown as
            HTMLCanvasElement;
        },
      },
    };
  };
  const ink = (id: string, rect: Rect, over: Partial<BoardMark> = {}): BoardMark =>
    ({ id, kind: "ink", rect, fresh: true, ...over });

  it("renders the region large enough to read", () => {
    // 60-unit handwriting arrives ~110px tall instead of the few dozen
    // pixels it gets on the overview.
    const rec = drawing();
    const crop = buildRegionCrop(
      board(stroke("s", 0, 0, 300, 60)),
      { ids: ["A1"], rect: { x: 0, y: 0, w: 300, h: 60 } },
      rec.services,
      60,
    );
    expect(crop?.scale).toBeGreaterThan(1.5);
    expect(rec.size()?.h).toBeGreaterThan(100);
  });

  it("always magnifies when it is sent at all", () => {
    // Session 661ecc5b: 118px handwriting got a close-up at scale 1 --
    // the overview again, which helped nothing.
    const rec = drawing();
    const crop = buildRegionCrop(
      board(), { ids: ["A1"], rect: { x: 0, y: 0, w: 400, h: 120 } }, rec.services, 118,
    );
    expect(crop?.scale).toBeGreaterThanOrEqual(1.5);
  });

  it("never shrinks below 1:1 for large handwriting that fits", () => {
    const rec = drawing();
    const crop = buildRegionCrop(
      board(), { ids: ["A1"], rect: { x: 0, y: 0, w: 600, h: 250 } }, rec.services, 250,
    );
    expect(crop?.scale).toBeGreaterThanOrEqual(1);
  });

  it("caps the image size for a very wide region", () => {
    const rec = drawing();
    buildRegionCrop(
      board(), { ids: ["A1"], rect: { x: 0, y: 0, w: 4000, h: 60 } }, rec.services, 60,
    );
    expect(rec.size()?.w).toBeLessThanOrEqual(1536);
  });

  it("crops new unread ink together with the agent object it touches", () => {
    // `( ln|x|+C )²`: the `)²` alone reads as a `?`; with the answer
    // inside the brackets it reads as squaring it.
    const marks: BoardMark[] = [
      ink("A2", { x: 896, y: 185, w: 222, h: 217 }),
      { id: "B1", kind: "agent", rect: { x: 617, y: 274, w: 289, h: 70 },
        origin: "c1", label: "ln|x| + C", touched: true },
    ];
    const [region] = cropRegions(marks, 69);
    expect(region.ids).toEqual(["A2", "B1"]);
    expect(region.rect.x).toBeLessThanOrEqual(617);
    expect(region.rect.x + region.rect.w).toBeGreaterThanOrEqual(896 + 222);
  });

  it("keeps far-apart new marks as separate close-ups", () => {
    const marks = [
      ink("A1", { x: 0, y: 0, w: 100, h: 50 }),
      ink("A2", { x: 5000, y: 5000, w: 100, h: 50 }),
    ];
    expect(cropRegions(marks, 40)).toHaveLength(2);
  });

  it("merges new marks that sit close together", () => {
    const marks = [
      ink("A1", { x: 0, y: 0, w: 100, h: 50 }),
      ink("A2", { x: 120, y: 0, w: 100, h: 50 }),
    ];
    const regions = cropRegions(marks, 40);
    expect(regions).toHaveLength(1);
    expect(regions[0].ids).toEqual(["A1", "A2"]);
  });

  it("skips ink that already has a reading, and old ink", () => {
    const marks = [
      ink("A1", { x: 0, y: 0, w: 1, h: 1 }, { reading: "known" }),
      ink("A2", { x: 0, y: 0, w: 1, h: 1 }, { fresh: false }),
      ink("A3", { x: 9000, y: 9000, w: 1, h: 1 }),
    ];
    expect(cropRegions(marks, 40).map((r) => r.ids)).toEqual([["A3"]]);
  });

  it("is bounded per turn", () => {
    const marks = Array.from({ length: 5 }, (_, i) =>
      ink(`A${i + 1}`, { x: i * 5000, y: 0, w: 1, h: 1 }));
    expect(cropRegions(marks, 40)).toHaveLength(MAX_CROPS);
  });

  it("rides on the turn metadata with its image index", () => {
    const plan = planAtlas(emptyDoc(), null, view, viewport, services);
    const meta = atlasMetadata(plan, null, {
      crops: [{ marks: ["A2", "B1"], imageIndex: 1, scale: 1.59 }],
    });
    expect(meta.crops).toEqual([{ marks: ["A2", "B1"], imageIndex: 1, scale: 1.59 }]);
  });
});

describe("slot marks", () => {
  const slotDoc = () =>
    ({
      ...emptyDoc(),
      objects: [{ id: "local:9", origin: "local", selected: false, kind: "slot",
                  x: 140, y: 20, w: 60, h: 70, hint: "a letter" }],
    }) as never;

  it("labels a slot S1 with its hint and object id", () => {
    const marks = boardMarks(slotDoc(), services, { unit: 40 });
    expect(marks).toEqual([
      expect.objectContaining({ id: "S1", kind: "slot", objectId: "local:9", hint: "a letter" }),
    ]);
  });

  it("carries hint and object id in the turn metadata", () => {
    const plan = planAtlas(emptyDoc(), null, view, viewport, services);
    const meta = atlasMetadata(plan, null, {
      marks: boardMarks(slotDoc(), services, { unit: 40 }),
    });
    expect((meta.marks as Record<string, unknown>[])[0]).toMatchObject({
      id: "S1", kind: "slot", hint: "a letter", objectId: "local:9",
    });
  });
});


// ---------------------------------------------------------------------
// The images sent to the model must be opaque
//
// From a real session: the atlas and its close-up both arrived ~99%
// transparent. `renderDoc` opens with a clearRect -- the live canvas
// needs it to wipe the previous frame -- which also wiped the white
// background both builders painted immediately before calling it. How
// the board looked to the model then depended entirely on what its
// provider composited alpha against; against black, dark ink on a dark
// ground is barely legible.
// ---------------------------------------------------------------------

describe("an opaque ground", () => {
  /** The shared stub has no 2D context; record the calls instead. */
  function recordingServices() {
    const ctx = new Proxy({ calls: [] as unknown[][] } as Record<string, unknown>, {
      get(target, property) {
        if (property in target) return target[property as string];
        if (property === "measureText") {
          return (t: string) => ({ width: t.length * 8 });
        }
        return (...args: unknown[]) =>
          (target.calls as unknown[][]).push([property, ...args]);
      },
      set(target, property, value) {
        target[property as string] = value;
        return true;
      },
    }) as unknown as CanvasRenderingContext2D & {
      calls: unknown[][];
      globalCompositeOperation: string;
    };
    return {
      ctx,
      services: {
        ...services,
        createCanvas: (w: number, h: number) =>
          ({ width: w, height: h, getContext: () => ctx }) as unknown as
            HTMLCanvasElement,
      },
    };
  }

  const board = () =>
    applyCommands(emptyDoc(), [
      { tool: "draw_formula", x: 0, y: 0, latex: "x^2", fontSize: 40 },
    ], 1);

  it("backs the overview after painting, not before", () => {
    const { ctx, services: rec } = recordingServices();
    const doc = board();
    buildAtlas(doc, planAtlas(doc, null, view, viewport, rec), rec);
    const calls = ctx.calls.map((c) => String(c[0]));
    // A ground painted before the clear is a ground that is wiped.
    expect(calls.lastIndexOf("clearRect")).toBeGreaterThanOrEqual(0);
    expect(calls.lastIndexOf("fillRect")).toBeGreaterThan(
      calls.lastIndexOf("clearRect"),
    );
  });

  it("puts the ground underneath rather than over the ink", () => {
    const { ctx, services: rec } = recordingServices();
    const doc = board();
    buildAtlas(doc, planAtlas(doc, null, view, viewport, rec), rec);
    expect(ctx.globalCompositeOperation).toBe("destination-over");
  });

  it("backs the close-up too", () => {
    const { ctx, services: rec } = recordingServices();
    const doc = board();
    const marks = boardMarks(doc, rec, { unit: 40, newLocalIds: new Set() });
    const [region] = cropRegions(marks, 40);
    if (region) {
      buildRegionCrop(doc, region, rec, 40);
      const calls = ctx.calls.map((c) => String(c[0]));
      expect(calls.lastIndexOf("fillRect")).toBeGreaterThan(
        calls.lastIndexOf("clearRect"),
      );
    }
  });
});
