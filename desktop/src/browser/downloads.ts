// A download a page of the agent's browser started (spec, Section 5): the browser host stages it in
// its own temporary folder, under the identity's profiles; then it is saved under Downloads in the
// chat's folder through the chat's file host, by the rules and approvals of any write. Nothing there
// is ever replaced: the file is made create-only, and the next free name is taken.

import { randomUUID } from "node:crypto";
import { readFile, rm } from "node:fs/promises";
import { extname, posix } from "node:path";

import type { DownloadBy } from "../binding/approvals.js";
import type { Bindings } from "../journal/bindings.js";
import type { Operation, Outcome } from "../link/protocol.js";

// A finished download, as the browser host staged it: the chat, the session whose page started it,
// the name the browser gave it, and where the host keeps it until it is saved. user: it started
// while the chat's user held the browser, so it is theirs, and nothing of it is the agent's to hear.
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

/** The name a page gave a download, as a notice quotes it. */
export const quoted = (name: string): string => JSON.stringify(cut(name, NAME_BYTES));

/** The page's name for a download as one file name of the chat's folder: never a path, a hidden file or an invisible character. */
export function savedName(suggested: string): string {
  // The browser makes it one name already ("../a" is "_.._a"): a separator left is cut all the same.
  const last = suggested.split(/[/\\]/).at(-1) ?? "";
  // Control, bidi and invisible characters go: the name is shown in the prompts and the file manager.
  const shown = last.replace(/[\p{Cc}\p{Cf}]/gu, "").trim();
  // Never hidden: programs in the folder act on some (.envrc, .npmrc).
  const named = shown.startsWith(".") ? `_${shown.slice(1)}` : shown;
  if (named.replace(/[._]/g, "") === "") return "download";
  if (Buffer.byteLength(named) <= NAME_BYTES) return named;
  const extension = cut(extname(named), 20);
  return cut(named.slice(0, named.length - extname(named).length), NAME_BYTES - Buffer.byteLength(extension)) + extension;
}

// The *n*th name tried for *name*.
function numbered(name: string, n: number): string {
  if (n === 1) return name;
  const extension = extname(name);
  return `${name.slice(0, name.length - extension.length)} (${n})${extension}`;
}

/**
 * Save *download* under Downloads in its chat's folder, and say what came of it, for the agent to
 * hear at its next answer. Asked once, as a write of the chat's; made create-only, so what is at a
 * name by then is never replaced: the next name is taken. The staged file goes either way.
 */
export async function saveDownload(download: StagedDownload, bindings: Pick<Bindings, "get">, saver: Saver): Promise<string> {
  const said = `The page downloaded ${quoted(download.name)}`;
  const notSaved = (why: string) => `${said}, but it was not saved: ${why}.`;
  const op = (kind: string, args: Record<string, unknown>): Operation => ({
    id: `download-${randomUUID()}`, sessionId: download.root, callingSessionId: download.session, invocationId: "download", ordinal: 0,
    kind, args, digest: "",
  });
  // ponytail: never cancelled; an unanswered prompt expires by itself, and a quit denies what still asks.
  const signal = new AbortController().signal;
  try {
    const binding = bindings.get(download.root);
    if (!binding) return `${said}, but this chat has no folder on this computer, so it was not saved.`;
    const data = (await readFile(download.path)).toString("base64");
    // Where the chat's downloads go, as its file host resolves it: a link there that leads out of the folder is refused.
    const into = await saver.run(op("resolve", { path: posix.join(binding.folder, DOWNLOADS) }), signal);
    if ("error" in into) return notSaved(into.error.message);
    const name = savedName(download.name);
    let asked = false;
    // Why the file host refused the last name it would not take.
    let refused: string | null = null;
    for (let n = 1; n <= NAMES; n += 1) {
      const key = posix.join(String(into.ok), numbered(name, n));
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
      const outcome = (asked ? null : await saver.admit(write, signal, download.user ? "user" : "page")) ?? (await saver.run(write, signal));
      asked = true;
      if ("error" in outcome && outcome.error.code === "EEXIST") {
        // Made there since the look, by another writer: left as it is, and the next name is tried.
        refused = outcome.error.message;
        continue;
      }
      if ("error" in outcome) return notSaved(outcome.error.message);
      return `${said}. It is saved in the chat's folder as ${key.slice(binding.folder.length + 1)}.`;
    }
    return notSaved(refused ?? `the chat's folder has ${NAMES} files of that name already`);
  } catch (error) {
    return notSaved(error instanceof Error ? error.message : String(error));
  } finally {
    await rm(download.path, { force: true }).catch(() => {});
  }
}

/**
 * What saves a device's downloads: one at a time for a chat, in the order they finished, so each
 * looks for its name once the one before it is saved, and its prompt names the file it becomes.
 */
export function downloadSaver(bindings: Pick<Bindings, "get">, saver: Saver): (download: StagedDownload) => Promise<string> {
  // Each chat's last save in line.
  const lines = new Map<string, Promise<string>>();
  return (download) => {
    // A save never rejects: it says why it saved nothing.
    const mine = (lines.get(download.root) ?? Promise.resolve("")).then(() => saveDownload(download, bindings, saver));
    lines.set(download.root, mine);
    void mine.then(() => {
      if (lines.get(download.root) === mine) lines.delete(download.root);
    });
    return mine;
  };
}
