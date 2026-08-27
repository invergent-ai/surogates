import { describe, expect, it } from "vitest";
import {
  MAX_ZOOM,
  MIN_ZOOM,
  StrokeBuilder,
  logicalToScreen,
  panBy,
  screenToLogical,
  strokePointsFromEvent,
  zoomAt,
  zoomFactorFromWheel,
  zoomToFit,
} from "@/components/whiteboard/input";

describe("coordinate mapping", () => {
  it("round-trips a point through screen and back", () => {
    const view = { x: 500, y: 300, zoom: 1.5 };
    const logical = { x: 1234, y: 5678 };
    const back = screenToLogical(logicalToScreen(logical, view), view);
    expect(back.x).toBeCloseTo(logical.x, 6);
    expect(back.y).toBeCloseTo(logical.y, 6);
  });

  it("is identity at origin and zoom 1", () => {
    const view = { x: 0, y: 0, zoom: 1 };
    expect(screenToLogical({ x: 10, y: 20 }, view)).toEqual({ x: 10, y: 20 });
  });

  it("offsets by the view origin", () => {
    const view = { x: 100, y: 200, zoom: 1 };
    expect(screenToLogical({ x: 0, y: 0 }, view)).toEqual({ x: 100, y: 200 });
  });

  it("divides by zoom when converting to logical", () => {
    const view = { x: 0, y: 0, zoom: 2 };
    expect(screenToLogical({ x: 100, y: 100 }, view)).toEqual({ x: 50, y: 50 });
  });
});

describe("zoom", () => {
  it("keeps the anchor point stationary", () => {
    const view = { x: 100, y: 100, zoom: 1 };
    const anchor = { x: 400, y: 300 };
    const before = screenToLogical(anchor, view);
    const after = screenToLogical(anchor, zoomAt(view, anchor, 2));
    expect(after.x).toBeCloseTo(before.x, 4);
    expect(after.y).toBeCloseTo(before.y, 4);
  });

  it("keeps the anchor stationary when zooming out too", () => {
    const view = { x: 800, y: 600, zoom: 3 };
    const anchor = { x: 120, y: 90 };
    const before = screenToLogical(anchor, view);
    const after = screenToLogical(anchor, zoomAt(view, anchor, 0.5));
    expect(after.x).toBeCloseTo(before.x, 4);
    expect(after.y).toBeCloseTo(before.y, 4);
  });

  it("multiplies the zoom factor", () => {
    expect(zoomAt({ x: 0, y: 0, zoom: 1 }, { x: 0, y: 0 }, 2).zoom)
      .toBeCloseTo(2);
  });

  it("clamps zoom to a sane range", () => {
    const far = zoomAt({ x: 0, y: 0, zoom: 1 }, { x: 0, y: 0 }, 1000);
    expect(far.zoom).toBeLessThanOrEqual(MAX_ZOOM);
    const near = zoomAt({ x: 0, y: 0, zoom: 1 }, { x: 0, y: 0 }, 0.0001);
    expect(near.zoom).toBeGreaterThanOrEqual(MIN_ZOOM);
  });

  it("keeps the anchor stationary even when the factor is clamped", () => {
    // The clamp must feed back into the origin solve, or a zoom that hits
    // the limit drifts the board under the cursor.
    const view = { x: 100, y: 100, zoom: MAX_ZOOM };
    const anchor = { x: 400, y: 300 };
    const before = screenToLogical(anchor, view);
    const after = screenToLogical(anchor, zoomAt(view, anchor, 4));
    expect(after.x).toBeCloseTo(before.x, 4);
    expect(after.y).toBeCloseTo(before.y, 4);
  });
});

describe("pan", () => {
  it("moves the view opposite to the drag", () => {
    // Dragging the board right must reveal content to its left.
    const panned = panBy({ x: 500, y: 500, zoom: 1 }, { x: 50, y: 20 });
    expect(panned.x).toBe(450);
    expect(panned.y).toBe(480);
  });

  it("scales the delta by zoom", () => {
    const panned = panBy({ x: 500, y: 500, zoom: 2 }, { x: 50, y: 0 });
    expect(panned.x).toBe(475);
  });
});

