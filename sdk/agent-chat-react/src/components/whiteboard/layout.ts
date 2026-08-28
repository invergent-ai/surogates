/**
 * Relational placement: the model names a thing and a relation, this
 * module computes the geometry.
 *
 * The old contract asked the model to be a geometry engine — convert
 * image pixels to canvas units, predict font metrics, guess wrapping —
 * and production sessions failed at each: a conversion done for x and
 * skipped for y, a sentence wrapped into a nine-line tower, an answer
 * placed on a board that had moved since the picture was taken. Every
 * one of those is deterministic arithmetic, so it lives here now.
 *
 * Anchored commands resolve at fold time against the board AS IT IS
 * THEN, not as it was when the model saw it — so a user dragging
 * objects mid-turn no longer corrupts placement: "right of the equals
 * sign" follows the equals sign. See docs/board/relational-placement.md.
 */
import type { Rect } from "./atlas";
import type { CommandResolver, WbDoc } from "./doc";
import { objectBounds } from "./render";
import type { RenderServices } from "./render";

/** Average glyph advance as a fraction of font size. Same fudge the
 *  server-side wrap check uses; only ever sizes blocks, never lays out
 *  glyphs — the renderer measures for real. */
const GLYPH_ADVANCE = 0.6;

/** Text at or under this length is an answer, not prose: a number, a
 *  short formula, a label. Answers match the anchor's height; prose
 *  gets a readable size instead — matching 80-unit handwriting is what
 *  turned one sentence into a 9-line tower. */
const SHORT_ANSWER_CHARS = 24;

/** Readable prose bounds, in canvas units. */
const PROSE_MIN_FONT = 18;
const PROSE_MAX_FONT = 34;

/** ponytail: push-down collision nudge, 8 tries then give up — a
 *  constraint solver if boards ever get dense enough to need one. */
const NUDGE_TRIES = 8;

const clamp = (v: number, lo: number, hi: number) =>
  Math.min(hi, Math.max(lo, v));

/** The `whiteboard` block of the turn's user-message metadata. */
export interface TurnAnchors {
  latestInput?: Rect;
  selection?: Rect;
}

function rectOf(value: unknown): Rect | null {
  if (!value || typeof value !== "object") return null;
  const r = value as Record<string, unknown>;
  return typeof r.x === "number" &&
    typeof r.y === "number" &&
    typeof r.w === "number" &&
    typeof r.h === "number"
    ? { x: r.x, y: r.y, w: r.w, h: r.h }
    : null;
}

/** Anchors from a user message's metadata, or null when it has none. */
export function turnAnchorsFromMetadata(
  metadata: Record<string, unknown> | undefined,
): TurnAnchors | null {
  const wb = metadata?.whiteboard;
  if (!wb || typeof wb !== "object") return null;
  const block = wb as Record<string, unknown>;
  const anchors: TurnAnchors = {};
  const latest = rectOf(block.latestInput);
  const selection = rectOf(block.selection);
  if (latest) anchors.latestInput = latest;
  if (selection) anchors.selection = selection;
  return anchors;
}

/** Union bounds of every surviving object from one draw call. */
function originBounds(
  doc: WbDoc,
  origin: string,
  services: RenderServices,
): Rect | null {
  let acc: Rect | null = null;
  for (const obj of doc.objects) {
    if (obj.origin !== origin) continue;
    const b = objectBounds(obj, services);
    if (!b) continue;
    if (!acc) {
      acc = { ...b };
      continue;
    }
    const x = Math.min(acc.x, b.x);
    const y = Math.min(acc.y, b.y);
    acc = {
      x,
      y,
      w: Math.max(acc.x + acc.w, b.x + b.w) - x,
      h: Math.max(acc.y + acc.h, b.y + b.h) - y,
    };
  }
  return acc;
}

function anchorRect(
  cmd: Record<string, unknown>,
  doc: WbDoc,
  anchors: TurnAnchors | null,
  services: RenderServices,
): Rect | null {
  const anchor = cmd.anchor;
  if (typeof anchor === "string" && anchor) {
    if (anchor === "latest") return anchors?.latestInput ?? null;
    if (anchor === "selection") return anchors?.selection ?? null;
    return originBounds(doc, anchor, services);
  }
  // A replaces with no anchor and no coordinates: the revision takes
  // the replaced object's place.
  if (typeof cmd.replaces === "string" && cmd.replaces) {
    return originBounds(doc, cmd.replaces, services);
  }
  return null;
}

/** Estimated block size for a command whose sizes are already chosen. */
function estimateSize(
  cmd: Record<string, unknown>,
  services: RenderServices,
): { w: number; h: number } {
  const font = typeof cmd.fontSize === "number" ? cmd.fontSize : 24;
  if (cmd.tool === "draw_formula") {
    return services.formula(String(cmd.latex ?? ""), font);
  }
  const text = String(cmd.text ?? "");
  const maxWidth = typeof cmd.maxWidth === "number" ? cmd.maxWidth : 320;
  const lineHeight =
    typeof cmd.lineHeight === "number" ? cmd.lineHeight : 1.35;
  let lines = 0;
  let widest = 0;
  for (const paragraph of text.split("\n")) {
    const width = paragraph.length * font * GLYPH_ADVANCE;
    lines += Math.max(1, Math.ceil(width / maxWidth));
    widest = Math.max(widest, Math.min(width, maxWidth));
  }
  return { w: widest || maxWidth, h: Math.max(1, lines) * font * lineHeight };
}

