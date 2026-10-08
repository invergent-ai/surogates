// A download a page of the agent's browser started (spec, Section 5): the browser host stages it in
// its own temporary folder, under the identity's profiles; then it is saved under Downloads in the
// chat's folder through the chat's file host, by the rules and approvals of any write. Nothing there
// is ever replaced: the file is made create-only, and the next free name is taken.

import { randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { open, rm } from "node:fs/promises";
import { extname, posix } from "node:path";

import type { DownloadBy } from "../binding/approvals.js";
import { MAX_WRITE_BYTES, osError, strerror } from "../files/answers.js";
import type { Bindings } from "../journal/bindings.js";
import type { Operation, Outcome } from "../link/protocol.js";

// A finished download, as the browser host staged it: the chat, the session whose page started it,
// the name the browser gave it, and where the host keeps it until it is saved. user: it is its
// user's, not the agent's: it began while they held the browser, or cannot be told from one that
// did (host.ts, whose). It is asked about in either mode, and nothing of it is the agent's to hear.
export interface StagedDownload {
  root: string;
  session: string;
  name: string;
  path: string;
  user: boolean;
}

// What saves it: the device's binder, which asks the chat's approvals, then runs it on the chat's file host.
export interface Saver {
  admit(operation: Operation, signal: AbortSignal, download: DownloadBy): Promise<Outcome | null>;
  run(operation: Operation, signal: AbortSignal): Promise<Outcome>;
}

// Where a chat's downloads go, under its folder: beside its files, never among them, where the agent's
// next command would act on a page's conftest.py, Makefile or package.json.
export const DOWNLOADS = "Downloads";
// How many names are tried: report.txt, then report (2).txt up to report (100).txt.
const NAMES = 100;
// The most of a name, in bytes of UTF-8, as the file system counts it: 255 there, and " (100)" comes after.
const NAME_BYTES = 200;

// *text* cut to at most *bytes* of UTF-8, between characters, never inside one.
function cut(text: string, bytes: number): string {
  let kept = "";
  let size = 0;
  for (const character of text) {
    size += Buffer.byteLength(character);
    if (size > bytes) break;
    kept += character;
  }
  return kept;
}

// What goes from a name, which is shown in the prompts and the file manager: control and format characters,
// bidi among them; the separators of lines and of paragraphs; half a character (a lone surrogate), which
// the file system would write as another; private-use and unassigned characters; and the letters that
// draw nothing. What a script joins a letter with, or picks its shape by, shows, and stays.
const UNSEEN = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Cs}\p{Co}\p{Cn}\u115F\u1160\u2800\u3164\uFFA0]/gu;
// Every space but the plain one, which each becomes.
const SPACES = /\p{Zs}/gu;

/** The name a page gave a download, as a notice quotes it. */
export const quoted = (name: string): string => JSON.stringify(cut(name, NAME_BYTES));

/**
 * The page's name for a download as one file name of the chat's folder: never a path, a hidden file
 * or a character that does not show, and text a file's name holds as it is, so the name asked about
 * is the name written.
 */
export function savedName(suggested: string): string {
  // The browser makes it one name already ("../a" is "_.._a"): a separator left is cut all the same.
  const last = suggested.split(/[/\\]/).at(-1) ?? "";
  const shown = last.replace(UNSEEN, "").replace(SPACES, " ").trim();
  // Never hidden: programs in the folder act on some (.envrc, .npmrc).
  const named = shown.startsWith(".") ? `_${shown.slice(1)}` : shown;
  if (named.replace(/[._]/g, "") === "") return "download";
  if (Buffer.byteLength(named) <= NAME_BYTES) return named;
  const extension = cut(extname(named), 20);
  return cut(named.slice(0, named.length - extname(named).length), NAME_BYTES - Buffer.byteLength(extension)) + extension;
}

