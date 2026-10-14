// How a measurement judges what a root's commands said (harness.ts): a check of a command's output holds only where
// the command ran in its root. One the root's runner answered for, as with ENOENT on the root's folder, judged
// nothing: its check is UNTESTED, never PASS, and fails the measurement with that word.

import type { Outcome } from "../../../../src/link/protocol.js";

/** What a command of a root's said: *ran* once it ran in its root, and not where the root's runner answered for it. */
export interface Ran {
  output: string;
  returncode: number;
  outcome: Outcome;
  ran: boolean;
}

/**
 * Whether *outcome*, a run's, is of a command that ran in its root and ended: a runner that could not start it, or
 * not in the root's folder (its ENOENT there, answered with a return code of -1), an error, and a run that timed
 * out are not.
 */
export const ranInRoot = (outcome: Outcome): boolean => {
  if (!("ok" in outcome)) return false;
  const { returncode, timed_out } = outcome.ok as { returncode?: unknown; timed_out?: unknown };
  return typeof returncode === "number" && returncode >= 0 && timed_out !== true;
};

/** A check's verdict: held, or not, or UNTESTED where a command it judges did not run in its root. */
export const UNTESTED = "UNTESTED";
export type Verdict = boolean | typeof UNTESTED;

/** *held*, a check of what *runs* said, or UNTESTED unless each of them ran in its root. */
export const judged = (runs: Array<Pick<Ran, "ran">>, held: boolean): Verdict => (runs.every(({ ran }) => ran) ? held : UNTESTED);

/** A verdict's word: PASS, FAIL, or UNTESTED. */
export const verdictWord = (verdict: Verdict): string => (verdict === UNTESTED ? UNTESTED : verdict ? "PASS" : "FAIL");

/** Each of *checks* that did not pass, by its word and its name. */
export const failures = (checks: Record<string, Verdict>): string[] =>
  Object.entries(checks).filter(([, verdict]) => verdict !== true).map(([check, verdict]) => `${verdictWord(verdict)} ${check}`);