describe("the canvas has no edges", () => {
  it("pans arbitrarily far into negative space", () => {
    // Figma-style: there is no origin corner and nothing to bump into.
    let view = { x: 0, y: 0, zoom: 1 };
    for (let i = 0; i < 100; i++) {
      view = panBy(view, { x: 500, y: 500 });
    }
    expect(view.x).toBeLessThan(-40_000);
    expect(view.y).toBeLessThan(-40_000);
  });

  it("pans arbitrarily far into positive space", () => {
    let view = { x: 0, y: 0, zoom: 1 };
    for (let i = 0; i < 100; i++) {
      view = panBy(view, { x: -500, y: -500 });
    }
    expect(view.x).toBeGreaterThan(40_000);
  });

  it("keeps a stroke drawn at negative coordinates intact", () => {
    const b = new StrokeBuilder("#111", 4);
    b.begin({ x: -5000, y: -9000 });
    b.extend({ x: -4990, y: -8990 });
    expect((b.finish() as { pts: number[] }).pts)
      .toEqual([-5000, -9000, -4990, -8990]);
  });

  it("drops a non-finite point rather than poisoning the bounds", () => {
    const b = new StrokeBuilder("#111", 4);
    b.begin({ x: 0, y: 0 });
    b.extend({ x: Number.NaN, y: 5 });
    b.extend({ x: Number.POSITIVE_INFINITY, y: 5 });
    b.extend({ x: 10, y: 5 });
    expect((b.finish() as { pts: number[] }).pts).toEqual([0, 0, 10, 5]);
  });
});

describe("wheel zoom", () => {
  it("zooms in on a negative delta", () => {
    expect(zoomFactorFromWheel(-100)).toBeGreaterThan(1);
  });

  it("zooms out on a positive delta", () => {
    expect(zoomFactorFromWheel(100)).toBeLessThan(1);
  });

  it("is a no-op at zero", () => {
    expect(zoomFactorFromWheel(0)).toBeCloseTo(1, 6);
  });

  it("is symmetric, so a scroll and its reverse cancel", () => {
    expect(zoomFactorFromWheel(120) * zoomFactorFromWheel(-120))
      .toBeCloseTo(1, 6);
  });

  it("scales smoothly rather than in fixed steps", () => {
    // A trackpad emits many small deltas; a fixed 1.1x per event would
    // make it wildly over-sensitive next to a notched wheel.
    const small = zoomFactorFromWheel(-4);
    const large = zoomFactorFromWheel(-120);
    expect(small).toBeLessThan(large);
    expect(small).toBeGreaterThan(1);
  });
});

describe("zoom to fit", () => {
  const size = { w: 800, h: 600 };

  it("frames the content", () => {
    const bounds = { x: 1000, y: 1000, w: 400, h: 300 };
    const view = zoomToFit(bounds, size);
    const centre = screenToLogical({ x: size.w / 2, y: size.h / 2 }, view);
    expect(centre.x).toBeCloseTo(bounds.x + bounds.w / 2, 4);
    expect(centre.y).toBeCloseTo(bounds.y + bounds.h / 2, 4);
  });

  it("frames content in negative space too", () => {
    const bounds = { x: -9000, y: -7000, w: 400, h: 300 };
    const view = zoomToFit(bounds, size);
    const centre = screenToLogical({ x: size.w / 2, y: size.h / 2 }, view);
    expect(centre.x).toBeCloseTo(bounds.x + bounds.w / 2, 4);
  });

  it("zooms out for content larger than the viewport", () => {
    expect(zoomToFit({ x: 0, y: 0, w: 8000, h: 6000 }, size).zoom)
      .toBeLessThan(1);
  });

  it("respects the zoom floor for absurdly large content", () => {
    expect(zoomToFit({ x: 0, y: 0, w: 5e7, h: 5e7 }, size).zoom)
      .toBeGreaterThanOrEqual(MIN_ZOOM);
  });

  it("returns a sane view for an empty board", () => {
    const view = zoomToFit(null, size);
    expect(view.zoom).toBe(1);
    const centre = screenToLogical({ x: size.w / 2, y: size.h / 2 }, view);
    expect(centre.x).toBeCloseTo(0, 4);
    expect(centre.y).toBeCloseTo(0, 4);
  });

  it("returns a sane view for zero-area content", () => {
    expect(zoomToFit({ x: 5, y: 5, w: 0, h: 0 }, size).zoom).toBe(1);
  });
});

