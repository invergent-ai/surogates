/**
 * Validation, bounds and Canvas2D rendering for the `draw` command.
 *
 * Ported from PenEcho's `public/draw.js` (https://penecho.ai,
 * AGPL-3.0-only — the same licence as this project). The algorithms are
 * theirs: cubic and arc extrema for exact bounds, the Catmull-Rom-style
 * smoothing, and the arrowhead geometry. Rewritten as typed ESM because
 * the upstream UMD wrapper assigns `module.exports` at runtime, which no
 * bundler can resolve statically.
 *
 * The limits below are load-bearing and mirrored on the Python side in
 * `surogates/whiteboard/commands.py`.
 */

const TYPES = new Set([
  "line",
  "smooth",
  "rect",
  "ellipse",
  "circle",
  "arc",
] as const);

export type DrawType =
  | "line"
  | "smooth"
  | "rect"
  | "ellipse"
  | "circle"
  | "arc";

const MAX_ITEMS = 64;
const MAX_VALUES = 2048;
const MAX_POINT_VALUES = 512;
const MAX_RASTER_PIXELS = 12_000_000;
const MAX_RASTER_SIDE = 4096;
const DEFAULT_WIDTH = 30;
const DEFAULT_TENSION = 50;
const FILL_ALPHA = 0.14;
const TAU = Math.PI * 2;

export interface Point {
  x: number;
  y: number;
}

export interface CubicSegment {
  from: Point;
  c1: Point;
  c2: Point;
  to: Point;
}

