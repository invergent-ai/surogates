// What a file helper on a thread's copy answers, in the words of the folder the copy is of (spec, Section 13, "The
// user's computer": "It maps the folder's path ... onto the worktree at its edge"). Requests are mapped where their
// paths are judged (paths.ts); here the answers: a failure's words, and what a search prints, whose paths its caller
// asks about next. And what such a helper may be started with.

import { posix } from "node:path";

import { Failure, type Refusal } from "./answers.js";
import { type Edge, inside, shown } from "./paths.js";

// A path as a helper is given one: absolute, every name in it a name, no slash doubled or at its end, and not the root.
const whole = (path: string): boolean => path.startsWith("/") && path !== "/" && !path.endsWith("/") && posix.normalize(path) === path;

/**
 * Why a helper on *folder* cannot name it by *at*, the path it was started with, or null. The two are told apart by
 * their names alone, so each is a whole path; and *at* is another folder's than the helper's own, which neither
 * holds it nor lies in it: a copy is no part of the folder it stands for.
 */
export function edgeRefused(folder: string, at: string): string | null {
  if (!whole(at)) return `the file helper's SUROGATE_AT must be the whole path of the folder its copy is of: '${at}'`;
  if (!whole(folder)) return "the file helper's SUROGATE_FOLDER must be a whole path for SUROGATE_AT to name it by another";
  if (inside(at, folder) || inside(folder, at)) {
    return `the file helper's SUROGATE_AT must name a folder that is not the helper's own, neither holds it nor lies in it: '${at}'`;
  }
  return null;
}

/** *text*, with the folder's path wherever it holds the copy's: words nothing told how to name their paths, as rg's own. */
export const said = (text: string, { at, folder }: Edge): string => text.split(folder).join(at);

/** What an operation is refused with, *refusal*, as a helper on a copy answers it. *failure*: what the kind threw. */
export function retold(refusal: Refusal, failure: unknown, edge: Edge): Refusal {
  if (failure instanceof Failure && failure.retold) return failure.retold((path) => shown(path, edge));
  return { ...refusal, message: said(refusal.message, edge) };
}

// A path rg could not print as text: its bytes, in base64.
function namedBytes(encoded: string, { at, folder }: Edge): string {
  const bytes = Buffer.from(encoded, "base64");
  const prefix = Buffer.from(`${folder}/`);
  if (bytes.length < prefix.length || !bytes.subarray(0, prefix.length).equals(prefix)) return encoded;
  return Buffer.concat([Buffer.from(`${at}/`), bytes.subarray(prefix.length)]).toString("base64");
}

// One event of rg --json, its file named by the folder's path. Its lines are the file's own text, and stay.
function event(line: string, edge: Edge): string {
  let parsed: { data?: { path?: { text?: unknown; bytes?: unknown } } } | null;
  try {
    parsed = JSON.parse(line) as typeof parsed;
  } catch {
    // No event: whatever it is, it does not say the copy's path, as JSON writes it or as it is.
    const written = { at: JSON.stringify(edge.at).slice(1, -1), folder: JSON.stringify(edge.folder).slice(1, -1) };
    return written.folder === edge.folder ? said(line, edge) : said(said(line, written), edge);
  }
  const path = parsed?.data?.path;
  if (typeof path !== "object" || path === null) return line;
  const [form, written] = typeof path.text === "string" ? (["text", path.text] as const) : typeof path.bytes === "string" ? (["bytes", path.bytes] as const) : [];
  if (form === undefined) return line;
  const named = form === "text" ? shown(written, edge) : namedBytes(written, edge);
  // An event whose file is named as it was is answered as rg wrote it.
  if (named === written) return line;
  path[form] = named;
  return JSON.stringify(parsed);
}

/** A search's *output* in *mode*, each file named by the folder's path: a list of files, a count for each, or rg's events. */
export function searched(mode: string, output: string, edge: Edge): string {
  const lines = output.split("\n");
  if (mode === "json") return lines.map((line) => (line ? event(line, edge) : line)).join("\n");
  // "<path>" or "<path>:<count>": the copy's path is the line's start.
  return lines.map((line) => (line.startsWith(`${edge.folder}/`) ? edge.at + line.slice(edge.folder.length) : line)).join("\n");
}

/**
 * The most bytes a line of rg's output in *mode* loses when its file is named by the folder's path, where that is the
 * shorter: a line names its file once. A search is capped by what it answers, so its lines count for that much less.
 */
export function spared(mode: string, { at, folder }: Edge): number {
  const [real, named] = [Buffer.byteLength(folder), Buffer.byteLength(at)];
  if (mode !== "json") return Math.max(0, real - named);
  // An event writes its path as JSON does, or its bytes in base64 where they are no text.
  const written = Buffer.byteLength(JSON.stringify(folder)) - Buffer.byteLength(JSON.stringify(at));
  return Math.max(0, written, 4 * Math.ceil((real - named) / 3) + 4);
}

/**
 * What rg wrote as it failed, by the folder's path. *kept* is the start of it, and *cut* whether it wrote more: a
 * path of the copy's that the cut fell in cannot be named by the folder's, and goes with what was cut.
 */
export function saidInFailing(kept: Buffer, cut: boolean, decode: (bytes: Buffer) => string, edge: Edge): string {
  const copy = Buffer.from(edge.folder);
  let end = kept.length;
  if (cut) {
    for (let length = Math.min(copy.length - 1, kept.length); length > 0; length -= 1) {
      if (kept.subarray(kept.length - length).equals(copy.subarray(0, length))) {
        end = kept.length - length;
        break;
      }
    }
  }
  return said(decode(kept.subarray(0, end)), edge);
}
