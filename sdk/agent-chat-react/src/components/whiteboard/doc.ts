import type { AgentChatMessage } from "../../types";
import { normalize as normalizeDraw } from "./draw";

/**
 * The canvas is infinite: no edges, an arbitrary origin, and freely
 * negative coordinates. This is only a sanity bound so a malformed
 * command cannot place an object somewhere no viewport can reach.
 * Must match `COORD_LIMIT` in surogates/whiteboard/commands.py.
 */
export const COORD_LIMIT = 1_000_000;

/** Default text line-height multiplier when the model omits one. */
const DEFAULT_LINE_HEIGHT = 1.35;

/** The tool whose calls carry canvas commands. */
const DRAW_TOOL = "whiteboard_draw";

export interface WbBase {
  id: string;
  /**
   * Source tool-call id, or "local" for the user's own edits. Folding is
   * keyed on this so a re-delivered event cannot duplicate objects: the
   * SSE stream and the reconciliation poll both replay the same calls.
   */
  origin: string;
  selected: boolean;
}

export type WbObject = WbBase &
  (
    | { kind: "ink"; pts: number[]; width: number; color: string }
    | {
        kind: "draw";
        origin_: [number, number];
        types: string[];
        items: number[][];
        width?: number;
        tension?: number;
        closed?: number[];
        fill?: number[];
        arrows?: number[];
      }
    | {
        kind: "text";
        x: number;
        y: number;
        text: string;
        fontSize: number;
        maxWidth: number;
        lineHeight: number;
      }
    | { kind: "formula"; x: number; y: number; latex: string; fontSize: number }
    | {
        kind: "artifact";
        x: number;
        y: number;
        w: number;
        h: number;
        artifactId: string;
        version?: number;
      }
    | {
        /**
         * Empty space the user reserved for the answer. The universal
         * "where": maths (`∫…dx = [slot]`), text (`H [slot] USE`), a
         * drawing ("the cat goes here"). Filling it removes it.
         */
        kind: "slot";
        x: number;
        y: number;
        w: number;
        h: number;
        /** What the user typed about what belongs here, if anything. */
        hint?: string;
      }
    | {
        kind: "erase";
        mode: "rect" | "path";
        x?: number;
        y?: number;
        w?: number;
        h?: number;
        points?: number[][];
        size?: number;
      }
  );

/**
 * What a cluster of the user's ink says, read once and kept.
 *
 * Keyed by the exact strokes it covers (see `readingKey`), so adding a
 * stroke to an expression changes its key and it reads as new -- which
 * it is: the meaning changed. A reading the user corrected outranks any
 * the agent produces later.
 */
export interface WbReading {
  text: string;
  source: "agent" | "user";
  strokeIds: string[];
}

export interface WbDoc {
  version: 1;
  objects: WbObject[];
  /**
   * Transcriptions of the user's ink by cluster key. Optional so boards
   * saved before readings existed still load; absent means "none".
   */
  readings?: Record<string, WbReading>;
  /**
   * Highest event id folded in. The persistence layer replays only tool
   * calls newer than this after loading the saved document.
   */
  lastEventId: number;
  /**
   * Ids of the `whiteboard_draw` calls already applied.
   *
   * Kept explicitly rather than derived from the surviving objects'
   * origins: deleting an object would otherwise erase the only record
   * that its call had been consumed, and the next load would draw it
   * again. A deletion has to outlive the thing deleted.
   */
  folded: string[];
}

export function emptyDoc(): WbDoc {
  return { version: 1, objects: [], lastEventId: 0, folded: [], readings: {} };
}

/** The key a cluster's reading is stored under: its strokes, in order. */
export function readingKey(strokeIds: readonly string[]): string {
  return [...strokeIds].sort().join("|");
}

/**
 * Record the agent's transcriptions of labelled ink.
 *
 * `readings` is the `readings` array of a `whiteboard_draw` call --
 * `{mark, text}` pairs naming A-labels from the turn's marks -- and
 * `metadata` is that turn's user message metadata, which carries each
 * mark's stroke ids. A reading the user has corrected is never
 * overwritten. Readings whose strokes are gone are dropped: the ink
 * they described no longer exists.
 */
