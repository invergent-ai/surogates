// What a tool host is started on, checked before it holds anything (spec, Section 13, "The user's
// computer"). A chat's host holds its folder. A project thread's host holds the thread's copy of
// its folder, which lies in the app's data, where no chat's folder may: it is taken only at the
// one path the app makes for it, as a folder of its own, since the guest writes there. A landing's
// host holds the folder itself, and is given the thread's copy to read and a folder of the app's
// to keep replaced files in. A recovery's host holds the folder itself too, and is given that
// folder of the app's and no copy: it puts back what a landing there cut short. A copy and a kept
// folder are named by the real path of the app's data, as the app makes them: through a link,
// neither is taken.
//
//   <data>/history/<key>/threads/<thread>/   a thread's copy of the folder with that key
//   <data>/landings/<key>/                   what that folder's landings keep of the files they replace
//
// Which of the four a start is, it says by what it carries beside its folder (STARTS). Each
// answers the same things of itself (Start), and the host goes by those alone.

import { lstatSync, mkdirSync, realpathSync, rmdirSync, type Stats } from "node:fs";
import { join, resolve } from "node:path";

import { checkFolder, type FolderCheck, type FolderGuards } from "../binding/folder.js";
import { said, whole } from "../files/edge.js";
import { inside, realpath } from "../files/paths.js";
import { PLACE_KEY } from "../guest/protocol.js";
import { KEPT_NAME, keyOf } from "../history/place.js";
// A thread's id, as the app names its copy by: the one spelling the folder's place has too (history/place.ts).
import { THREAD } from "../vm/history.js";
import type { HostStart } from "./messages.js";
import { GLOB, isReserved, sandboxPolicy } from "./policy.js";

// How long a file helper has to say it is ready. Every helper does nothing before it is: a landing's
// puts back what a step cut short in the first step it is asked (files/land.ts), however long that takes.
export const READY_MS = 15_000;

// The most bytes of a path this system takes, and of a name in one.
const PATH_BYTES = 4095;
const NAME_BYTES = 255;
// A search's output is lines, each begun by its file's path: a path that holds a line break begins none.
const LINE_BREAK = /[\n\r]/;
// A path in a line of words, no further than a line goes.
const shown = (path: string): string => (path.length > 300 ? `${path.slice(0, 300)}…` : path);

// A folder as it was found when it was checked: what a helper is told of each folder it is given, and holds what
// it finds at that folder's path in its sandbox against (files/helper.ts). A path can come to lead to another
// folder between the check and the sandbox's bind of it; the folder that was checked cannot become another.
export const isOf = ({ dev, ino }: { dev: number; ino: number }): string => `${dev}:${ino}`;

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
  // What its helper is told beside its folder, by the names it reads them under: each folder it is given beside
  // its own, with which folder that was when it was checked.
  env: Record<string, string>;
  // Whether the root's commands write the folder: where they do, the host keeps the folder's record and guards its hooks.
  commands: boolean;
  // The one kind its helper is asked, where it takes no other.
  only?: string;
  // How long its helper has to say it is ready.
  readyMs: number;
  // What is said of a folder that is no longer the one this start names by its identity.
  replaced: string;
  /**
   * *text*, words of a failure nothing told how to name its paths (a program's own, the system's),
   * as this host says them: a copy's host names its files by the folder the copy stands for, never
   * by the copy's own path.
   */
  named(text: string): string;
  /**
   * Makes what the app keeps for it and is not there yet, this user's alone, once the host holds
   * its folder. What it made, the last first: a start that then fails takes them away again. And
   * what its helper is told of what was made or found there: which folder each was.
   */
  make(): { made: string[]; env: Record<string, string> };
}

export type StartCheck = Start | Extract<FolderCheck, { ok: false }>;

const refused = (message: string): StartCheck => ({ ok: false, missing: false, message });

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

