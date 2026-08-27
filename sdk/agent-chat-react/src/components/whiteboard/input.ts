import type { WbObject } from "./doc";
import type { View, ViewportSize } from "./render";

// open-pencil's range. Wide on purpose: on an infinite canvas the only
// way to see a whole sprawling board is to zoom right out.
export const MIN_ZOOM = 0.02;
export const MAX_ZOOM = 256;

/** Wheel-delta divisor for exponential zoom. Larger = gentler. */
const ZOOM_DIVISOR = 300;

export interface Point {
  x: number;
  y: number;
}

// ---------------------------------------------------------------------
// Coordinate mapping
// ---------------------------------------------------------------------

/** Viewport pixel -> logical canvas coordinate. */
export function screenToLogical(pt: Point, view: View): Point {
  return { x: view.x + pt.x / view.zoom, y: view.y + pt.y / view.zoom };
}

/** Logical canvas coordinate -> viewport pixel. */
export function logicalToScreen(pt: Point, view: View): Point {
  return { x: (pt.x - view.x) * view.zoom, y: (pt.y - view.y) * view.zoom };
}

// ---------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------

const clamp = (v: number, lo: number, hi: number) =>
  Math.min(hi, Math.max(lo, v));

/**
 * Scale about a fixed viewport point.
 *
 * The clamp is applied to the zoom *before* solving for the new origin.
 * Solving first and clamping after lets the board slide under the cursor
 * on the frame a zoom hits its limit.
 */
export function zoomAt(view: View, screenPt: Point, factor: number): View {
  const zoom = clamp(view.zoom * factor, MIN_ZOOM, MAX_ZOOM);
  // The logical point under the cursor must not move, so:
  //   anchor = view.x + screenPt.x / view.zoom = x' + screenPt.x / zoom
  const anchor = screenToLogical(screenPt, view);
  return {
    zoom,
    x: anchor.x - screenPt.x / zoom,
    y: anchor.y - screenPt.y / zoom,
  };
}

/**
 * Pan by a viewport-pixel delta.
 *
 * The view origin moves opposite to the drag: dragging the board to the
 * right reveals content to its left.
 */
export function panBy(view: View, deltaScreen: Point): View {
  return {
    ...view,
    x: view.x - deltaScreen.x / view.zoom,
    y: view.y - deltaScreen.y / view.zoom,
  };
}

/**
 * Exponential zoom factor from a wheel delta.
 *
 * Exponential, not a fixed step: wheel deltas vary by three orders of
 * magnitude between a notched mouse wheel and a trackpad's pixel-precise
 * scroll, and a fixed 1.1x per event makes the trackpad unusable.
 * open-pencil's formula.
 */
export function zoomFactorFromWheel(delta: number): number {
  return Math.exp(-delta / ZOOM_DIVISOR);
}

/**
 * Fit *bounds* into the viewport, with margin.
 *
 * An infinite canvas needs a way home: pan far enough and there is no
 * edge to stop you, so "zoom to fit" is the only reliable route back to
 * the content. Returns the identity view when there is nothing to fit.
 */
export function zoomToFit(
  bounds: { x: number; y: number; w: number; h: number } | null,
  size: ViewportSize,
  padding = 80,
): View {
  if (!bounds || bounds.w <= 0 || bounds.h <= 0) {
    return { x: -size.w / 2, y: -size.h / 2, zoom: 1 };
  }
  const zoom = clamp(
    Math.min(size.w / (bounds.w + padding * 2), size.h / (bounds.h + padding * 2)),
    MIN_ZOOM,
    MAX_ZOOM,
  );
  return {
    zoom,
    x: bounds.x + bounds.w / 2 - size.w / 2 / zoom,
    y: bounds.y + bounds.h / 2 - size.h / 2 / zoom,
  };
}

// ---------------------------------------------------------------------
// Pointer samples
// ---------------------------------------------------------------------

interface CoalescingEvent {
  clientX: number;
  clientY: number;
  getCoalescedEvents?: () => { clientX: number; clientY: number }[];
}

/**
 * Every position this pointer event carries, oldest first.
 *
 * Browsers coalesce pointer moves to one per animation frame and hide the
 * intermediate positions behind `getCoalescedEvents()`. Without them a
 * fast stroke lands as a visibly cornered polygon — and that geometry is
 * exactly what the model reads back out of the atlas, so the fidelity is
 * not merely cosmetic.
 *
 * No pressure: the model reads a PNG, so stroke dynamics cannot change
 * what it transcribes, and only a stylus reports pressure at all.
 */
export function strokePointsFromEvent(event: PointerEvent): Point[] {
  const e = event as unknown as CoalescingEvent;
  const batch =
    typeof e.getCoalescedEvents === "function" ? e.getCoalescedEvents() : [];
  if (batch.length > 0) {
    return batch.map((s) => ({ x: s.clientX, y: s.clientY }));
  }
  return [{ x: e.clientX, y: e.clientY }];
}

// ---------------------------------------------------------------------
// Stroke building
// ---------------------------------------------------------------------

let strokeCounter = 0;

/**
 * Accumulates one freehand stroke as a flat point list.
 *
 * Points arrive in logical coordinates; the caller converts from screen
 * space, because only it knows the current view.
 */
export class StrokeBuilder {
  /** Flat [x0,y0,x1,y1,...], matching the ink object's `pts`. */
  private readonly pts: number[] = [];
  private started = false;

  constructor(
    private readonly color: string,
    private readonly width: number,
  ) {}

  begin(pt: Point): void {
    this.started = true;
    this.push(pt);
  }

  extend(pt: Point): void {
    if (!this.started) return;
    this.push(pt);
  }

  private push(pt: Point): void {
    // No clamping to a canvas rectangle: there isn't one. Only guard
    // against a non-finite coordinate, which would poison every bounds
    // computation downstream.
    if (!Number.isFinite(pt.x) || !Number.isFinite(pt.y)) return;
    const x = pt.x;
    const y = pt.y;
    // A held stylus emits the same coordinate repeatedly; keeping the
    // duplicates bloats the stroke and the saved document for no gain.
    const n = this.pts.length;
    if (n >= 2 && this.pts[n - 2] === x && this.pts[n - 1] === y) return;
    this.pts.push(x, y);
  }

  /** The finished stroke, or `null` if it never became a line. */
  finish(): WbObject | null {
    if (this.pts.length < 4) return null;
    strokeCounter += 1;
    return {
      id: `local:${strokeCounter}`,
      origin: "local",
      selected: false,
      kind: "ink",
      pts: [...this.pts],
      width: this.width,
      color: this.color,
    };
  }

  /** Points captured so far, for painting the in-progress stroke. */
  get points(): number[] {
    return this.pts;
  }
}