export function applyReadings(
  doc: WbDoc,
  readings: unknown[],
  metadata: Record<string, unknown> | undefined,
): WbDoc {
  const strokesByMark = new Map<string, string[]>();
  const wb = metadata?.whiteboard as Record<string, unknown> | undefined;
  if (Array.isArray(wb?.marks)) {
    for (const raw of wb.marks) {
      const m = raw as Record<string, unknown>;
      if (typeof m?.id === "string" && Array.isArray(m.strokes)) {
        strokesByMark.set(m.id, m.strokes.filter((s) => typeof s === "string"));
      }
    }
  }
  const alive = new Set(doc.objects.map((o) => o.id));
  const next: Record<string, WbReading> = {};
  for (const [key, reading] of Object.entries(doc.readings ?? {})) {
    if (reading.strokeIds.every((id) => alive.has(id))) next[key] = reading;
  }
  for (const raw of readings) {
    if (!raw || typeof raw !== "object") continue;
    const r = raw as Record<string, unknown>;
    if (typeof r.mark !== "string" || typeof r.text !== "string") continue;
    const strokeIds = strokesByMark.get(r.mark);
    if (!strokeIds?.length) continue;
    const text = r.text.trim();
    if (!text) continue;
    const key = readingKey(strokeIds);
    if (next[key]?.source === "user") continue;
    next[key] = { text, source: "agent", strokeIds };
  }
  return { ...doc, readings: next };
}

/** Store the user's own correction of what a cluster says. */
export function correctReading(
  doc: WbDoc,
  strokeIds: readonly string[],
  text: string,
): WbDoc {
  const key = readingKey(strokeIds);
  const readings = { ...(doc.readings ?? {}) };
  const trimmed = text.trim();
  if (trimmed) {
    readings[key] = { text: trimmed, source: "user", strokeIds: [...strokeIds] };
  } else {
    delete readings[key];
  }
  return { ...doc, readings };
}

/**
 * Object ids are unique across everything that mints them and across
 * page loads. Two counters used to run side by side -- strokes in
 * `input.ts`, slots and text here -- so the first slot on a board was
 * `local:1`, the same id as the first stroke, and selecting one
 * selected both. And a counter restarts on reload, so new objects
 * collided with the saved board too. The per-load token is what keeps
 * ids stable once persisted and distinct across sessions.
 */
const LOAD_TOKEN = Math.random().toString(36).slice(2, 8);
let localCounter = 0;
function nextId(origin: string): string {
  localCounter += 1;
  return `${origin}:${LOAD_TOKEN}${localCounter}`;
}

/** A fresh id for a user-authored object: strokes, slots, text. */
export function nextLocalId(): string {
  return nextId("local");
}

/**
 * A size the model may have spelled `width`/`height`.
 *
 * The command vocabulary is mixed — `draw` has a stroke `width`,
 * `write_text` has `maxWidth`, `erase` and `place_artifact` have `w`/`h`
 * — so the long spelling turns up on the short-spelled commands. The
 * validator accepts it, and this is the half that makes it draw: without
 * it an aliased erase passes validation and then rubs out nothing.
 */
function size(cmd: Record<string, unknown>, key: "w" | "h"): unknown {
  const alias = key === "w" ? "width" : "height";
  return cmd[key] ?? cmd[alias];
}

