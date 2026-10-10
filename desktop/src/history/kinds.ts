// The kinds only a project thread's root has on this computer (spec, Section 13, "The user's computer"), and who may
// ask for them over the device link. The server's journal takes them by the same rules (surogates/devices/binding.py,
// THREAD_ACTIONS; operations.py, _its_own_turn), and the server checks every answer (surogates/devices/history.py):
//
//   checkpoint  a snapshot of the thread's copy before a step, or the copy put back to one at a stop: {action: "take",
//               reason} and {action: "restore", hash}, the history's snapshot and restore. Any session of the
//               thread's asks it, under checkpoint:<turn>:…, as each takes one before its step.
//   history     a step of the folder's history that git in the guest runs for the thread's copy
//               (surogates/sandbox/local_history.py): {action, ...its arguments}. The thread itself asks it, under
//               land:<turn>…; a turn's open also under open:<turn>….
//   land        a landing's look at the folder, its writes into it and their put-back, what a landing cut short left
//               put back, and its forgetting: the file helper's land kind (files/land.ts), in the landing's own host on
//               the folder (hosts/tool-hosts.ts). The thread itself asks it, under land:<turn>….
//
// Each is the worker's own, outside any tool call: a tool call's invocation is its event's number, and the user's own
// request starts "request:". None asks its user anything (binding/approvals.ts): what a landing applies is what the
// thread wrote in its copy, each write asked about there.

import { SAGA } from "../files/land.js";
import { FOLDER_UNAVAILABLE } from "../hosts/messages.js";
import type { BoundFolder } from "../hosts/tool-hosts.js";
import type { Operation, Outcome } from "../link/protocol.js";

// The actions each kind takes over the device link. The history's own close and drop are the app's to ask, and a
// snapshot's listing, a pruning and the file helper's other kinds are none of them.
export const THREAD_ACTIONS: ReadonlyMap<string, ReadonlySet<string>> = new Map([
  ["checkpoint", new Set(["take", "restore"])],
  ["history", new Set(["open", "changed", "fetch", "pickup", "commit", "record", "keep", "forget"])],
  ["land", new Set(["recover", "revisions", "apply", "unapply", "forget"])],
]);
export const THREAD_KINDS: ReadonlySet<string> = new Set(THREAD_ACTIONS.keys());

// Who asks for what, by the invocation's prefix: whether a session under the thread may ask under it, besides the
// thread itself, and the actions of each kind it takes. A new way of asking is a line of its own.
const ASKED: ReadonlyArray<{ prefix: string; under: boolean; takes: ReadonlyMap<string, ReadonlySet<string>> }> = [
  { prefix: "checkpoint:", under: true, takes: new Map([["checkpoint", THREAD_ACTIONS.get("checkpoint")!]]) },
  { prefix: "open:", under: false, takes: new Map([["history", new Set(["open"])]]) },
  { prefix: "land:", under: false, takes: new Map([["history", THREAD_ACTIONS.get("history")!], ["land", THREAD_ACTIONS.get("land")!]]) },
];

export const NOT_A_THREAD: Outcome = {
  error: { type: "unsupported", message: "This chat works in its folder itself: it has no copy of it, no history and nothing to land" },
};
export const NOT_ITS_TURN: Outcome = {
  error: { type: "refused", message: "Only a thread's own turn may take its snapshots, move its history or land its work on this computer" },
};

/**
 * Why *operation*, one of a thread's own kinds, is not run for the root bound as *bound*; null where it may run. Asked
 * before its user would be asked anything, and again where it runs. It reads no argument but the action.
 */
export function refusedOf(operation: Operation, bound: BoundFolder | undefined): Outcome | null {
  const { kind, sessionId, callingSessionId, invocationId, args } = operation;
  if (!bound) return FOLDER_UNAVAILABLE;
  if (bound.history === undefined) return NOT_A_THREAD;
  const asked = ASKED.find(({ prefix }) => invocationId.startsWith(prefix));
  const { action } = args;
  // A binding names its thread's copy, which is the root's own (binding/binder.ts).
  if (!asked || bound.history !== sessionId || typeof action !== "string" || asked.takes.get(kind)?.has(action) !== true) return NOT_ITS_TURN;
  return asked.under || callingSessionId === sessionId ? null : NOT_ITS_TURN;
}