// The copy at *path* of the folder *at*, as a host may hold it or a landing read it; or why not, said
// by the folder: the copy's own path is the app's, and names nothing to whoever is answered.
function checkCopy(path: unknown, dataDir: string, at: string, uid: number): FolderCheck {
  const what = `the copy of ${shown(at)} this thread works in`;
  const no = (message: string, missing = false): FolderCheck => ({ ok: false, missing, message });
  if (typeof path !== "string" || copyKey(path, dataDir) === null) return no(`${what} is not where the app keeps one`);
  // The key and the thread's id hold none of these: the app's data does, or does not.
  if (LINE_BREAK.test(path)) return no(`this computer cannot work in ${what}: the path of the app's data holds a line break`);
  if (GLOB.test(path)) return no(`this computer cannot sandbox ${what}: the path of the app's data holds *, ?, [ or ]`);
  if (isReserved(path)) return no(`this computer cannot sandbox ${what}: the app's data is inside one of this computer's system folders`);
  const found = own(path, uid);
  if (found === "missing") return no(`${what} is not there`, true);
  if (found === null) return no(`${what} is not a folder of the app's own`);
  return { ok: true, path, dev: found.dev, ino: found.ino };
}

// What the sandbox of a helper would be given of *apart*, folders it must hold nothing of, beside
// what its start holds: by its working folder, by a folder of the app's own, or by one of the
// system's, each as it is spelled and as it resolves. The first that is, holds or lies in one of
// them, in words; or null. The sandbox is all that keeps a link a command left in a copy off what
// it names: the folder itself, another thread's copy, the folder's history.
function given(message: HostStart, appDirs: string[], apart: string[]): string | null {
  const tmp = resolve(message.tmp);
  // The policy's own list, less the folder a start holds: the working folder stands in for it here.
  const admitted = sandboxPolicy({ folder: tmp, tmp, appDirs }).filesystem.allowRead ?? [];
  const found = admitted.find((dir) => [dir, realpath(dir).path].some((spelled) => apart.some((one) => inside(spelled, one) || inside(one, spelled))));
  return found === undefined ? null : found === tmp ? "its working folder" : found;
}

// Where the app keeps every folder's place, and what every folder's landings keep, in its data
// *dataDir* by its real path: a helper's sandbox is given its own of each at most.
const keptApart = (dataDir: string): string[] => {
  const data = realData(dataDir);
  return data === null ? [] : [join(data, "history"), join(data, "landings")];
};

// What a chat's start and a copy's have alike: their helper is given nothing beside the folder, and
// is asked every kind; the root's commands write the folder.
const plain = (): Pick<Start, "reads" | "writes" | "commands" | "readyMs" | "make"> => ({
  reads: [], writes: [], commands: true, readyMs: READY_MS, make: () => ({ made: [], env: {} }),
});
// What a host on the folder itself says of one replaced since its chat was bound.
const replaced = (folder: string): string => `the folder ${folder} was replaced after it was confirmed for this chat`;
const asSaid = (text: string): string => text;

// A chat's folder, as any may be one (binding/folder.ts).
function onFolder(message: HostStart, guards: FolderGuards): StartCheck {
  const held = checkFolder(message.folder, guards);
  return held.ok ? { ...held, ...plain(), env: {}, replaced: replaced(message.folder), named: asSaid } : held;
}

// Why no thread's copy can stand for a folder, by the folder's path alone: how the path is spelled, or a folder every
// helper's sandbox reads, which holds it or lies in it.
export type NoCopyAt = { spelled: string } | { given: string };

/**
 * Why no thread's copy can stand for the folder at *at*, by that path alone, or null. That path names the folder to
 * the copy's helper and to the guest, byte for byte the path the guest mounts the copy at and the server sends keys
 * under, and is looked at by neither, nor here. So it is a whole path, as the helper takes one (files/edge.ts), with
 * no line break in it and no blank after it, and one the system would take; no part of the app's own data or
 * cache, as each is spelled and as it resolves; and none of what every helper's sandbox reads holds it or lies in
 * it, or a copy's sandbox would read the folder itself. A host's start on a copy refuses by it, and so does the
 * binder, before a project's thread is bound to a copy of its folder.
 */