describe("stroke building", () => {
  it("produces an ink object from three points", () => {
    const b = new StrokeBuilder("#111", 4);
    b.begin({ x: 0, y: 0 });
    b.extend({ x: 10, y: 10 });
    b.extend({ x: 20, y: 0 });
    const obj = b.finish();
    expect(obj).not.toBeNull();
    expect(obj!.kind).toBe("ink");
    expect((obj as { pts: number[] }).pts).toEqual([0, 0, 10, 10, 20, 0]);
  });

  it("carries the caller's colour and width", () => {
    const b = new StrokeBuilder("#ff0000", 12);
    b.begin({ x: 0, y: 0 });
    b.extend({ x: 10, y: 0 });
    expect(b.finish()).toMatchObject({ color: "#ff0000", width: 12 });
  });

  it("discards a single-point tap", () => {
    const b = new StrokeBuilder("#111", 4);
    b.begin({ x: 5, y: 5 });
    expect(b.finish()).toBeNull();
  });

  it("discards a stroke that never began", () => {
    expect(new StrokeBuilder("#111", 4).finish()).toBeNull();
  });

  it("drops a duplicate consecutive point", () => {
    // A held stylus emits the same coordinate repeatedly; keeping them
    // bloats the stroke and the saved document for no visible gain.
    const b = new StrokeBuilder("#111", 4);
    b.begin({ x: 0, y: 0 });
    b.extend({ x: 0, y: 0 });
    b.extend({ x: 10, y: 0 });
    expect((b.finish() as { pts: number[] }).pts).toEqual([0, 0, 10, 0]);
  });

  it("gives every stroke a distinct id", () => {
    const make = () => {
      const b = new StrokeBuilder("#111", 4);
      b.begin({ x: 0, y: 0 });
      b.extend({ x: 1, y: 1 });
      return b.finish()!;
    };
    expect(make().id).not.toBe(make().id);
  });

  it("marks a finished stroke as local, not agent-authored", () => {
    const b = new StrokeBuilder("#111", 4);
    b.begin({ x: 0, y: 0 });
    b.extend({ x: 1, y: 1 });
    expect(b.finish()!.origin).toBe("local");
  });
});

describe("coalesced pointer samples", () => {
  it("uses getCoalescedEvents when the browser provides it", () => {
    // Browsers drop intermediate points between animation frames; without
    // these a fast stroke lands as a visibly cornered polygon, and that
    // geometry is what the model reads back out of the atlas.
    const event = {
      clientX: 30,
      clientY: 40,
      getCoalescedEvents: () => [
        { clientX: 10, clientY: 20 },
        { clientX: 20, clientY: 30 },
      ],
    } as unknown as PointerEvent;
    expect(strokePointsFromEvent(event)).toEqual([
      { x: 10, y: 20 },
      { x: 20, y: 30 },
    ]);
  });

  it("falls back to the event itself", () => {
    const event = { clientX: 30, clientY: 40 } as unknown as PointerEvent;
    expect(strokePointsFromEvent(event)).toEqual([{ x: 30, y: 40 }]);
  });

  it("falls back when the browser returns an empty batch", () => {
    const event = {
      clientX: 7,
      clientY: 8,
      getCoalescedEvents: () => [],
    } as unknown as PointerEvent;
    expect(strokePointsFromEvent(event)).toEqual([{ x: 7, y: 8 }]);
  });
});