// Why nothing was saved, where the reason is this computer's own and not the agent's to read.
const COULD_NOT = "this computer could not save it";
// And where its chat was deleted meanwhile.
const STOPPED = "its chat was deleted";
// And where a link is at Downloads.
const LINKED = "Downloads in the chat's folder is a link, and nothing is saved through one";
// What stops no save: a save not told what would stop it.
const NEVER = new AbortController().signal;
/** What the agent is told of a download that came as no download this computer saves: not even its name is taken from it. */
export const UNSAVED = `The page downloaded a file, but it was not saved: ${COULD_NOT}.`;

/** What the agent is told of one too large to save. *most*: the limit in force, a write's most unless told another. */
export const tooLarge = (name: string, bytes: number, most = MAX_WRITE_BYTES): string =>
  `The page downloaded ${quoted(name)} (${bytes} bytes), too large to save in the chat's folder at once (at most ${most} bytes), so it was not saved.`;
/** What the agent is told of one of its own that its user's take-over of the browser stopped. */
export const interrupted = (name: string): string =>
  `The page's download of ${quoted(name)} was interrupted when the user took over the agent's browser on this computer, so it was not saved.`;

// The *n*th name tried for *name*.
function numbered(name: string, n: number): string {
  if (n === 1) return name;
  const extension = extname(name);
  return `${name.slice(0, name.length - extension.length)} (${n})${extension}`;
}

// What is staged at *path*, whole; or its size, where that is over what a write may carry. Its size is
// looked at before it is read, and no more than that is read: whatever is there, no more than a write's
// most is held in memory. A link at its last name is not followed.
async function stagedAt(path: string): Promise<Buffer | number> {
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const { size } = await file.stat();
    if (size > MAX_WRITE_BYTES) return size;
    const data = Buffer.alloc(size);
    let read = 0;
    while (read < size) {
      const { bytesRead } = await file.read(data, read, size - read, read);
      if (bytesRead === 0) break;
      read += bytesRead;
    }
    return data.subarray(0, read);
  } finally {
    await file.close();
  }
}

/**
 * Save *download* under Downloads in its chat's folder, and say what came of it, for the agent to
 * hear at its next answer. Asked once, as a write of the chat's; made create-only, so what is at a
 * name by then is never replaced: the next name is taken. The staged file goes either way, and
 * this never rejects: of one it cannot even name, as a browser host gone wrong would stage, it says
 * that a file was not saved. *stop* aborts when its chat is deleted: its open prompt goes, and
 * nothing more is asked or written. Otherwise an unanswered prompt expires by itself, and a quit
 * denies what still asks.
 */
export async function saveDownload(
  download: StagedDownload, bindings: Pick<Bindings, "get">, saver: Saver, stop: AbortSignal = NEVER,
): Promise<string> {
  try {
    return await save(download, bindings, saver, stop);
  } catch {
    return UNSAVED;
  } finally {
    await rm(download.path, { force: true }).catch(() => {});
  }
}