/** Convert one command into an object, or null to skip it. */
function toObject(
  cmd: Record<string, unknown>,
  origin: string,
): WbObject | null {
  const base = { id: nextId(origin), origin, selected: true };
  switch (cmd.tool) {
    case "write_text":
      return {
        ...base,
        kind: "text",
        x: Number(cmd.x),
        y: Number(cmd.y),
        text: String(cmd.text ?? ""),
        fontSize: Number(cmd.fontSize),
        maxWidth: Number(cmd.maxWidth),
        lineHeight: Number(cmd.lineHeight ?? DEFAULT_LINE_HEIGHT),
      };
    case "draw_formula":
      return {
        ...base,
        kind: "formula",
        x: Number(cmd.x),
        y: Number(cmd.y),
        latex: String(cmd.latex ?? ""),
        fontSize: Number(cmd.fontSize),
      };
    case "draw":
      // The draw normalizer is authoritative for geometry: if it
      // rejects the command the renderer would produce nothing, so drop
      // it here rather than carry an object that can never paint.
      if (normalizeDraw(cmd, COORD_LIMIT) === null) return null;
      return {
        ...base,
        kind: "draw",
        origin_: cmd.origin as [number, number],
        types: cmd.types as string[],
        items: cmd.items as number[][],
        width: cmd.width as number | undefined,
        tension: cmd.tension as number | undefined,
        closed: cmd.closed as number[] | undefined,
        fill: cmd.fill as number[] | undefined,
        arrows: cmd.arrows as number[] | undefined,
      };
    case "erase":
      if (cmd.mode !== "rect" && cmd.mode !== "path") return null;
      return {
        ...base,
        kind: "erase",
        mode: cmd.mode,
        x: cmd.x as number | undefined,
        y: cmd.y as number | undefined,
        w: size(cmd, "w") as number | undefined,
        h: size(cmd, "h") as number | undefined,
        points: cmd.points as number[][] | undefined,
        size: cmd.size as number | undefined,
      };
    case "place_artifact":
      if (typeof cmd.artifact_id !== "string" || !cmd.artifact_id) return null;
      return {
        ...base,
        kind: "artifact",
        artifactId: cmd.artifact_id,
        x: Number(cmd.x),
        y: Number(cmd.y),
        w: Number(size(cmd, "w")),
        h: Number(size(cmd, "h")),
      };
    default:
      return null;
  }
}

/**
 * Append one `whiteboard_draw` call's commands.
 *
 * New objects arrive as the active selection and clear the previous one,
 * so the user can immediately drag, resize or delete what the agent just
 * drew — this is what replaces PenEcho's unconfirmed-draft layer.
 *
 * Returns a new document; *doc* is never mutated.
 */
export function applyCommands(
  doc: WbDoc,
  commands: unknown[],
  eventId: number,
  origin = `evt${eventId}`,
): WbDoc {
  const nextEventId = Math.max(doc.lastEventId, eventId);
  if (!Array.isArray(commands)) return doc;

  const added: WbObject[] = [];
  // Origins this call supersedes. The agent can only add objects, so
  // without this a corrected answer is drawn on top of the wrong one —
  // `erase` paints white, it does not delete. One turn stacked four
  // answers on a single spot.
  const superseded = new Set<string>();
  for (const cmd of commands) {
    if (typeof cmd !== "object" || cmd === null) continue;
    const record = cmd as Record<string, unknown>;
    const obj = toObject(record, origin);
    if (obj) added.push(obj);
    // Honoured even when the command itself was unusable: the intent to
    // retract is independent of whether the replacement could be drawn,
    // and leaving the old one behind on a failed replace is the worse
    // of the two outcomes.
    if (typeof record.replaces === "string" && record.replaces) {
      superseded.add(record.replaces);
    }
  }
  // A command that filled a slot takes the slot with it: the space was
  // reserved for exactly this, and an empty box left behind would read
  // as still waiting for an answer.
  const filled = new Set<string>();
  for (const cmd of commands) {
    if (cmd && typeof cmd === "object") {
      const f = (cmd as Record<string, unknown>).fillsSlot;
      if (typeof f === "string" && f) filled.add(f);
    }
  }
  // Advance the cursor even when nothing survived validation, or the
  // persistence tail replays the same dead call on every load.
  if (added.length === 0 && superseded.size === 0 && filled.size === 0) {
    return { ...doc, lastEventId: nextEventId };
  }
  const kept = doc.objects.filter(
    (o) => !superseded.has(o.origin) && !(o.kind === "slot" && filled.has(o.id)),
  );
  return {
    ...doc,
    version: 1,
    objects: [
      ...kept.map((o) => (o.selected ? { ...o, selected: false } : o)),
      ...added,
    ],
    lastEventId: nextEventId,
  };
}

