// What a tool host is started on, checked before it holds anything (spec, Section 13, "The user's
// computer"). A chat's host holds its folder. A project thread's host holds the thread's copy of
// its folder, which lies in the app's data, where no chat's folder may: it is taken only at the
// one path the app makes for it, as a folder of its own, since the guest writes there. A landing's
// host holds the folder itself, and is given the thread's copy to read and a folder of the app's
// to keep replaced files in. A copy and a kept folder are named by the real path of the app's
// data, as the app makes them: through a link, neither is taken.
//
//   <data>/history/<key>/threads/<thread>/   a thread's copy of the folder with that key
//   <data>/landings/<key>/                   what that folder's landings keep of the files they replace
//
// Which of the three a start is, it says by what it carries beside its folder (STARTS). Each
// answers the same things of itself (Start), and the host goes by those alone.

import { lstatSync, mkdirSync, realpathSync, rmdirSync, type Stats } from "node:fs";
import { join, resolve } from "node:path";

import { checkFolder, type FolderCheck, type FolderGuards } from "../binding/folder.js";
import { edgeRefused } from "../files/edge.js";
import { inside, realpath } from "../files/paths.js";
import { PLACE_KEY } from "../guest/protocol.js";
import type { HostStart } from "./messages.js";
import { GLOB, isReserved } from "./policy.js";

// How long a file helper has to say it is ready.
export const READY_MS = 15_000;
// A landing's helper first puts back what a step cut short left (files/land.ts): where the app's
// data is on another filesystem than the folder, by a copy of a file of up to 1 GiB. A helper
// stopped before that ends is stopped again at the next start, and the file is never put back.
// So it has as long as that copy takes at under 2 MiB a second, a slow stick's or a share's rate.
export const LANDING_READY_MS = 600_000;

// A thread's id, as the app names its copy by.
const THREAD = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

// Where the app keeps a thread's copy, and what a folder's landings keep, in its data by its real path *data*.
const copyAt = (data: string, key: string, thread: string): string => join(data, "history", key, "threads", thread);
const keptAt = (data: string, key: string): string => join(data, "landings", key);

/** A host's start, checked: what it holds, and what the host must know of it. */
export interface Start {
  ok: true;
  // The folder it holds, as it resolves, and its identity: its lock is this folder's, and its helper works in it.
  path: string;
  dev: number;
  ino: number;
  // What its helper's sandbox admits beside that folder: to read and not write, and to write.
  reads: string[];
  writes: string[];
  // What its helper is told beside its folder, by the names it reads them under.
  env: Record<string, string>;
  // Whether the root's commands write the folder: where they do, the host keeps the folder's record and guards its hooks.
  commands: boolean;
  // The one kind its helper is asked, where it takes no other.
  only?: string;
  // How long its helper has to say it is ready.
  readyMs: number;
  /**
   * Makes what the app keeps for it and is not there yet, this user's alone, once the host holds
   * its folder. What it made, the last first: a start that then fails takes them away again.
   */
  make(): string[];
}

export type StartCheck = Start | Extract<FolderCheck, { ok: false }>;

const refused = (message: string, missing = false): StartCheck => ({ ok: false, missing, message });

// A folder of the app's own at *path*: this user's, not a link, and no link on its way.
function own(path: string, uid: number): Stats | "missing" | null {
  try {
    const found = lstatSync(path);
    return found.isDirectory() && found.uid === uid && realpathSync(path) === path ? found : null;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "ENOENT" ? "missing" : null;
  }
}

// The app's data *dataDir* by its real path; null where there is none.
function realData(dataDir: string): string | null {
  try {
    return realpathSync(dataDir);
  } catch {
    return null;
  }
}

// The key of the folder whose copy *path* is, in the app's data *dataDir*: only the path the app
// makes for a thread's copy, from a key and a thread's id as it writes each, has one.
function copyKey(path: unknown, dataDir: string): string | null {
  const data = realData(dataDir);
  if (data === null || typeof path !== "string") return null;
  const histories = `${join(data, "history")}/`;
  const [key, , thread] = path.startsWith(histories) ? path.slice(histories.length).split("/") : [];
  if (key === undefined || thread === undefined || !PLACE_KEY.test(key) || !THREAD.test(thread)) return null;
  return path === copyAt(data, key, thread) ? key : null;
}

// The copy at *path* of the folder *at*, as a host may hold it or a landing read it; or why not.
function checkCopy(path: string, dataDir: string, at: string, uid: number): FolderCheck {
  const what = `the copy of ${at} this thread works in`;
  if (copyKey(path, dataDir) === null) return { ok: false, missing: false, message: `${what} is not where the app keeps one` };
  const found = own(path, uid);
  if (found === "missing") return { ok: false, missing: true, message: `${what} is not there` };
  if (found === null) return { ok: false, missing: false, message: `${what} is not a folder of the app's own` };
  if (GLOB.test(path)) return { ok: false, missing: false, message: `this computer cannot sandbox a folder whose path holds *, ?, [ or ]: ${path}` };
  if (isReserved(path)) return { ok: false, missing: false, message: `the folder ${path} is inside one of this computer's system folders` };
  return { ok: true, path, dev: found.dev, ino: found.ino };
}

