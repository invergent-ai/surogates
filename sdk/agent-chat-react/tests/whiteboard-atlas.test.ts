import { describe, expect, it } from "vitest";
import {
  HOTSPOT_GRID,
  MAX_ATLAS_HEIGHT,
  MAX_ATLAS_WIDTH,
  atlasMetadata,
  contentBounds,
  mapHotspots,
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
    const latest = { x: -8000, y: -6000, w: 400, h: 300 };
    const { sourceRect } = planAtlas(
      emptyDoc(), latest, view, viewport, services,
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

describe("hotspot grid", () => {
  const rect = { x: 0, y: 0, w: 800, h: 800 };

  it("maps a point to its grid cell", () => {
    expect(mapHotspots(rect, [{ x: 50, y: 50 }])).toEqual([[0, 0]]);
  });

  it("maps the far corner to the last cell", () => {
    expect(mapHotspots(rect, [{ x: 799, y: 799 }]))
      .toEqual([[HOTSPOT_GRID - 1, HOTSPOT_GRID - 1]]);
  });

  it("clamps a point exactly on the far edge into the last cell", () => {
    // 800/800*8 == 8, one past the last index; without the clamp this
    // emits a cell that does not exist.
    expect(mapHotspots(rect, [{ x: 800, y: 800 }]))
      .toEqual([[HOTSPOT_GRID - 1, HOTSPOT_GRID - 1]]);
  });

  it("drops points outside the rectangle", () => {
    expect(mapHotspots(rect, [{ x: -5, y: 10 }, { x: 900, y: 10 }]))
      .toEqual([]);
  });

  it("preserves order oldest to newest", () => {
    const cells = mapHotspots(rect, [
      { x: 50, y: 50 }, { x: 750, y: 750 }, { x: 400, y: 50 },
    ]);
    expect(cells[0]).toEqual([0, 0]);
    expect(cells[1]).toEqual([7, 7]);
  });

  it("collapses consecutive duplicates", () => {
    expect(mapHotspots(rect, [
      { x: 10, y: 10 }, { x: 12, y: 12 }, { x: 750, y: 750 },
    ])).toHaveLength(2);
  });

  it("keeps a revisited cell that is not consecutive", () => {
    // The trajectory matters: returning to a cell is real information.
    expect(mapHotspots(rect, [
      { x: 10, y: 10 }, { x: 750, y: 750 }, { x: 10, y: 10 },
    ])).toHaveLength(3);
  });

  it("returns an empty array for no points", () => {
    expect(mapHotspots(rect, [])).toEqual([]);
  });

  it("tolerates a zero-area rectangle", () => {
    expect(() => mapHotspots({ x: 0, y: 0, w: 0, h: 0 }, [{ x: 0, y: 0 }]))
      .not.toThrow();
  });
});

describe("atlas metadata", () => {
  const plan = planAtlas(
    emptyDoc(), { x: 0, y: 0, w: 100, h: 100 }, view, viewport, services,
  );

  it("carries the geometry the prompt reads", () => {
    const meta = atlasMetadata(plan, { x: 0, y: 0, w: 100, h: 100 }, [], {});
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
    const meta = atlasMetadata(plan, null, [], {});
    expect(meta.viewport).toEqual({ x: 0, y: 0, w: 800, h: 600 });
  });

  it("shapes rectangles as x/y/w/h", () => {
    // The Python note builder reads exactly these keys and renders a
    // shorter note if any is missing.
    const meta = atlasMetadata(plan, { x: 1, y: 2, w: 3, h: 4 }, [], {});
    expect(meta.latestInput).toEqual({ x: 1, y: 2, w: 3, h: 4 });
    expect(Object.keys(meta.sourceRect as object).sort())
      .toEqual(["h", "w", "x", "y"]);
  });

  it("defaults mode to sketch", () => {
    expect(atlasMetadata(plan, null, [], {}).mode).toBe("sketch");
  });

  it("carries an explicit deep mode", () => {
    expect(atlasMetadata(plan, null, [], { mode: "deep" }).mode).toBe("deep");
  });

  it("omits an empty hotspot list", () => {
    expect(atlasMetadata(plan, null, [], {})).not.toHaveProperty("hotspots");
  });

  it("includes a non-empty hotspot list", () => {
    expect(atlasMetadata(plan, null, [[0, 0]], {}).hotspots).toEqual([[0, 0]]);
  });

  it("omits latestInput when there is none", () => {
    expect(atlasMetadata(plan, null, [], {}))
      .not.toHaveProperty("latestInput");
  });

  it("carries an optional selection and typed input", () => {
    const meta = atlasMetadata(plan, null, [], {
      selection: { x: 5, y: 6, w: 7, h: 8 },
      typedInput: "integral of x^2",
    });
    expect(meta.selection).toEqual({ x: 5, y: 6, w: 7, h: 8 });
    expect(meta.typedInput).toBe("integral of x^2");
  });

  it("omits selection and typedInput when absent", () => {
    const meta = atlasMetadata(plan, null, [], {});
    expect(meta).not.toHaveProperty("selection");
    expect(meta).not.toHaveProperty("typedInput");
  });

  it("stays under the server's 64KB metadata cap", () => {
    // A full grid is 64 cells; the server rejects the whole turn above
    // 65536 bytes, so the worst realistic payload must clear it easily.
    const many = Array.from({ length: HOTSPOT_GRID * HOTSPOT_GRID }, (_, i) => [
      i % HOTSPOT_GRID,
      Math.floor(i / HOTSPOT_GRID),
    ]);
    const size = new TextEncoder().encode(
      JSON.stringify(atlasMetadata(plan, null, many, {
        typedInput: "x".repeat(2000),
      })),
    ).length;
    expect(size).toBeLessThan(65_536);
  });
});