/**
 * Fold every `whiteboard_draw` tool call in *messages* into *doc*.
 *
 * Idempotent on the tool-call id: the SSE stream and the reconciliation
 * poll deliver the same events, so a re-fold must not duplicate objects.
 */
/**
 * Turns an anchored command list into an absolute one before it is
 * applied. `anchors` is the metadata of the user message that started
 * the call's turn — the resolver reads `latest`/`selection` rects from
 * it. Injected rather than imported so this module stays free of the
 * render dependency; see `layout.ts` for the real one.
 */
export type CommandResolver = (
  doc: WbDoc,
  commands: unknown[],
  anchors: Record<string, unknown> | undefined,
) => unknown[];

export function foldToolCalls(
  doc: WbDoc,
  messages: AgentChatMessage[],
  resolve?: CommandResolver,
): WbDoc {
  const seen = new Set(doc.folded);
  const newlyFolded: string[] = [];
  let next = doc;
  // The metadata of the user message governing the calls that follow
  // it: relational anchors ("latest", "selection") are rects captured
  // at that Ask, and they ride on the message so replay from the event
  // log resolves exactly like the live fold did.
  let turnMetadata: Record<string, unknown> | undefined;
  for (const message of messages) {
    if (message.role === "user") {
      turnMetadata = message.metadata;
      continue;
    }
    for (const call of message.toolCalls ?? []) {
      if (call.toolName !== DRAW_TOOL) continue;
      // A call the server rejected must not stay on the board. The call
      // streams before its result, so it may already have been folded
      // by the time the error lands; every fold therefore re-checks and
      // removes what a rejected call drew. Without this each validator
      // retry double-drew: the rejected version and the corrected one.
      if (call.result?.startsWith("Error:")) {
        if (!seen.has(call.id)) {
          seen.add(call.id);
          newlyFolded.push(call.id);
        }
        if (next.objects.some((o) => o.origin === call.id)) {
          next = {
            ...next,
            objects: next.objects.filter((o) => o.origin !== call.id),
          };
        }
        continue;
      }
      if (seen.has(call.id)) continue;
      seen.add(call.id);
      // Recorded even when the payload turns out to be unusable, or the
      // dead call is retried on every load for the life of the session.
      newlyFolded.push(call.id);
      let parsed: unknown;
      try {
        parsed = JSON.parse(call.args);
      } catch {
        continue;
      }
      const rawCommands = (parsed as { commands?: unknown } | null)?.commands;
      if (!Array.isArray(rawCommands)) continue;
      const commands = resolve
        ? resolve(next, rawCommands, turnMetadata)
        : rawCommands;
      next = applyCommands(next, commands, next.lastEventId, call.id);
      const readings = (parsed as { readings?: unknown } | null)?.readings;
      if (Array.isArray(readings) && readings.length > 0) {
        next = applyReadings(next, readings, turnMetadata);
      }
    }
  }
  if (newlyFolded.length === 0) return next;
  return { ...next, folded: [...next.folded, ...newlyFolded] };
}

// ---------------------------------------------------------------------
// Geometry edits
//
// The agent's output arrives as the active selection, which is what
// replaces PenEcho's unconfirmed-draft layer — but only if the selection
// can actually be moved and resized. These are the operations behind
// that promise.
// ---------------------------------------------------------------------

/** Shift one object by a logical delta. */
export function translateObject(
  obj: WbObject,
  dx: number,
  dy: number,
): WbObject {
  switch (obj.kind) {
    case "ink":
      return {
        ...obj,
        pts: obj.pts.map((v, i) => (i % 2 === 0 ? v + dx : v + dy)),
      };
    case "draw":
      // Item coordinates are offsets from origin, so moving the origin
      // moves the whole primitive set and nothing else has to change.
      return { ...obj, origin_: [obj.origin_[0] + dx, obj.origin_[1] + dy] };
    case "text":
    case "formula":
    case "artifact":
    case "slot":
      return { ...obj, x: obj.x + dx, y: obj.y + dy };
    case "erase":
      return {
        ...obj,
        x: obj.x === undefined ? undefined : obj.x + dx,
        y: obj.y === undefined ? undefined : obj.y + dy,
        points: obj.points?.map(([x, y]) => [x + dx, y + dy]),
      };
  }
}

