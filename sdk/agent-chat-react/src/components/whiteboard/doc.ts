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

export interface WbDoc {
  version: 1;
  objects: WbObject[];
  /**
   * Highest event id folded in. The persistence layer replays only tool
   * calls newer than this after loading the saved document.
   */
  lastEventId: number;
}

export function emptyDoc(): WbDoc {
  return { version: 1, objects: [], lastEventId: 0 };
}

let localCounter = 0;
function nextId(origin: string): string {
  localCounter += 1;
  return `${origin}:${localCounter}`;
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
        w: cmd.w as number | undefined,
        h: cmd.h as number | undefined,
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
        w: Number(cmd.w),
        h: Number(cmd.h),
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
  for (const cmd of commands) {
    if (typeof cmd !== "object" || cmd === null) continue;
    const obj = toObject(cmd as Record<string, unknown>, origin);
    if (obj) added.push(obj);
  }
  // Advance the cursor even when nothing survived validation, or the
  // persistence tail replays the same dead call on every load.
  if (added.length === 0) {
    return { ...doc, lastEventId: nextEventId };
  }
  return {
    version: 1,
    objects: [
      ...doc.objects.map((o) => (o.selected ? { ...o, selected: false } : o)),
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
export function foldToolCalls(
  doc: WbDoc,
  messages: AgentChatMessage[],
): WbDoc {
  const seen = new Set(doc.objects.map((o) => o.origin));
  let next = doc;
  for (const message of messages) {
    for (const call of message.toolCalls ?? []) {
      if (call.toolName !== DRAW_TOOL) continue;
      if (seen.has(call.id)) continue;
      seen.add(call.id);
      let parsed: unknown;
      try {
        parsed = JSON.parse(call.args);
      } catch {
        continue;
      }
      const commands = (parsed as { commands?: unknown } | null)?.commands;
      if (!Array.isArray(commands)) continue;
      next = applyCommands(next, commands, next.lastEventId, call.id);
    }
  }
  return next;
}
