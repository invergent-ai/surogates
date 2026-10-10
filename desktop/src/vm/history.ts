// A request to a folder's history, which git in the guest runs (surogates/sandbox/local_history.py),
// and its answer, checked on this computer before anything is done with it (spec, Section 13,
// "The user's computer"). The guest is not trusted: a commit or blob id is forty hex digits, a
// path is one file's name, who changed a file is one of three shapes, and each answer is built
// again from its own fields alone, so nothing else the guest said goes on. Why a history did not
// answer is one of its codes, which whoever asked goes by; the words beside it are a person's.
//
// What the guest can have this computer hold is one line of the control port, which the link
// closes past 8 MiB, before the line is read as anything (control.ts, MAX_LINE_BYTES). Of what
// such a line holds, a list is counted before any of it is looked at, and a word is measured
// before it is kept.

import { landable } from "../files/land.js";
import type { Outcome } from "../link/protocol.js";
import type { Place } from "./manager.js";

export interface HistoryRequest {
  place: Place;
  thread: string; // the thread whose copy it is: its session's id
  user: string; // who started the thread: the folder's first commit, and its pickups, are theirs
  action: string;
  args: Record<string, unknown>;
}

// A session's id, and a user's as the server names one.
export const THREAD = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export const USER = /^[A-Za-z0-9_.@-]{1,128}$/;

// Why a history did not answer, in a word that is never changed. The history's own
// (surogates/sandbox/history.py and local_history.py):
//   failed             git, or the system under it, did not do what was asked
//   history_refused    the folder's history is not what the platform wrote: nothing is read from it
//   conflict           main, or a ref, moved in the history since the landing began
//   no_whole_copy      the thread's copy is gone, or its making was cut short: its next open makes it
//   name_not_utf8      a file's name is not UTF-8: nothing lands until the file is renamed
//   not_a_request      the request is none the history takes
//   record_unfinished  a landing of the thread's is in the history, and its copy could not be made the landing's files
//   move_unfinished    the copy's move to main was cut, and could not be finished
//   landing_unsettled  the landing was neither recorded nor put back whole: what it kept is not to be forgotten
//   not_on_base        the snapshot was taken on another base than the copy's is now: the copy is not put back to it
// and the agent's (guest/places.ts):
//   no_answer          the history ended without an answer: its bound passed, it was stopped, or it wrote none
const SAID = [
  "failed", "history_refused", "conflict", "no_whole_copy", "name_not_utf8", "not_a_request", "record_unfinished", "move_unfinished",
  "landing_unsettled", "not_on_base", "no_answer",
] as const;
// And this computer's own, for what the guest sent in an answer's place and it would not take.
export type HistoryCode = (typeof SAID)[number] | "not_an_answer";
// What the agent itself answers a request it did not run with: a place that is not in the guest, a
// request it cannot take, an id still running. A cancel and a sandbox that stopped are this computer's
// to say (control.ts), never the guest's.
const AGENTS = ["unavailable", "value", "other"] as const;

export const NOT_A_REQUEST: Outcome = { error: { type: "value", message: "This request names no thread, user or action of a history's" } };
const REFUSED: Outcome = {
  error: { type: "history", code: "not_an_answer", message: "This computer's sandbox answered what is not a history's answer, so it was not used" },
};

// The longest path Linux takes, and the most files one answer names: a folder with more has no history.
const PATH_UNITS = 4_096;
const MAX_FILES = 50_000;
const MESSAGE_UNITS = 2_000;

/** Whether *request* names a thread, a user and an action as a history's request does: nothing else is ever sent to the guest. */
export function named({ thread, user, action, args }: HistoryRequest): boolean {
  if (typeof thread !== "string" || typeof user !== "string" || typeof action !== "string") return false;
  return THREAD.test(thread) && USER.test(user) && typeof args === "object" && args !== null && !Array.isArray(args);
}

// A value as an answer may hold it, or undefined for anything else.
type Parse<T> = (value: unknown) => T | undefined;

const ID = /^[0-9a-f]{40}$/;
const id: Parse<string> = (value) => (typeof value === "string" && value.length === 40 && ID.test(value) ? value : undefined);
const idOrNull: Parse<string | null> = (value) => (value === null ? null : id(value));
// A file as git names it: a path from the folder's top, with no part that leads out of it, in text that is UTF-8's to carry.
const path: Parse<string> = (value) => {
  if (typeof value !== "string" || value === "" || value.length > PATH_UNITS || value.includes("\0") || value.startsWith("/")) return undefined;
  if (!value.isWellFormed()) return undefined;
  return value.split("/").some((part) => part === "" || part === "." || part === "..") ? undefined : value;
};
// A file or a folder history leaves out, as git lists it: a folder's name ends in a slash.
const name: Parse<string> = (value) => (typeof value === "string" ? path(value.endsWith("/") ? value.slice(0, -1) : value) && value : undefined);
const count: Parse<number> = (value) => (typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined);
const flag: Parse<boolean> = (value) => (typeof value === "boolean" ? value : undefined);
const text: Parse<string> = (value) => (typeof value === "string" && value.length <= PATH_UNITS ? value : undefined);
const oneOf = <T extends string>(...values: readonly T[]): Parse<T> => (value) => values.find((known) => known === value);
const said = oneOf(...SAID);
const agents = oneOf(...AGENTS);

function list<T>(item: Parse<T>): Parse<T[]> {
  return (value) => {
    if (!Array.isArray(value) || value.length > MAX_FILES) return undefined;
    const parsed: T[] = [];
    for (const entry of value) {
      const one = item(entry);
      if (one === undefined) return undefined;
      parsed.push(one);
    }
    return parsed;
  };
}

