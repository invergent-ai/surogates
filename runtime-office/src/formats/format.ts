import type { EditorPage } from "../connector.js";
import { ContentError } from "../markdown.js";

/** A request to an editor script: `scope` inside it. */
export type Scope = Record<string, unknown>;

/** What a batch came to: each op's result, or the refusal that stopped it before anything applied. */
export type Outcome = { results: unknown[] } | { refused: unknown };

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