/**
 * Scale one object about *anchor* by independent x/y factors.
 *
 * Kinds with an intrinsic type size scale that instead of their box,
 * so the glyphs are never stretched on one axis. Text takes its font
 * from the vertical factor and its wrap width from the horizontal one:
 * the box then follows the corner on both axes. A formula has no width
 * to stretch, so it follows whichever axis the pointer moved most --
 * pulling a handle in any direction resizes it.
 */
export function scaleObject(
  obj: WbObject,
  sx: number,
  sy: number,
  anchor: { x: number; y: number },
): WbObject {
  const px = (v: number) => anchor.x + (v - anchor.x) * sx;
  const py = (v: number) => anchor.y + (v - anchor.y) * sy;
  const uniform = Math.max(0.05, Math.min(sx, sy));
  const dominant =
    Math.abs(Math.log(Math.max(sx, 1e-6))) >=
    Math.abs(Math.log(Math.max(sy, 1e-6)))
      ? sx
      : sy;

  switch (obj.kind) {
    case "ink":
      return {
        ...obj,
        pts: obj.pts.map((v, i) => (i % 2 === 0 ? px(v) : py(v))),
        width: Math.max(1, obj.width * uniform),
      };
    case "draw":
      return {
        ...obj,
        origin_: [px(obj.origin_[0]), py(obj.origin_[1])],
        items: obj.items.map((item) =>
          // Offsets scale in place; they are relative to the origin,
          // which has already moved.
          item.map((v, i) => Math.round(v * (i % 2 === 0 ? sx : sy))),
        ),
      };
    case "text":
      return {
        ...obj,
        x: px(obj.x),
        y: py(obj.y),
        // Width is the wrap width, so it takes the horizontal factor;
        // the glyphs themselves scale uniformly.
        maxWidth: Math.max(16, obj.maxWidth * sx),
        fontSize: Math.max(4, obj.fontSize * sy),
      };
    case "formula":
      return {
        ...obj,
        x: px(obj.x),
        y: py(obj.y),
        fontSize: Math.max(4, obj.fontSize * dominant),
      };
    case "artifact":
    case "slot":
      return {
        ...obj,
        x: px(obj.x),
        y: py(obj.y),
        w: Math.max(16, obj.w * sx),
        h: Math.max(16, obj.h * sy),
      };
    case "erase":
      return {
        ...obj,
        x: obj.x === undefined ? undefined : px(obj.x),
        y: obj.y === undefined ? undefined : py(obj.y),
        w: obj.w === undefined ? undefined : obj.w * sx,
        h: obj.h === undefined ? undefined : obj.h * sy,
        points: obj.points?.map(([x, y]) => [px(x), py(y)]),
      };
  }
}

/** A slot the user reserved for the answer, covering *rect*. */
export function makeSlotObject(
  rect: { x: number; y: number; w: number; h: number },
  hint?: string,
): WbObject {
  return {
    id: nextId("local"),
    origin: "local",
    selected: false,
    kind: "slot",
    x: rect.x,
    y: rect.y,
    w: rect.w,
    h: rect.h,
    ...(hint?.trim() ? { hint: hint.trim() } : {}),
  };
}

/** A user-authored text object placed at *pt*. */
export function makeTextObject(
  pt: { x: number; y: number },
  text: string,
  fontSize: number,
  maxWidth: number,
): WbObject {
  return {
    id: nextId("local"),
    origin: "local",
    selected: false,
    kind: "text",
    x: pt.x,
    y: pt.y,
    text,
    fontSize,
    maxWidth,
    lineHeight: DEFAULT_LINE_HEIGHT,
  };
}

/** Apply *edit* to every selected object, leaving the rest alone. */
export function mapSelected(
  doc: WbDoc,
  edit: (obj: WbObject) => WbObject,
): WbDoc {
  return {
    ...doc,
    objects: doc.objects.map((o) => (o.selected ? edit(o) : o)),
  };
}