// A snapshot's words, as its commit's title: at most as many characters as the server sends, counted as it counts them.
const REASON_CHARACTERS = 200;
const ID = /^[0-9a-f]{40}$/;
export const NOT_A_CHECKPOINT: Outcome = {
  error: { type: "value", message: `A checkpoint takes words of 1 to ${REASON_CHARACTERS} characters for its snapshot, or the commit to put the copy back to` },
};

/**
 * What the folder's history is asked for a checkpoint, by the history's own names: a take is its snapshot with the
 * words given, and a restore its restore of the commit the hash names. Null for arguments it does not take.
 */
export function checkpointOf(args: Record<string, unknown>): { action: string; args: Record<string, unknown> } | null {
  const { action, reason, hash } = args;
  if (action === "restore") return typeof hash === "string" && ID.test(hash) ? { action, args: { commit: hash } } : null;
  // Text git can make a commit's title of: no NUL, and nothing of half a character.
  const words = typeof reason === "string" && reason.length <= 2 * REASON_CHARACTERS && reason.isWellFormed() && !reason.includes("\0");
  if (action !== "take" || !words || reason === "" || [...reason].length > REASON_CHARACTERS) return null;
  return { action: "snapshot", args: { reason } };
}

// One apply a landing's forgetting names: its step, its file in the folder, and that file's versions before and after.
export interface Applied {
  step: number;
  path: string;
  before: string | null;
  after: string | null;
}

// The most applies one forgetting names: a turn's commit names no more changes than a history tracks files.
const MAX_APPLIED = 50_000;
const PATH_UNITS = 4_096;
export const NOT_A_FORGETTING_ASKED: Outcome = {
  error: { type: "value", message: "A landing's forgetting names its saga, and each apply that was sent for it: its step, its file and that file's two versions" },
};

/**
 * A landing's forgetting as asked (`land` `forget`): its saga, and *applied*, each apply that was sent for it, a step
 * once. Null for arguments that are none.
 */
export function forgettingOf(args: Record<string, unknown>): { saga: string; applied: Applied[] } | null {
  const { saga, applied } = args;
  if (typeof saga !== "string" || !SAGA.test(saga) || !Array.isArray(applied) || applied.length > MAX_APPLIED) return null;
  const version = (value: unknown): value is string | null => value === null || (typeof value === "string" && ID.test(value));
  const steps = new Set<number>();
  const named: Applied[] = [];
  for (const entry of applied as unknown[]) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) return null;
    const { step, path, before, after } = entry as Record<string, unknown>;
    if (typeof step !== "number" || !Number.isSafeInteger(step) || step < 0 || steps.has(step)) return null;
    if (typeof path !== "string" || path === "" || path.length > PATH_UNITS || path.includes("\0") || !version(before) || !version(after)) return null;
    steps.add(step);
    named.push({ step, path, before, after });
  }
  return { saga, applied: named };
}

/**
 * Why what a landing kept is not to be forgotten by the steps its forgetting names, *applied*, where its helper holds
 * *records*, of each step by its number with the file it names (files/land.ts, recordsOf): a step that is not among
 * them, or is named for another file. Null where every one is named. The helper's records are the proof of what was
 * applied in the folder; the steps named are the server's word.
 */
export function unrecorded(records: ReadonlyMap<number, string | null>, applied: readonly Applied[]): Outcome | null {
  const named = new Map(applied.map(({ step, path }) => [step, path]));
  for (const [step, path] of [...records].sort(([a], [b]) => a - b)) {
    if (named.has(step) && (path === null || named.get(step) === path)) continue;
    return {
      error: {
        type: "conflict",
        message: `Step ${step} of this landing${path === null ? "" : `, of ${path},`} was applied on this computer, and the steps named to forget the landing leave it out, so nothing the landing kept was forgotten`,
      },
    };
  }
  return null;
}
