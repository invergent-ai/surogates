import { checkBatch } from "./formats/format.js";
import { compileContent, ContentError, type Block } from "./markdown.js";

export type Found = { find: string; nth: number };
export type Target = Found | { block: string; hash: string } | { pending: string } | { end: true };
export type Side = "before" | "after";

/** An op as the editor script runs it. */
export type EditorOp =
  | { op: "insert"; target: Target; side: Side; blocks: Block[] }
  | { op: "replace"; target: Target; blocks: Block[] }
  | { op: "reword"; target: Found; text: string }
  | { op: "delete"; target: Target }
  | { op: "move"; target: Target; to: Target; side: Side }
  | { op: "comment"; anchor: Found; body: string }
  | { op: "reply"; thread: string; body: string }
  | { op: "resolve"; thread: string };

export const MAX_OPS = 50;

type Raw = Record<string, unknown>;

/** The agent's batch, checked and compiled; a ContentError names the op it refuses. */
export function compileOps(input: unknown): EditorOp[] {
  return checkBatch(input, MAX_OPS, compileOp);
}

function compileOp(raw: Raw): EditorOp {
  switch (raw.op) {
    case "insert":
      return { op: "insert", target: target(raw.target, true), side: side(raw.side), blocks: markdownOf(raw.content) };
    case "replace": {
      const content = (raw.content ?? {}) as Raw;
      const where = target(raw.target, false);
      if (typeof content.text === "string") {
        if (!("find" in where)) {
          throw new ContentError('"text" content re-words quoted words: give a {"find": …} target, or markdown content to replace a block.');
        }
        return { op: "reword", target: where, text: content.text };
      }
      if ("find" in where) {
        throw new ContentError('to change quoted words give {"text": …} content; markdown content replaces a whole block or pending block.');
      }
      return { op: "replace", target: where, blocks: markdownOf(content) };
    }
    case "delete":
      return { op: "delete", target: target(raw.target, false) };
    case "move": {
      const what = target(raw.target, false);
      if ("find" in what) throw new ContentError("move takes a block or a pending block, not quoted words.");
      return { op: "move", target: what, to: target(raw.to, true), side: side(raw.side) };
    }
    case "comment": {
      const anchor = target(raw.anchor, false);
      if (!("find" in anchor)) throw new ContentError('a comment is anchored to quoted words: {"find": "…"}.');
      return { op: "comment", anchor, body: text(raw.body, "body", 2000) };
    }
    case "reply":
      return { op: "reply", thread: thread(raw.thread), body: text(raw.body, "body", 2000) };
    case "resolve":
      return { op: "resolve", thread: thread(raw.thread) };
    default:
      throw new ContentError(`unknown op ${JSON.stringify(raw.op)}; use insert, replace, delete, move, comment, reply or resolve.`);
  }
}

function markdownOf(content: unknown): Block[] {
  const raw = (content ?? {}) as Raw;
  if (typeof raw.markdown !== "string" || !raw.markdown.trim()) {
    throw new ContentError('content is {"markdown": "…", "blocks": {…}}, with markdown to write.');
  }
  const blocks = raw.blocks === undefined ? {} : raw.blocks;
  if (typeof blocks !== "object" || blocks === null || Array.isArray(blocks)) throw new ContentError('"blocks" maps names to typed blocks.');
  const compiled = compileContent(raw.markdown, blocks as Record<string, unknown>);
  if (!compiled.length) throw new ContentError("the markdown writes nothing.");
  return compiled;
}

function target(input: unknown, allowEnd: boolean): Target {
  const raw = (input ?? {}) as Raw;
  if (raw.end === true) {
    if (!allowEnd) throw new ContentError('{"end": true} is a place to insert at, not something to change.');
    return { end: true };
  }
  if (typeof raw.find === "string") {
    if (!raw.find.trim()) throw new ContentError('"find" quotes words that are in the document.');
    const nth = raw.nth === undefined ? 1 : Number(raw.nth);
    if (!Number.isInteger(nth) || nth < 1) throw new ContentError('"nth" counts matches from 1.');
    return { find: raw.find, nth };
  }
  if (typeof raw.block === "string" || typeof raw.block === "number") {
    if (typeof raw.hash !== "string" || !raw.hash) throw new ContentError("a block target carries the hash the block had when you read it.");
    return { block: String(raw.block), hash: raw.hash };
  }
  if (typeof raw.pending === "string" && raw.pending) return { pending: raw.pending };
  throw new ContentError('a target is {"find": "…"}, {"block": "<id>", "hash": "<hash>"}, {"pending": "<key>"} or, to insert at the end, {"end": true}.');
}

function side(input: unknown): Side {
  if (input === undefined) return "after";
  if (input === "before" || input === "after") return input;
  throw new ContentError('"side" is "before" or "after".');
}

// Thread ids are all digits: a number is taken as its string, as a block id is.
function thread(input: unknown): string {
  return text(typeof input === "number" ? String(input) : input, "thread", 64);
}

function text(input: unknown, name: string, max: number): string {
  if (typeof input !== "string" || !input.trim()) throw new ContentError(`"${name}" is required.`);
  if (input.length > max) throw new ContentError(`"${name}" is at most ${max} characters.`);
  return input;
}