// What a chat's start and a copy's have alike: their helper is given nothing beside the folder, and
// is asked every kind; the root's commands write the folder.
const plain = (): Pick<Start, "reads" | "writes" | "commands" | "readyMs" | "make"> => ({
  reads: [], writes: [], commands: true, readyMs: READY_MS, make: () => [],
});

// A chat's folder, as any may be one (binding/folder.ts).
function onFolder(message: HostStart, guards: FolderGuards): StartCheck {
  const held = checkFolder(message.folder, guards);
  return held.ok ? { ...held, ...plain(), env: {} } : held;
}

// A thread's copy, named by the path of the folder it stands for. That path names the folder to the
// helper and to the guest, and is looked at by neither, nor here: a whole path, as the helper takes
// one (files/edge.ts), and no part of the app's own data or cache, as each is spelled and as it resolves.
function onCopy(message: HostStart, guards: FolderGuards, uid: number): StartCheck {
  const { folder, at } = message;
  if (typeof at !== "string") return refused("a thread's copy stands for a folder by that folder's path");
  const apps = [guards.dataDir, guards.cacheDir].flatMap((dir) => [resolve(dir), realpath(resolve(dir)).path]);
  if (copyKey(folder, guards.dataDir) !== null && (edgeRefused(folder, at) !== null || apps.some((dir) => inside(at, dir) || inside(dir, at)))) {
    return refused(`${at} is no path a thread's copy can stand for`);
  }
  const held = checkCopy(folder, guards.dataDir, at, uid);
  return held.ok ? { ...held, ...plain(), env: { SUROGATE_AT: at } } : held;
}

// The folder a landing writes, as a chat's; with the thread's copy, read-only, and the folder the
// app keeps the files a landing replaces in, each where the app keeps it for one folder, a folder
// of its own. It runs no command, and its helper is asked the land kind alone: the folder is the user's own.
function onLanding(message: HostStart, guards: FolderGuards, uid: number): StartCheck {
  const held = checkFolder(message.folder, guards);
  if (!held.ok) return held;
  const { copy, kept } = (typeof message.landing === "object" && message.landing !== null ? message.landing : {}) as Partial<NonNullable<HostStart["landing"]>>;
  const key = copyKey(copy, guards.dataDir);
  const from = key === null ? null : checkCopy(copy!, guards.dataDir, held.path, uid);
  if (!from?.ok) return refused(`this landing's copy is not a thread's copy of the folder: ${from ? from.message : String(copy)}`);
  // The copy is one, so the app's data is there, and no part of its path is one the sandbox cannot be given.
  const data = realpathSync(guards.dataDir);
  if (kept !== keptAt(data, key!)) return refused(`this landing's kept folder is not the folder's own: ${String(kept)}`);
  const notOwn = `this landing's kept folder is not a folder of the app's own: ${kept}`;
  // The kept folder, and the folder that holds every folder's: there as folders of the app's own, or
  // not there yet. Neither is made here: a start that is refused, or waits for the folder in vain, leaves nothing.
  const lacking = (): string[] | null => {
    const lacks: string[] = [];
    for (const dir of [join(data, "landings"), kept]) {
      const found = lacks.length > 0 ? "missing" : own(dir, uid);
      if (found === null) return null;
      if (found === "missing") lacks.push(dir);
    }
    return lacks;
  };
  if (lacking() === null) return refused(notOwn);
  const make = (): string[] => {
    const made: string[] = [];
    try {
      // Looked at again: a link put at either since would keep what a landing replaced wherever it leads.
      const lacks = lacking();
      if (lacks === null) throw new Error(notOwn);
      for (const dir of lacks) {
        mkdirSync(dir, { mode: 0o700 });
        made.unshift(dir);
      }
      const there = own(kept, uid);
      if (there === null || there === "missing") throw new Error(notOwn);
    } catch (error) {
      for (const dir of made) {
        try {
          rmdirSync(dir);
        } catch {
          // Left where it could not be taken away.
        }
      }
      throw error;
    }
    return made;
  };
  return {
    ...held, reads: [copy!], writes: [kept], env: { SUROGATE_COPY: copy!, SUROGATE_KEPT: kept }, commands: false, only: "land",
    readyMs: LANDING_READY_MS, make,
  };
}

// The starts that are no chat's, each by the field of its message that says so.
const STARTS = { at: onCopy, landing: onLanding } as const;

/**
 * What *message* starts a host on, checked as what it is: the folder it holds, as it resolves,
 * with its identity, and what the host needs of the start; or why no host may hold it. A chat's
 * folder and a landing's are checked as any chat's. A thread's copy, and what a landing is given,
 * must be the app's own, where the app keeps them: *uid* is the user whose they are.
 */
export function startOn(message: HostStart, home: string, appDirs: string[], uid = process.getuid?.() ?? -1): StartCheck {
  const guards: FolderGuards = { home, dataDir: message.dataDir, cacheDir: message.cacheDir, appDirs };
  const said = (Object.keys(STARTS) as Array<keyof typeof STARTS>).filter((field) => message[field] !== undefined);
  if (said.length > 1) return refused("a tool host is started on a chat's folder, on a thread's copy of one, or on a folder for a landing: this start names more than one");
  return said[0] === undefined ? onFolder(message, guards) : STARTS[said[0]](message, guards, uid);
}
