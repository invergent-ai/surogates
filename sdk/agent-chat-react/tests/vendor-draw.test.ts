import { describe, expect, it } from "vitest";
import { DRAW } from "@/vendor/penecho";

/**
 * Records every 2D-context call. Ported from PenEcho's own
 * `test/draw.test.js:6` — happy-dom has no real canvas 2D context, so the
 * renderer is exercised against a recorder instead.
 */
function fakeCanvas(width: number, height: number) {
  const calls: unknown[][] = [];
  const context = new Proxy({ calls } as Record<string, unknown>, {
    get(target, property) {
      if (property in target) return target[property as string];
      return (...args: unknown[]) => calls.push([property, ...args]);
    },
    set(target, property, value) {
      target[property as string] = value;
      return true;
    },
  });
  return {
    width,
    height,
    calls,
    getContext: () => context,
  } as unknown as HTMLCanvasElement & { calls: unknown[][] };
}

const rect = {
  origin: [100, 100],
  types: ["rect"],
  items: [[0, 0, 200, 120]],
};

describe("vendored draw.js — validation", () => {
  it("normalizes a valid rect command", () => {
    const result = DRAW.normalize(rect);
    expect(result).not.toBeNull();
    expect(result!._draw.bounds.w).toBeGreaterThan(0);
  });

  it("rejects mismatched types and items", () => {
    expect(DRAW.normalize({ ...rect, types: ["rect", "circle"] })).toBeNull();
  });

  it("rejects an unknown primitive type", () => {
    expect(DRAW.normalize({ ...rect, types: ["squiggle"] })).toBeNull();
  });

  it("rejects a width outside 2..200", () => {
    expect(DRAW.normalize({ ...rect, width: 500 })).toBeNull();
    expect(DRAW.normalize({ ...rect, width: 1 })).toBeNull();
  });

  it("rejects an origin outside the canvas", () => {
    expect(DRAW.normalize({ ...rect, origin: [999999, 0] })).toBeNull();
  });

  it("rejects a non-object command", () => {
    expect(DRAW.normalize(null)).toBeNull();
    expect(DRAW.normalize("nope")).toBeNull();
  });

  it("accepts every documented primitive type", () => {
    const cmd = {
      origin: [0, 0],
      types: ["line", "smooth", "rect", "ellipse", "circle", "arc"],
      items: [
        [0, 0, 10, 10],
        [0, 0, 5, 5, 10, 0],
        [0, 0, 20, 20],
        [10, 10, 5, 8],
        [10, 10, 6],
        [10, 10, 5, 5, 0, 90],
      ],
    };
    expect(DRAW.normalize(cmd)).not.toBeNull();
  });
});

describe("vendored draw.js — bounds", () => {
  it("includes stroke padding in the bounds", () => {
    const bounds = DRAW.normalize({ ...rect, width: 30 })!._draw.bounds;
    expect(bounds.w).toBeGreaterThan(200);
    expect(bounds.h).toBeGreaterThan(120);
  });

  it("accounts for arc extrema rather than just endpoints", () => {
    // A 0..180 sweep bulges past the chord between its endpoints; taking
    // endpoints alone would clip the rendered arc.
    const arc = {
      origin: [0, 0],
      types: ["arc"],
      items: [[100, 100, 50, 50, 0, 180]],
    };
    const bounds = DRAW.normalize(arc)!._draw.bounds;
    expect(bounds.h).toBeGreaterThan(50);
  });
});

describe("vendored draw.js — rendering", () => {
  it("renders a rect and returns its logical origin", () => {
    const rendered = DRAW.render(rect, (w, h) => fakeCanvas(w, h));
    expect(rendered).not.toBeNull();
    expect(rendered!.image.width).toBeGreaterThan(0);
    expect(typeof rendered!.x).toBe("number");
    expect(typeof rendered!.y).toBe("number");
  });

  it("returns null from render for an invalid command", () => {
    expect(DRAW.render({ types: [] }, (w, h) => fakeCanvas(w, h))).toBeNull();
  });

  it("actually strokes onto the context", () => {
    const rendered = DRAW.render(rect, (w, h) => fakeCanvas(w, h));
    const calls = (rendered!.image as unknown as { calls: unknown[][] }).calls;
    expect(calls.some((c) => c[0] === "stroke")).toBe(true);
  });
});

describe("vendored draw.js — smoothing", () => {
  const points = [
    { x: 0, y: 0 },
    { x: 10, y: 10 },
    { x: 20, y: 0 },
  ];

  it("produces one segment per gap on an open path", () => {
    expect(DRAW.smoothSegments(points, false, 50)).toHaveLength(2);
  });

  it("closes the loop on a closed path", () => {
    expect(DRAW.smoothSegments(points, true, 50)).toHaveLength(3);
  });

  it("returns nothing for fewer than three points", () => {
    expect(DRAW.smoothSegments(points.slice(0, 2), false, 50)).toHaveLength(0);
  });
});