export interface DrawBounds {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface MutableBounds {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

interface Primitive {
  type: DrawType;
  closed: boolean;
  fill: boolean;
  arrow: boolean;
  points?: Point[];
  segments?: CubicSegment[];
  arrowPoints?: Point[];
  x?: number;
  y?: number;
  w?: number;
  h?: number;
  cx?: number;
  cy?: number;
  rx?: number;
  ry?: number;
  start?: number;
  sweep?: number;
}

export interface DrawCommand {
  origin: [number, number] | number[];
  types: string[];
  items: number[][];
  width?: number;
  tension?: number;
  closed?: number[];
  fill?: number[];
  arrows?: number[];
}

export interface NormalizedDraw {
  tool: "draw";
  origin: number[];
  types: string[];
  items: number[][];
  closed: number[];
  fill: number[];
  arrows: number[];
  width: number;
  tension: number;
  x: number;
  y: number;
  _draw: { primitives: Primitive[]; bounds: DrawBounds };
}

export interface RenderedDraw {
  image: HTMLCanvasElement;
  x: number;
  y: number;
}

// ---------------------------------------------------------------------
// Geometry helpers
// ---------------------------------------------------------------------

const isInt = (value: unknown, min: number, max: number): value is number =>
  typeof value === "number" &&
  Number.isInteger(value) &&
  value >= min &&
  value <= max;

function includePoint(bounds: MutableBounds, p: Point): void {
  bounds.left = Math.min(bounds.left, p.x);
  bounds.top = Math.min(bounds.top, p.y);
  bounds.right = Math.max(bounds.right, p.x);
  bounds.bottom = Math.max(bounds.bottom, p.y);
}

function flatPoints(values: number[], origin: number[]): Point[] {
  const points: Point[] = [];
  for (let i = 0; i < values.length; i += 2) {
    points.push({ x: origin[0] + values[i], y: origin[1] + values[i + 1] });
  }
  return points;
}

const cubicAt = (
  a: number,
  c1: number,
  c2: number,
  b: number,
  t: number,
): number => {
  const inv = 1 - t;
  return (
    inv ** 3 * a + 3 * inv ** 2 * t * c1 + 3 * inv * t ** 2 * c2 + t ** 3 * b
  );
};

/** Parameter values where a cubic turns, so bounds cover the bulge. */
function cubicExtrema(
  start: number,
  c1: number,
  c2: number,
  end: number,
): number[] {
  const a = -start + 3 * c1 - 3 * c2 + end;
  const b = 2 * (start - 2 * c1 + c2);
  const c = c1 - start;
  const roots: number[] = [];
  if (Math.abs(a) < 1e-9) {
    if (Math.abs(b) >= 1e-9) roots.push(-c / b);
  } else {
    const discriminant = b * b - 4 * a * c;
    if (discriminant >= 0) {
      const root = Math.sqrt(discriminant);
      roots.push((-b + root) / (2 * a), (-b - root) / (2 * a));
    }
  }
  return roots.filter((v) => v > 0 && v < 1);
}

function includeCubic(bounds: MutableBounds, seg: CubicSegment): void {
  includePoint(bounds, seg.from);
  includePoint(bounds, seg.to);
  const ts = new Set([
    ...cubicExtrema(seg.from.x, seg.c1.x, seg.c2.x, seg.to.x),
    ...cubicExtrema(seg.from.y, seg.c1.y, seg.c2.y, seg.to.y),
  ]);
  for (const t of ts) {
    includePoint(bounds, {
      x: cubicAt(seg.from.x, seg.c1.x, seg.c2.x, seg.to.x, t),
      y: cubicAt(seg.from.y, seg.c1.y, seg.c2.y, seg.to.y, t),
    });
  }
}

const normalizeAngle = (a: number): number => ((a % TAU) + TAU) % TAU;

function angleOnSweep(angle: number, start: number, sweep: number): boolean {
  if (Math.abs(sweep) >= TAU) return true;
  const distance =
    sweep > 0 ? normalizeAngle(angle - start) : normalizeAngle(start - angle);
  return distance <= Math.abs(sweep) + 1e-9;
}

/** Arc bounds: the axis crossings the sweep actually passes through. */
function includeArc(bounds: MutableBounds, p: Primitive): void {
  const cx = p.cx!;
  const cy = p.cy!;
  const rx = p.rx!;
  const ry = p.ry!;
  const start = p.start!;
  const sweep = p.sweep!;
  if (Math.abs(sweep) >= TAU) {
    includePoint(bounds, { x: cx - rx, y: cy - ry });
    includePoint(bounds, { x: cx + rx, y: cy + ry });
    return;
  }
  const angles = [
    start,
    start + sweep,
    0,
    Math.PI / 2,
    Math.PI,
    Math.PI * 1.5,
  ];
  for (const angle of angles) {
    if (!angleOnSweep(angle, start, sweep)) continue;
    includePoint(bounds, {
      x: cx + rx * Math.cos(angle),
      y: cy + ry * Math.sin(angle),
    });
  }
}

/**
 * Cubic control points for a smooth path through *points*.
 *
 * `tension` is 0..100; 50 is the neutral default.
 */
export function smoothSegments(
  points: Point[],
  closed: boolean,
  tension: number,
): CubicSegment[] {
  if (points.length < 3) return [];
  const segments: CubicSegment[] = [];
  const count = closed ? points.length : points.length - 1;
  const strength = tension / 50 / 6;
  for (let i = 0; i < count; i++) {
    const p1 = points[i];
    const p2 = points[(i + 1) % points.length];
    const p0 = closed
      ? points[(i - 1 + points.length) % points.length]
      : points[Math.max(0, i - 1)];
    const p3 = closed
      ? points[(i + 2) % points.length]
      : points[Math.min(points.length - 1, i + 2)];
    segments.push({
      from: p1,
      c1: {
        x: p1.x + (p2.x - p0.x) * strength,
        y: p1.y + (p2.y - p0.y) * strength,
      },
      c2: {
        x: p2.x - (p3.x - p1.x) * strength,
        y: p2.y - (p3.y - p1.y) * strength,
      },
      to: p2,
    });
  }
  return segments;
}

function arrowGeometry(end: Point, tangentFrom: Point, width: number): Point[] {
  const angle = Math.atan2(end.y - tangentFrom.y, end.x - tangentFrom.x);
  const size = Math.max(18, width * 2.2);
  const spread = 0.52;
  return [
    { x: end.x, y: end.y },
    {
      x: end.x - size * Math.cos(angle - spread),
      y: end.y - size * Math.sin(angle - spread),
    },
    {
      x: end.x - size * Math.cos(angle + spread),
      y: end.y - size * Math.sin(angle + spread),
    },
  ];
}

/**
 * The last point distinct from the path's end, so the arrowhead points
 * along the real direction of travel rather than at a duplicate.
 */
function terminalTangentFrom(p: Primitive): Point | null {
  const points = p.points!;
  const end = points[points.length - 1];
  const candidates: Point[] = [];
  if (p.segments?.length) {
    for (let i = p.segments.length - 1; i >= 0; i--) {
      const s = p.segments[i];
      candidates.push(s.c2, s.c1, s.from);
    }
  } else {
    for (let i = points.length - 2; i >= 0; i--) candidates.push(points[i]);
  }
  return (
    candidates.find((pt) => Math.hypot(end.x - pt.x, end.y - pt.y) > 1e-6) ??
    null
  );
}

/** Parse an optional index list; `null` means invalid. */
function indexSet(value: unknown, count: number): Set<number> | null {
  if (value === undefined) return new Set();
  if (!Array.isArray(value) || value.length > count) return null;
  const result = new Set<number>();
  for (const index of value) {
    if (!isInt(index, 0, count - 1) || result.has(index)) return null;
    result.add(index);
  }
  return result;
}

// ---------------------------------------------------------------------
// Normalize
// ---------------------------------------------------------------------

/**
 * Validate a `draw` command and compute its geometry.
 *
 * Returns `null` for anything malformed — the caller drops the object
 * rather than carrying one that can never paint.
 */
export function normalize(
  command: unknown,
  canvasSize = 20_000,
): NormalizedDraw | null {
  const cmd = command as DrawCommand | null;
  if (
    !cmd ||
    typeof cmd !== "object" ||
    !Array.isArray(cmd.origin) ||
    cmd.origin.length !== 2 ||
    !cmd.origin.every((v) => isInt(v, 0, canvasSize))
  ) {
    return null;
  }
  if (
    !Array.isArray(cmd.types) ||
    !Array.isArray(cmd.items) ||
    !cmd.types.length ||
    cmd.types.length !== cmd.items.length ||
    cmd.types.length > MAX_ITEMS
  ) {
    return null;
  }

  const width = cmd.width === undefined ? DEFAULT_WIDTH : cmd.width;
  const tension = cmd.tension === undefined ? DEFAULT_TENSION : cmd.tension;
  if (!isInt(width, 2, 200) || !isInt(tension, 0, 100)) return null;

  const closed = indexSet(cmd.closed, cmd.items.length);
  const fill = indexSet(cmd.fill, cmd.items.length);
  const arrows = indexSet(cmd.arrows, cmd.items.length);
  if (!closed || !fill || !arrows) return null;

  const bounds: MutableBounds = {
    left: Infinity,
    top: Infinity,
    right: -Infinity,
    bottom: -Infinity,
  };
  const primitives: Primitive[] = [];
  const origin = cmd.origin;
  let valueCount = 0;

  for (let index = 0; index < cmd.items.length; index++) {
    const type = cmd.types[index] as DrawType;
    const item = cmd.items[index];
    if (
      !TYPES.has(type as never) ||
      !Array.isArray(item) ||
      !item.every((v) => isInt(v, -canvasSize, canvasSize))
    ) {
      return null;
    }
    valueCount += item.length;
    if (valueCount > MAX_VALUES) return null;

    const p: Primitive = {
      type,
      closed: closed.has(index),
      fill: fill.has(index),
      arrow: arrows.has(index),
    };

    if (type === "line" || type === "smooth") {
      if (
        item.length < 4 ||
        item.length % 2 ||
        item.length > MAX_POINT_VALUES
      ) {
        return null;
      }
      p.points = flatPoints(item, origin);
      if (p.closed && p.points.length < 3) return null;
      p.segments =
        type === "smooth" ? smoothSegments(p.points, p.closed, tension) : [];
      if (p.segments.length) {
        for (const s of p.segments) includeCubic(bounds, s);
      } else {
        for (const pt of p.points) includePoint(bounds, pt);
      }
      if (p.fill && !p.closed) return null;
      if (p.arrow && p.closed) return null;
      if (p.arrow) {
        const end = p.points[p.points.length - 1];
        const tangentFrom = terminalTangentFrom(p);
        if (!tangentFrom) return null;
        p.arrowPoints = arrowGeometry(end, tangentFrom, width);
        for (const pt of p.arrowPoints) includePoint(bounds, pt);
      }
    } else if (type === "rect") {
      if (
        item.length !== 4 ||
        !isInt(item[2], 1, canvasSize) ||
        !isInt(item[3], 1, canvasSize) ||
        p.closed ||
        p.arrow
      ) {
        return null;
      }
      p.x = origin[0] + item[0];
      p.y = origin[1] + item[1];
      p.w = item[2];
      p.h = item[3];
      includePoint(bounds, { x: p.x, y: p.y });
      includePoint(bounds, { x: p.x + p.w, y: p.y + p.h });
    } else if (type === "ellipse" || type === "circle") {
      const wantsLength = type === "ellipse" ? 4 : 3;
      if (
        item.length !== wantsLength ||
        !isInt(item[2], 1, canvasSize) ||
        (type === "ellipse" && !isInt(item[3], 1, canvasSize)) ||
        p.closed ||
        p.arrow
      ) {
        return null;
      }
      p.cx = origin[0] + item[0];
      p.cy = origin[1] + item[1];
      p.rx = item[2];
      p.ry = type === "ellipse" ? item[3] : item[2];
      includePoint(bounds, { x: p.cx - p.rx, y: p.cy - p.ry });
      includePoint(bounds, { x: p.cx + p.rx, y: p.cy + p.ry });
    } else {
      if (
        item.length !== 6 ||
        !isInt(item[2], 1, canvasSize) ||
        !isInt(item[3], 1, canvasSize) ||
        !isInt(item[4], -3600, 3600) ||
        !isInt(item[5], -3600, 3600) ||
        item[5] === 0 ||
        p.closed ||
        p.fill
      ) {
        return null;
      }
      p.cx = origin[0] + item[0];
      p.cy = origin[1] + item[1];
      p.rx = item[2];
      p.ry = item[3];
      p.start = (item[4] * Math.PI) / 180;
      p.sweep = (item[5] * Math.PI) / 180;
      includeArc(bounds, p);
      if (p.arrow) {
        const endAngle = p.start + p.sweep;
        const end = {
          x: p.cx + p.rx * Math.cos(endAngle),
          y: p.cy + p.ry * Math.sin(endAngle),
        };
        const direction = p.sweep > 0 ? 1 : -1;
        p.arrowPoints = arrowGeometry(
          end,
          {
            x: end.x + direction * p.rx * Math.sin(endAngle) * 0.1,
            y: end.y - direction * p.ry * Math.cos(endAngle) * 0.1,
          },
          width,
        );
        for (const pt of p.arrowPoints) includePoint(bounds, pt);
      }
    }
    primitives.push(p);
  }

  if (
    bounds.left < 0 ||
    bounds.top < 0 ||
    bounds.right > canvasSize ||
    bounds.bottom > canvasSize
  ) {
    return null;
  }

  // Half the stroke sits outside the path, plus a little slack for the
  // round join, or a thick outline clips at the raster edge.
  const pad = Math.ceil(width / 2 + 4);
  const x = Math.max(0, Math.floor(bounds.left - pad));
  const y = Math.max(0, Math.floor(bounds.top - pad));
  const right = Math.min(canvasSize, Math.ceil(bounds.right + pad));
  const bottom = Math.min(canvasSize, Math.ceil(bounds.bottom + pad));

  return {
    tool: "draw",
    origin: [...origin],
    types: [...cmd.types],
    items: cmd.items.map((item) => [...item]),
    closed: [...closed].sort((a, b) => a - b),
    fill: [...fill].sort((a, b) => a - b),
    arrows: [...arrows].sort((a, b) => a - b),
    width,
    tension,
    x,
    y,
    _draw: {
      primitives,
      bounds: {
        x,
        y,
        w: Math.max(1, right - x),
        h: Math.max(1, bottom - y),
      },
    },
  };
}

// ---------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------

function trace(ctx: CanvasRenderingContext2D, p: Primitive): void {
  if (p.type === "line") {
    const points = p.points!;
    ctx.moveTo(points[0].x, points[0].y);
    for (const pt of points.slice(1)) ctx.lineTo(pt.x, pt.y);
    if (p.closed) ctx.closePath();
  } else if (p.type === "smooth") {
    const points = p.points!;
    ctx.moveTo(points[0].x, points[0].y);
    if (p.segments?.length) {
      for (const s of p.segments) {
        ctx.bezierCurveTo(s.c1.x, s.c1.y, s.c2.x, s.c2.y, s.to.x, s.to.y);
      }
    } else {
      ctx.lineTo(points[1].x, points[1].y);
    }
    if (p.closed) ctx.closePath();
  } else if (p.type === "rect") {
    ctx.rect(p.x!, p.y!, p.w!, p.h!);
  } else if (p.type === "circle" || p.type === "ellipse") {
    ctx.ellipse(p.cx!, p.cy!, p.rx!, p.ry!, 0, 0, TAU);
  } else {
    ctx.ellipse(
      p.cx!,
      p.cy!,
      p.rx!,
      p.ry!,
      0,
      p.start!,
      p.start! + p.sweep!,
      p.sweep! < 0,
    );
  }
}

/**
 * Rasterise a `draw` command onto its own canvas.
 *
 * Returns the image plus the logical position of its top-left corner, or
 * `null` when the command is invalid. The raster is capped by both side
 * length and total pixels so one command cannot allocate an enormous
 * bitmap.
 */
export function render(
  command: unknown,
  createCanvas: (w: number, h: number) => HTMLCanvasElement,
  color = "#2563eb",
): RenderedDraw | null {
  const prepared =
    (command as NormalizedDraw | null)?._draw != null
      ? (command as NormalizedDraw)
      : normalize(command);
  if (!prepared) return null;

  const bounds = prepared._draw.bounds;
  const logicalWidth = bounds.w;
  const logicalHeight = bounds.h;
  const rasterScale = Math.min(
    1,
    MAX_RASTER_SIDE / logicalWidth,
    MAX_RASTER_SIDE / logicalHeight,
    Math.sqrt(MAX_RASTER_PIXELS / (logicalWidth * logicalHeight)),
  );
  const rasterWidth = Math.max(1, Math.floor(logicalWidth * rasterScale));
  const rasterHeight = Math.max(1, Math.floor(logicalHeight * rasterScale));
  const scaleX = rasterWidth / logicalWidth;
  const scaleY = rasterHeight / logicalHeight;

  const image = createCanvas(rasterWidth, rasterHeight);
  const ctx = image.getContext("2d");
  if (!ctx) return null;

  ctx.setTransform(scaleX, 0, 0, scaleY, -bounds.x * scaleX, -bounds.y * scaleY);
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = prepared.width;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";

  for (const p of prepared._draw.primitives) {
    ctx.beginPath();
    trace(ctx, p);
    if (p.fill) {
      ctx.save();
      ctx.globalAlpha = FILL_ALPHA;
      ctx.fill();
      ctx.restore();
    }
    ctx.stroke();
    if (p.arrowPoints) {
      ctx.beginPath();
      ctx.moveTo(p.arrowPoints[0].x, p.arrowPoints[0].y);
      ctx.lineTo(p.arrowPoints[1].x, p.arrowPoints[1].y);
      ctx.lineTo(p.arrowPoints[2].x, p.arrowPoints[2].y);
      ctx.closePath();
      ctx.fill();
    }
  }

  return { image, x: bounds.x, y: bounds.y };
}