// An object of *shape*'s fields, each parsed; *optional* ones may be absent. Any other field is left out.
function fields<T extends Record<string, unknown>>(shape: { [K in keyof T]: Parse<T[K]> }, optional: ReadonlyArray<keyof T> = []): Parse<T> {
  return (value) => {
    if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
    const parsed: Record<string, unknown> = {};
    for (const [name, parse] of Object.entries(shape) as Array<[string, Parse<unknown>]>) {
      const given = (value as Record<string, unknown>)[name];
      if (given === undefined && optional.includes(name)) continue;
      const one = parse(given);
      if (one === undefined) return undefined;
      parsed[name] = one;
    }
    return parsed as T;
  };
}

const either = <A, B>(first: Parse<A>, second: Parse<B>): Parse<A | B> => (value) => first(value) ?? second(value);

// One file's two versions, as git names them: null where there is none.
const version = fields({ path, before: idOrNull, after: idOrNull });
// Who changed a file a landing leaves out: you, another thread, or a routine.
const who = either(
  fields({ kind: oneOf("you") }),
  either(fields({ kind: oneOf("thread"), id: text, title: text }), fields({ kind: oneOf("routine"), name: text })),
);
const held = fields({ path, reason: oneOf("changed", "shape", "with"), before: idOrNull, after: idOrNull, by: who }, ["by"]);
type Version = NonNullable<ReturnType<typeof version>>;
// What a landing leaves out, and why: the guest's three reasons, or this computer's own.
type Held = Version & { reason: "changed" | "shape" | "with" | "protected"; by?: NonNullable<ReturnType<typeof who>> };

// A turn's commit: what it would apply, and what it leaves out. A file a landing may not write on
// this computer, a name whose change could run code here (files/protect.ts), is left out by
// decision before the first apply, whatever the guest's git said: it stays in the thread's
// copy and in history, and the report names it.
const turn: Parse<unknown> = (value) => {
  const answer = fields({
    commit: idOrNull, base: id, changes: list(version), overlapped: list(held), excluded: list(name), repositories: list(name), not_taken: list(path),
  })(value);
  if (answer === undefined) return undefined;
  const refused = answer.changes.filter((change: Version) => !landable(change.path));
  if (refused.length === 0) return answer;
  const overlapped: Held[] = [...answer.overlapped, ...refused.map((change: Version) => ({ ...change, reason: "protected" as const }))];
  return { ...answer, changes: answer.changes.filter((change: Version) => landable(change.path)), overlapped: overlapped.sort((a, b) => (a.path < b.path ? -1 : 1)) };
};

// Each action's answer (local_history.py's _ACTIONS).
const ANSWERS: Record<string, Parse<unknown>> = {
  // *set_asides*: the snapshots of what the copy held of its own when a record or a move made it other files,
  // the oldest first, at every open while the thread's repository holds any. A folder with no history says
  // why: more files than history tracks, or a name in it that is not UTF-8.
  open: either(
    fields({ copy: oneOf("made", "moved", "kept"), set_asides: list(id) }, ["set_asides"]),
    fields({ history: oneOf("off"), reason: oneOf("cap", "names") }),
  ),
  changed: fields({ paths: list(path) }),
  snapshot: fields({ hash: id }),
  restore: fields({}),
  // *landing*: the saga's landing where main holds it, the only proof that it was pushed; *hidden*: it was not
  // found, and a pruning's cut hid where it would be. *missing*: those of the commits asked for that the
  // history does not hold, which no fetch can bring.
  fetch: fields({ main: idOrNull, landing: idOrNull, hidden: flag, packs: count, missing: list(id) }),
  pickup: fields({ main: idOrNull, commit: idOrNull, picked_up: list(version), packs: count }),
  commit: turn,
  // *set_aside*: a snapshot of what the copy held beyond its turn, where making it the landing's files took that away.
  record: fields({ commit: id, set_aside: idOrNull }),
  // A kept turn: its commit, and the helpers' files its copy left as it had them, as a turn's commit names them.
  keep: fields({ commit: id, not_taken: list(path) }),
  // The landing where main holds it, recorded; null where each file it applied is in the folder as it was before.
  forget: fields({ landing: idOrNull }),
};

function taken(action: string, outcome: unknown): Outcome {
  if (typeof outcome !== "object" || outcome === null || Array.isArray(outcome)) return REFUSED;
  if ("error" in outcome) {
    const { error } = outcome;
    if (typeof error !== "object" || error === null) return REFUSED;
    const { type, code, message } = error as { type?: unknown; code?: unknown; message?: unknown };
    if (typeof message !== "string") return REFUSED;
    const words = message.slice(0, MESSAGE_UNITS);
    if (type !== "history") {
      const own = agents(type);
      return own === undefined ? REFUSED : { error: { type: own, message: words } };
    }
    // A refusal whose code is none of a history's is no refusal of one, whatever its words say.
    const known = said(code);
    return known === undefined ? REFUSED : { error: { type, code: known, message: words } };
  }
  const parse = Object.hasOwn(ANSWERS, action) ? ANSWERS[action] : undefined;
  const answer = parse?.((outcome as { ok?: unknown }).ok);
  return answer === undefined ? REFUSED : { ok: answer };
}

/**
 * What the guest sent as the outcome of *action*, as this computer takes it: its own fields alone, or
 * a refusal. Nothing of its shape is taken for granted, so it never throws: what is no outcome at
 * all is refused as an answer that is none is. An error of the type "history" always has a code,
 * one of HistoryCode; any other error is the agent's own, of a type it has, and has none.
 */
export function checked(action: string, outcome: unknown): Outcome {
  try {
    return taken(action, outcome);
  } catch {
    // Only what is no data at all can throw when it is read: no line of the control port parses to it.
    return REFUSED;
  }
}
