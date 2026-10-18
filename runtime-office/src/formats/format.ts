import type { EditorPage } from "../connector.js";
import { ContentError } from "../markdown.js";

/** A request to an editor script: `scope` inside it. */
export type Scope = Record<string, unknown>;

/**
 * What a batch came to: each op's result, with the document's length where the format measures it, or the refusal
 * that stopped it before anything applied.
 */
export type Outcome = { results: unknown[]; length?: { words: number; pages: number } } | { refused: unknown };

/** What a format runs its commands with. */
export interface EditContext {
  page: EditorPage;
  /** Runs the format's script with *scope*: its answer; throws EditorError, or Stalled when the editor stopped answering. */
  run(scope: Scope): Promise<unknown>;
}

/** One document type: its editor script, and how a read is asked and a batch is checked and run. */
export interface Format {
  script: string;
  /** Whether a batch runs inside a group of actions; PDF's editor cannot start one (it wedges). */
  grouped: boolean;
  /** The script's scope for a read; a ContentError says what is wrong with it. */
  readScope(body: Record<string, unknown>): Scope;
  /** The batch, checked (and for slides, measured); a ContentError names the op it refuses. */
  compile(raw: unknown, context: EditContext): Promise<unknown[]>;
  edit(context: EditContext, ops: unknown[]): Promise<Outcome>;
}

/** The editor could not do what was asked (not the agent's input, not a dead page). */
export class EditorError extends Error {}

/** The editor stopped answering a command: what it did is not known. */
export class Stalled extends Error {}

export function textArg(value: unknown, name: string): string {
  if (typeof value === "number") value = String(value);
  if (typeof value !== "string" || !value.trim()) throw new ContentError(`"${name}" is required.`);
  return value;
}

export function wholeNumber(value: unknown, name: string, min: number): number {
  const number = Number(value);
  if (!Number.isInteger(number) || number < min) throw new ContentError(`"${name}" is a whole number from ${min}.`);
  return number;
}

/** The agent's batch, each op checked by *one*; a ContentError names the op it refuses. */
export function checkBatch<T>(input: unknown, max: number, one: (raw: Record<string, unknown>) => T): T[] {
  if (!Array.isArray(input) || input.length === 0) throw new ContentError('"ops" is a non-empty list of ops.');
  if (input.length > max) throw new ContentError(`One call carries at most ${max} ops.`);
  return input.map((raw: unknown, index) => {
    try {
      if (typeof raw !== "object" || raw === null) throw new ContentError("an op is an object.");
      return one(raw as Record<string, unknown>);
    } catch (error) {
      if (error instanceof ContentError) throw new ContentError(`op ${index}: ${error.message}`);
      throw error;
    }
  });
}

/** The most characters a reply in a comment thread has: it states the result, and the conversation stays in the chat. */
export const MAX_THREAD_REPLY = 400;

/** A reply to a comment thread, or its resolve. The thread is named as the outline names it. */
export type CommentOp = { op: "reply"; thread: string; body: string } | { op: "resolve"; thread: string };

export const isCommentOp = (op: { op: unknown }): boolean => op.op === "reply" || op.op === "resolve";

/** *raw* checked as a reply or a resolve; null when it is another op. */
export function commentOp(raw: Record<string, unknown>): CommentOp | null {
  if (raw.op === "resolve") return { op: "resolve", thread: threadArg(raw.thread) };
  if (raw.op !== "reply") return null;
  const thread = threadArg(raw.thread);
  const body = textArg(raw.body, "body");
  if (body.length > MAX_THREAD_REPLY) throw new ContentError("a thread reply states just the result, in one or two sentences: keep the rest for the chat");
  return { op: "reply", thread, body };
}

/** *ops*, its replies and resolves alone in it or absent: a spreadsheet, deck or PDF changes a thread in a call of its own. */
export function commentsAlone<T extends { op: unknown }>(ops: T[]): T[] {
  if (ops.some(isCommentOp) && !ops.every(isCommentOp)) throw new ContentError("reply and resolve go in a call of their own, with no other op beside them.");
  return ops;
}

// A thread's id as the outline shows it: a number is taken as its string.
function threadArg(value: unknown): string {
  const thread = textArg(value, "thread");
  if (thread.length > 64) throw new ContentError('"thread" is a comment thread\'s id, as the outline shows it.');
  return thread;
}