export function noCopyAt(at: string, guards: FolderGuards): NoCopyAt | null {
  if (!whole(at)) return { spelled: "its path is not a whole one" };
  if (LINE_BREAK.test(at)) return { spelled: "its path holds a line break" };
  if (at.trimEnd() !== at) return { spelled: "its path ends in a space or a tab" };
  if (Buffer.byteLength(at) > PATH_BYTES || at.split("/").some((name) => Buffer.byteLength(name) > NAME_BYTES)) {
    return { spelled: "its path, or a name in it, is longer than this computer takes" };
  }
  const apps = [guards.dataDir, guards.cacheDir].flatMap((dir) => [resolve(dir), realpath(resolve(dir)).path]);
  if (apps.some((dir) => inside(at, dir) || inside(dir, at))) return { spelled: "it holds the app's own data or cache, or lies in them" };
  // The policy's own list, less the folder a helper holds and its working folder, which are each start's.
  const read = (sandboxPolicy({ folder: "", tmp: "", appDirs: guards.appDirs }).filesystem.allowRead ?? []).filter((dir) => dir !== "");
  const found = read.find((dir) => [dir, realpath(dir).path].some((spelled) => inside(spelled, at) || inside(at, spelled)));
  return found === undefined ? null : { given: found };
}

const cannotStand = (at: string, through: string): string =>
  `a thread's copy cannot stand for ${at}: its helper's sandbox would be given ${through}, and by it the folder itself or what the app keeps of it`;

// A thread's copy, named by the path of the folder it stands for: a path the binder took by the same rule (noCopyAt).
function onCopy(message: HostStart, guards: FolderGuards, uid: number): StartCheck {
  const { folder, at } = message;
  if (typeof at !== "string") return refused("a thread's copy stands for a folder by that folder's path");
  const alone = noCopyAt(at, guards);
  if (copyKey(folder, guards.dataDir) !== null && alone !== null) {
    return refused("given" in alone ? cannotStand(at, alone.given) : `${shown(at)} is no path a thread's copy can stand for`);
  }
  const held = checkCopy(folder, guards.dataDir, at, uid);
  if (!held.ok) return held;
  // Its sandbox holds the copy and nothing else of the folder's: not the folder itself, nor any place or kept folder.
  const through = given(message, guards.appDirs, [at, ...keptApart(guards.dataDir)]);
  if (through !== null) return refused(cannotStand(at, through));
  return {
    ...held, ...plain(), env: { SUROGATE_AT: at },
    replaced: `the copy of ${at} this thread works in was made again after the app looked at it`,
    named: (text) => said(text, { at, folder: held.path }),
  };
}

// The folder a landing writes, as a chat's; with the thread's copy, read-only, and the folder the
// app keeps the files a landing replaces in, each where the app keeps it for one folder, a folder
// of its own. It runs no command, and its helper is asked the land kind alone: the folder is the user's own.
function onLanding(message: HostStart, guards: FolderGuards, uid: number): StartCheck {
  const held = checkFolder(message.folder, guards);
  if (!held.ok) return held;
  const { copy, kept } = (typeof message.landing === "object" && message.landing !== null ? message.landing : {}) as Partial<NonNullable<HostStart["landing"]>>;
  const from = checkCopy(copy, guards.dataDir, held.path, uid);
  if (!from.ok) return refused(`this landing's copy is not a thread's copy of the folder: ${from.message}`);
  // The copy is one, so the app's data is there, and no part of its path is one the sandbox cannot be given.
  const data = realpathSync(guards.dataDir);
  if (kept !== keptAt(data, copyKey(copy, guards.dataDir)!)) return refused(`this landing's kept folder is not the one the app keeps for ${held.path}`);
  const notOwn = `the kept folder of ${held.path}'s landings is not a folder of the app's own`;
  // Its sandbox holds, of the app's data, the copy and the kept folder alone: no other thread's copy, nor the folder's history.
  const through = given(message, guards.appDirs, keptApart(guards.dataDir));
  if (through !== null) return refused(`this landing's sandbox would be given ${through}, and by it what the app keeps of other threads and folders`);
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
  const make = (): ReturnType<Start["make"]> => {
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
      return { made, env: { SUROGATE_KEPT_IS: isOf(there) } };
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
  };
  return {
    ...held, reads: [from.path], writes: [kept], env: { SUROGATE_COPY: from.path, SUROGATE_COPY_IS: isOf(from), SUROGATE_KEPT: kept }, commands: false, only: "land",
    readyMs: READY_MS, make, replaced: replaced(message.folder), named: asSaid,
  };
}