async function save(download: StagedDownload, bindings: Pick<Bindings, "get">, saver: Saver, signal: AbortSignal): Promise<string> {
  const said = `The page downloaded ${quoted(download.name)}`;
  const notSaved = (why: string) => `${said}, but it was not saved: ${why}.`;
  const op = (kind: string, args: Record<string, unknown>): Operation => ({
    id: `download-${randomUUID()}`, sessionId: download.root, callingSessionId: download.session, invocationId: "download", ordinal: 0,
    kind, args, digest: "",
  });
  try {
    const binding = bindings.get(download.root);
    if (!binding) return `${said}, but this chat has no folder on this computer, so it was not saved.`;
    let data: string;
    try {
      const staged = await stagedAt(download.path);
      if (typeof staged === "number") return tooLarge(download.name, staged);
      data = staged.toString("base64");
    } catch (error) {
      // Gone with the browser's close, say. Told by the error's code alone, in the file tools' words for it:
      // what the error says itself names where the browser host keeps its files on this computer.
      const why = strerror((error as NodeJS.ErrnoException | null)?.code);
      return notSaved(why === null ? COULD_NOT : `the file the browser kept could not be read (${why})`);
    }
    // Where the chat's downloads go: the folder's own Downloads, there or still to be made. Why nothing is saved
    // there now, otherwise: a link at it, wherever it leads (out of the folder, in its file host's words; within
    // it, to the folder's top or to a folder whose files run by themselves), or something that is no folder.
    const own = posix.join(binding.folder, DOWNLOADS);
    const unfit = async (): Promise<string | null> => {
      const into = await saver.run(op("resolve", { path: own }), signal);
      if ("error" in into) return into.error.message;
      if (into.ok !== own) return LINKED;
      const there = await saver.run(op("stat", { key: own }), signal);
      if ("error" in there) return there.error.message;
      return there.ok !== null && (there.ok as { is_dir?: unknown }).is_dir !== true ? osError("EEXIST", own).refusal.message : null;
    };
    // Said before anyone is asked, and before any write is tried.
    const why = await unfit();
    if (why !== null) return notSaved(why);
    const name = savedName(download.name);
    let asked = false;
    // Why the file host refused the last name it would not take.
    let refused: string | null = null;
    for (let n = 1; n <= NAMES; n += 1) {
      const key = posix.join(own, numbered(name, n));
      // A name that resolves elsewhere, as a link at it does, to anywhere, is passed over: never written through.
      const resolved = await saver.run(op("resolve", { path: key }), signal);
      if ("error" in resolved) refused = resolved.error.message;
      if ("error" in resolved || resolved.ok !== key) continue;
      // Nothing there is a stat of null; anything there, a file or a folder, is passed over too.
      const found = await saver.run(op("stat", { key }), signal);
      if ("error" in found) return notSaved(found.error.message);
      if (found.ok !== null) continue;
      // Made only where nothing is. Asked once, at the first name found free: its user allows the download, not one name.
      const write = op("write", { key, data, create: true });
      const denied = asked ? null : await saver.admit(write, signal, download.user ? "user" : "page");
      // Told to stop while it asked, or before: nobody answered, and nothing is written, in a chat that works freely either.
      if (signal.aborted) return notSaved(STOPPED);
      // Denied, or not answered: no other name is asked about.
      if (denied !== null) return notSaved("error" in denied ? denied.error.message : COULD_NOT);
      asked = true;
      const outcome = await saver.run(write, signal);
      if (!("error" in outcome)) return `${said}. It is saved in the chat's folder as ${key.slice(binding.folder.length + 1)}.`;
      // Refused. What is there now says whether another name may do: nothing is written through or over any of it.
      // Downloads itself, made a link or a file since the look, ends the save.
      const since = await unfit();
      if (since !== null) return notSaved(since);
      // A link made at the name since, or a file or a folder: left as it is, and the next name is tried.
      const now = await saver.run(op("resolve", { path: key }), signal);
      const taken = "error" in now || now.ok !== key ? null : await saver.run(op("stat", { key }), signal);
      if (taken !== null && ("error" in taken || taken.ok === null)) return notSaved(outcome.error.message);
      refused = outcome.error.message;
    }
    return notSaved(refused ?? `the chat's folder has ${NAMES} files of that name already`);
  } catch {
    // Not in the error's own words, which can name a path of this computer.
    return notSaved(COULD_NOT);
  }
}

/**
 * What saves a device's downloads: one at a time for a chat, in the order they finished, so each
 * looks for its name once the one before it is saved, and its prompt names the file it becomes.
 */
export function downloadSaver(
  bindings: Pick<Bindings, "get">, saver: Saver,
): (download: StagedDownload, stop?: AbortSignal) => Promise<string> {
  // Each chat's last save in line.
  const lines = new Map<string, Promise<string>>();
  return (download, stop) => {
    // A save never rejects: it says why it saved nothing, so the chat's line goes on to the next whatever came of it.
    const mine = (lines.get(download.root) ?? Promise.resolve("")).then(() => saveDownload(download, bindings, saver, stop));
    lines.set(download.root, mine);
    void mine.then(() => {
      if (lines.get(download.root) === mine) lines.delete(download.root);
    });
    return mine;
  };
}