/** Fill in fontSize / maxWidth for an anchored command that omits them. */
function applySizing(cmd: Record<string, unknown>, target: Rect): void {
  const isProse =
    cmd.tool === "write_text" &&
    (String(cmd.text ?? "").length > SHORT_ANSWER_CHARS ||
      String(cmd.text ?? "").includes("\n"));
  if (typeof cmd.fontSize !== "number") {
    cmd.fontSize = isProse
      ? clamp(target.h * 0.45, PROSE_MIN_FONT, PROSE_MAX_FONT)
      : clamp(target.h * 0.9, 16, 220);
  }
  if (cmd.tool === "write_text" && typeof cmd.maxWidth !== "number") {
    // Wide enough to read across: ~42 characters a line, never a tower.
    const font = cmd.fontSize as number;
    cmd.maxWidth = Math.max(320, Math.round(42 * font * GLYPH_ADVANCE));
  }
}

function overlaps(a: Rect, b: Rect): boolean {
  return (
    a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y
  );
}

/**
 * Resolve anchored commands into absolute ones.
 *
 * Absolute commands pass through untouched — explicit coordinates are
 * the escape hatch and always win. An anchored command whose anchor
 * cannot be resolved is dropped (returned as null in place), because a
 * guessed position is exactly the failure this module exists to end.
 */
export function resolveCommands(
  doc: WbDoc,
  commands: unknown[],
  anchors: TurnAnchors | null,
  services: RenderServices,
): unknown[] {
  const occupied: Rect[] = [];
  for (const obj of doc.objects) {
    const b = objectBounds(obj, services);
    if (b) occupied.push(b);
  }

  const resolved: unknown[] = [];
  for (const raw of commands) {
    if (!raw || typeof raw !== "object") {
      resolved.push(raw);
      continue;
    }
    const cmd = { ...(raw as Record<string, unknown>) };
    const positioned =
      typeof cmd.x === "number" && typeof cmd.y === "number";
    const wantsAnchor =
      (typeof cmd.anchor === "string" && cmd.anchor) ||
      (!positioned && typeof cmd.replaces === "string" && cmd.replaces);

    if (positioned || !wantsAnchor) {
      resolved.push(cmd);
      continue;
    }

    const target = anchorRect(cmd, doc, anchors, services);
    if (!target) {
      // No rect to relate to. Dropping beats guessing: the model is
      // told in the tool result what was skipped and why.
      resolved.push(null);
      continue;
    }

    applySizing(cmd, target);
    const est = estimateSize(cmd, services);
    const gap = Math.max(12, target.h * 0.35);
    // A revision with no anchor and no side takes the replaced object's
    // place — that is the whole point of replacing. A side is honoured
    // when given ("put the correction below the old one"), and an
    // explicit anchor always goes through the side placement.
    const inPlace =
      typeof cmd.anchor !== "string" &&
      typeof cmd.side !== "string" &&
      typeof cmd.replaces === "string";
    const side = inPlace
      ? "in-place"
      : typeof cmd.side === "string"
        ? cmd.side
        : "right";

    let x: number;
    let y: number;
    switch (side) {
      case "in-place":
        x = target.x;
        y = target.y;
        break;
      case "below":
        x = target.x;
        y = target.y + target.h + gap;
        break;
      case "above":
        x = target.x;
        y = target.y - gap - est.h;
        break;
      case "left":
        x = target.x - gap - est.w;
        y = target.y + (target.h - est.h) / 2;
        break;
      default:
        x = target.x + target.w + gap;
        y = target.y + (target.h - est.h) / 2;
    }

    // Keep off existing work: step downward until clear. The replaced
    // objects do not count — the revision is taking their spot.
    const ignore = typeof cmd.replaces === "string" ? cmd.replaces : null;
    const blockers = ignore
      ? doc.objects
          .filter((o) => o.origin !== ignore)
          .map((o) => objectBounds(o, services))
          .filter((b): b is Rect => b !== null)
      : occupied;
    for (let i = 0; i < NUDGE_TRIES; i++) {
      const box = { x, y, w: est.w, h: est.h };
      if (!blockers.some((b) => overlaps(box, b))) break;
      y += Math.max(est.h * 0.5, gap);
    }

    cmd.x = Math.round(x * 100) / 100;
    cmd.y = Math.round(y * 100) / 100;
    delete cmd.anchor;
    delete cmd.side;
    resolved.push(cmd);
  }
  return resolved;
}

/** A `CommandResolver` bound to a services bag, for `foldToolCalls`. */
export function makeCommandResolver(
  services: RenderServices,
): CommandResolver {
  return (doc, commands, metadata) =>
    resolveCommands(
      doc,
      commands,
      turnAnchorsFromMetadata(metadata),
      services,
    );
}