// The folder a landing was cut short in, as a chat's, with what that folder's landings keep, where the app keeps it for
// that folder and a folder of its own that is there: its helper puts back what the landing cut short by those records,
// and does nothing else. It is given no copy, runs no command, and is asked the land kind alone.
function onRecovery(message: HostStart, guards: FolderGuards, uid: number): StartCheck {
  const held = checkFolder(message.folder, guards);
  if (!held.ok) return held;
  const { kept } = (typeof message.recovery === "object" && message.recovery !== null ? message.recovery : {}) as Partial<NonNullable<HostStart["recovery"]>>;
  const data = realData(guards.dataDir);
  const landings = data === null ? null : join(data, "landings");
  const name = typeof kept === "string" && landings !== null && kept.startsWith(`${landings}/`) ? kept.slice(landings.length + 1) : "";
  // Only the folder's own: one kept for another folder names files that are not this one's, and would put them here.
  if (landings === null || KEPT_NAME.exec(name)?.[1] !== keyOf(message.folder) || kept !== join(landings, name)) {
    return refused(`what this recovery is given is not where the app keeps what ${held.path}'s landings replaced`);
  }
  const notOwn = `what the app keeps of ${held.path}'s landings is not a folder of the app's own`;
  // The kept folder, and the folder that holds every folder's: there, as folders of the app's own. Nothing is made.
  const there = (): Stats | null => {
    const [all, its] = [own(landings, uid), own(kept, uid)];
    return all === null || all === "missing" || its === null || its === "missing" ? null : its;
  };
  if (there() === null) return refused(notOwn);
  // Its sandbox holds, of the app's data, what the folder's landings keep alone: no thread's copy, nor the folder's history.
  const through = given(message, guards.appDirs, keptApart(guards.dataDir));
  if (through !== null) return refused(`this recovery's sandbox would be given ${through}, and by it what the app keeps of other threads and folders`);
  // Looked at again once the host holds the folder: a link put at it since would have its records read wherever it leads.
  const make = (): ReturnType<Start["make"]> => {
    const found = there();
    if (found === null) throw new Error(notOwn);
    return { made: [], env: { SUROGATE_KEPT_IS: isOf(found) } };
  };
  return {
    ...held, reads: [], writes: [kept], env: { SUROGATE_KEPT: kept }, commands: false, only: "land", readyMs: READY_MS, make,
    replaced: `the folder at ${message.folder} is not the one a landing was cut short in`, named: asSaid,
  };
}

// The starts that are no chat's, each by the field of its message that says so.
const STARTS = { at: onCopy, landing: onLanding, recovery: onRecovery } as const;

/**
 * What *message* starts a host on, checked as what it is: the folder it holds, as it resolves,
 * with its identity, and what the host needs of the start; or why no host may hold it. A chat's
 * folder, a landing's and a recovery's are checked as any chat's. A thread's copy, and what a
 * landing or a recovery is given, must be the app's own, where the app keeps them: *uid* is the
 * user whose they are.
 */
export function startOn(message: HostStart, home: string, appDirs: string[], uid = process.getuid?.() ?? -1): StartCheck {
  const guards: FolderGuards = { home, dataDir: message.dataDir, cacheDir: message.cacheDir, appDirs };
  const said = (Object.keys(STARTS) as Array<keyof typeof STARTS>).filter((field) => message[field] !== undefined);
  if (said.length > 1) return refused("a tool host is started on a chat's folder, on a thread's copy of one, or on a folder for a landing or a recovery: this start names more than one");
  return said[0] === undefined ? onFolder(message, guards) : STARTS[said[0]](message, guards, uid);
}
