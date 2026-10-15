// What an installed app on macOS reads of its install, how it installs an update, and how it removes itself from a Mac
// (spec, Section 9). The app is the bundle a person dragged from the disk image, <parent>/Surogate.app, which its code
// signature seals and Gatekeeper checks; nothing is ever run as root, and no administrator is asked for. Its record of
// where updates come from is root's alone, /Library/Application Support/Surogate/install.json, which an enterprise's
// kit or its MDM places; without one, the public base and channel built into the app (vm/image.ts, BUILT_IN). Its
// release keys are the bundle's own, Contents/Resources/release-keys, in the install script's form.
//
// An update is unpacked beside the bundle, on its volume, by the archive rules the install script keeps; checked there
// by the app, then again by surogate-helper's apply (native/helper), which waits for the app to quit and exchanges the
// two bundles' names in one rename. The version it replaced is kept beside it, until a start of the app reaches its
// window. A user who may not change the bundle's folder is told that an administrator installs the disk image; an app
// run from a read-only disk, or from the copy macOS makes of one opened where it was downloaded, says so. Electron-free.

import type { ChildProcess } from "node:child_process";
import { accessSync, chmodSync, closeSync, constants, lstatSync, mkdirSync, openSync, readdirSync, readFileSync, readlinkSync, realpathSync, renameSync, rmSync } from "node:fs";
import { rm } from "node:fs/promises";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { StringDecoder } from "node:string_decoder";

import { spawnClean } from "../clean-child.js";
import type { Fetch } from "../download.js";
import { HELPER } from "../native-helper.js";
import { forThisOs } from "../platform.js";
import type { Places } from "./places.js";
import { type Applied, type Held, RESULT, type Staged, type UpdatesOptions } from "./updates.js";

/** The install record an administrator places, root's own: the base and channel updates come from. */
export const MACHINE_RECORD = "/Library/Application Support/Surogate/install.json";

/** The release's disk image under <base>/desktop/, by the name the release keeps for its newest: what an administrator installs. */
export const DISK_IMAGE = "darwin-arm64/Surogate.dmg";

const APP = "Surogate.app";
// What the helper and tar are started with of the environment: nothing but a PATH of the system's.
const ENV: NodeJS.ProcessEnv = { PATH: "/usr/bin:/bin" };
// What the helper says once it has taken its arguments and waits for the app to quit (native/helper, main.swift).
const WAITING = "waiting";
// The most that is kept of what a program said, its end.
const SAID_MAX = 4000;

/** The app bundle whose program *exec* is, <parent>/<name>.app for <parent>/<name>.app/Contents/MacOS/<program>; null for one in none. */
export function bundleOf(exec: string = process.execPath): string | null {
  return /^(\/.+\.app)\/Contents\/MacOS\/[^/]+$/.exec(exec)?.[1] ?? null;
}

/** Where an update to *version* is staged beside *app*, on its volume: a name Launch Services takes for no app. */
export const stagingOf = (app: string, version: string): string => join(dirname(app), `.Surogate-${version}.staging`);
// Where the tarball is unpacked, before the tree is checked and staged.
const unpackingOf = (app: string, version: string): string => join(dirname(app), `.Surogate-${version}.unpack`);

// Why an update is not installed where the app runs: macOS's own copy of an app opened where it was downloaded (App
// Translocation, under a folder of that name), which is read-only and goes at the app's quit; and a read-only disk, as
// the disk image is.
const TRANSLOCATED = "macOS runs it from a copy of its own, as it does an app opened where it was downloaded. Move Surogate to Applications, and open it from there";
const READ_ONLY = (parent: string) => `${parent} is on a disk that cannot be written to. Copy Surogate to Applications, and open it from there`;

/**
 * Why the app *app* cannot be updated where it is, by this user, *uid*, or null where it can: nobody can where it runs
 * from macOS's own copy of it, or from a read-only disk, or under another name than Surogate.app; and an administrator
 * installs the disk image where the folder that holds it is not this user's to change, or the bundle is another's.
 */
export function heldAt(app: string, uid: number = process.getuid?.() ?? -1): Held | null {
  if (app.includes("/AppTranslocation/")) return { why: TRANSLOCATED };
  if (basename(app) !== APP) return { why: `it is named ${basename(app)}. Name it ${APP} again, and it updates itself` };
  const parent = dirname(app);
  try {
    accessSync(parent, constants.W_OK);
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EROFS" ? { why: READ_ONLY(parent) } : { download: DISK_IMAGE };
  }
  const found = lstatSync(app, { throwIfNoEntry: false });
  return found?.isDirectory() && found.uid === uid ? null : { download: DISK_IMAGE };
}

// Where *path* leads, as realpath -m has it (the install script's check of a link): each link on the way followed, and
// each part that is not there taken as it is written. Null for a path of more links than the system follows (ELOOP).
function leadsTo(path: string): string | null {
  const pending = path.split("/").reverse();
  let at = "/";
  let links = 0;
  while (pending.length > 0) {
    const part = pending.pop()!;
    if (part === "" || part === ".") continue;
    if (part === "..") {
      at = dirname(at);
      continue;
    }
    const next = at === "/" ? `/${part}` : `${at}/${part}`;
    let link = false;
    try {
      link = lstatSync(next).isSymbolicLink();
    } catch {
      // not there, or below a file: taken as it is written
    }
    if (link) {
      if ((links += 1) > 32) return null;
      const target = readlinkSync(next);
      if (isAbsolute(target)) at = "/";
      pending.push(...target.split("/").reverse());
      continue;
    }
    at = next;
  }
  return at;
}

const SPECIAL = "the release's archive holds a special file, a set-id file or a hard link";

/**
 * Why the tree at *top*, a release's Surogate.app as unpacked, is not one the app takes, or null: the install script's
 * archive rules (apply in release/install.sh). Nothing in it is a special file, has a set-id bit, or is a hard link; no
 * link names a whole path, climbs out of the tree as it is written, or leaves it as it resolves; and it is Surogate's
 * bundle, its program, its helper, its Info.plist and its package each a file of its own.
 */
export function treeRefusal(top: string): string | null {
  const root = realpathSync(top);
  const inside = (path: string | null) => path !== null && path.startsWith(`${root}/`);
  const named = (path: string) => path.slice(root.length + 1);
  const walk = (path: string): string | null => {
    const found = lstatSync(path);
    if ((found.mode & 0o6000) !== 0) return SPECIAL;
    if (found.isSymbolicLink()) {
      const target = readlinkSync(path);
      if (isAbsolute(target)) return `the release's archive holds a link to a whole path: ${named(path)}`;
      return inside(resolve(dirname(path), target)) && inside(leadsTo(path)) ? null : `the release's archive links outside itself: ${named(path)}`;
    }
    if (found.isDirectory()) {
      for (const name of readdirSync(path)) {
        const why = walk(join(path, name));
        if (why) return why;
      }
      return null;
    }
    return found.isFile() && found.nlink === 1 ? null : SPECIAL;
  };
  const why = walk(root);
  if (why) return why;
  const file = (path: string, program: boolean) => {
    const found = lstatSync(join(root, path), { throwIfNoEntry: false });
    return found !== undefined && found.isFile() && (!program || (found.mode & 0o111) !== 0);
  };
  const app = file("Contents/MacOS/Surogate", true) && file("Contents/MacOS/surogate-helper", true) && file("Contents/Info.plist", false)
    && file("Contents/Resources/app/package.json", false);
  return app ? null : "the release's archive is not Surogate Desktop";
}

// What *program* came to with *args*: its exit code, null where it did not run or a signal ended it, and the end of what
// it said on stderr. Started as every program the app starts (clean-child.ts), with *env* alone.
function ran(program: string, args: string[], env: NodeJS.ProcessEnv): Promise<Applied> {
  return new Promise((resolve) => {
    let child: ChildProcess;
    try {
      child = spawnClean(program, args, { stdio: ["ignore", "ignore", "pipe"], env });
    } catch (error) {
      return resolve({ code: null, said: error instanceof Error ? error.message : String(error) });
    }
    const decoder = new StringDecoder("utf8");
    let said = "";
    child.once("error", (error) => resolve({ code: null, said: error.message }));
    child.stderr?.on("data", (chunk: Buffer) => {
      said = (said + decoder.write(chunk)).slice(-SAID_MAX);
    });
    child.once("close", (code, signal) => resolve({ code, said: (code === null ? `${said}\n${program} was stopped by ${signal}` : said + decoder.end()).trim() }));
  });
}

// The last line of what was said, or *otherwise*.
const lastLine = (said: string, otherwise: string) => said.split("\n").map((line) => line.trim()).filter((line) => line !== "").at(-1) ?? otherwise;

// How a release's tarball is unpacked: macOS's tar, bsdtar, as the user, with no owner of the archive's, no set-id bit,
// file flag, extended attribute or ACL (--no-same-owner, --no-same-permissions, as the install script unpacks), and no
// AppleDouble metadata applied (--no-mac-metadata, bsdtar's on macOS alone, which this module's tests elsewhere run
// without). Nothing of the bundle's signature is in an attribute: each of its programs carries its own, and
// Contents/_CodeSignature/CodeResources seals every other file.
const TAR = "/usr/bin/tar";
const MAC_METADATA = forThisOs({ darwin: ["--no-mac-metadata"] }) ?? [];
const UNPACK = (tarball: string, into: string) => ["-xzf", tarball, "-C", into, "--no-same-owner", "--no-same-permissions", ...MAC_METADATA];

/**
 * *tarball*, the release of *version*, unpacked beside *app* on its volume by the archive rules (treeRefusal), and
 * staged there: the staged bundle's path. What an earlier try left under either name goes first, a link by itself.
 * Throws why not, and leaves nothing.
 */
export async function stage(tarball: string, app: string, version: string): Promise<string> {
  const staged = stagingOf(app, version);
  const work = unpackingOf(app, version);
  for (const path of [staged, work]) rmSync(path, { recursive: true, force: true });
  mkdirSync(work, { mode: 0o700 });
  try {
    const unpacked = await ran(TAR, UNPACK(tarball, work), ENV);
    if (unpacked.code !== 0) throw new Error("the release's archive could not be unpacked");
    const top = join(work, APP);
    const found = lstatSync(top, { throwIfNoEntry: false });
    if (readdirSync(work).length !== 1 || !found?.isDirectory()) throw new Error(`the release's archive holds more than ${APP}/`);
    const why = treeRefusal(top);
    if (why) throw new Error(why);
    settleModes(top);
    renameSync(top, staged);
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
  return staged;
}

// Each file and folder of the tree at *path* its user's to write and no one else's (u+w,go-w): tar gives it the user's
// umask, and the code signature seals no mode. So the next version's own files are its own (updates.ts, bundled), and
// the version it replaces can be removed in turn. A link has no mode of its own.
function settleModes(path: string): void {
  const found = lstatSync(path);
  if (found.isSymbolicLink()) return;
  chmodSync(path, ((found.mode & 0o7777) | 0o200) & ~0o022);
  if (found.isDirectory()) for (const name of readdirSync(path)) settleModes(join(path, name));
}

// The version and state schema the release's manifest, as the check kept it, names.
function manifestOf(path: string): { version: string; stateSchema: number } {
  try {
    const { version, stateSchema } = JSON.parse(readFileSync(path, "utf8")) as { version?: unknown; stateSchema?: unknown };
    if (typeof version === "string" && Number.isSafeInteger(stateSchema)) return { version, stateSchema: stateSchema as number };
  } catch {
    // not JSON: as no manifest
  }
  throw new Error(`${path} is not the release's manifest`);
}

/**
 * The app's own check of the bundle *staged* for *release*, before it quits: its package names the release's version and
 * state schema; and its helper, *helper*, takes it as it will again before the swap (surogate-helper verify): signed
 * whole by this app's team, accepted by Gatekeeper, of that version, and newer than *app*. Throws why not.
 */
export async function checkStaged(staged: string, app: string, release: { version: string; stateSchema: number }, helper: string, env: NodeJS.ProcessEnv = ENV): Promise<void> {
  const own = join(staged, "Contents", "Resources", "app", "package.json");
  let read: { version?: unknown; stateSchema?: unknown } = {};
  try {
    if (lstatSync(own).isFile()) read = JSON.parse(readFileSync(own, "utf8")) as typeof read;
  } catch {
    // no package of its own
  }
  if (read.version !== release.version || read.stateSchema !== release.stateSchema) throw new Error(`the release's ${APP} is not the release its manifest names`);
  const { code, said } = await ran(helper, ["verify", "--staged", staged, "--app", app, "--version", release.version], env);
  if (code !== 0) throw new Error(lastLine(said, `its helper's check of it ended ${code ?? "with no exit code"}`));
}

// The locks of flock(2)'s kind that macOS's open(2) takes as it opens a file (Apple, open(2): O_SHLOCK and O_EXLOCK, and
// with O_NONBLOCK, EAGAIN where the file is locked), which Node names neither of and has no flock for. A lock is the
// open file's, and lasts while any descriptor of it is open: the helper's descriptor 3 keeps the app's. Linux takes
// none: there the start's check finds none.
const LOCKS = forThisOs({ darwin: { shared: 0x10, exclusive: 0x20 } }) ?? { shared: 0, exclusive: 0 };

/**
 * Whether surogate-helper is applying an update downloaded into *cache*, the updates folder: a result file there that
 * is locked, as the app locks the one it hands the helper's apply (started), for as long as the helper holds it. A
 * start that finds one touches nothing of the update's, and leaves the helper to open the app (handoff.ts).
 */
export function applyingUnder(cache: string): boolean {
  let names: string[];
  try {
    names = readdirSync(cache);
  } catch {
    return false; // no updates folder: nothing downloaded, nothing applied
  }
  for (const name of names) {
    let fd: number | undefined;
    try {
      fd = openSync(join(cache, name, RESULT), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK | LOCKS.shared);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EAGAIN") return true;
    } finally {
      if (fd !== undefined) closeSync(fd);
    }
  }
  return false;
}

// *helper* started with *args* to go on once the app has quit, as its own process, which outlives the app: answered once
// it says it waits, with restarts, as the app may then quit; or, where it ends first, as it ended. Where *result* names a
// file, made afresh and locked as it is made (applyingUnder), the helper's descriptor 3 is that file, where it keeps how
// it ended for the app's next start.
function started(helper: string, args: string[], env: NodeJS.ProcessEnv, result: string | null = null): Promise<Applied> {
  return new Promise((resolve) => {
    let kept: number | undefined;
    let child: ChildProcess;
    try {
      if (result !== null) {
        rmSync(result, { recursive: true, force: true });
        kept = openSync(result, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW | LOCKS.exclusive, 0o600);
      }
      child = spawnClean(helper, args, { stdio: ["ignore", "pipe", "pipe", ...(kept === undefined ? [] : [kept])], detached: true, env });
    } catch (error) {
      return resolve({ code: null, said: error instanceof Error ? error.message : String(error) });
    } finally {
      // The child holds its own copy once it is started.
      if (kept !== undefined) closeSync(kept);
    }
    const decoder = new StringDecoder("utf8");
    let said = "";
    let heard = "";
    let answered = false;
    const answer = (applied: Applied) => {
      if (answered) return;
      answered = true;
      resolve(applied);
    };
    child.once("error", (error) => answer({ code: null, said: error.message }));
    child.stderr?.on("data", (chunk: Buffer) => {
      said = (said + decoder.write(chunk)).slice(-SAID_MAX);
    });
    child.stdout?.setEncoding("utf8").on("data", (chunk: string) => {
      heard += chunk;
      if (!heard.split("\n").slice(0, -1).includes(WAITING)) return;
      // It waits: the app lets it go, and quits.
      child.stdout?.destroy();
      child.stderr?.destroy();
      child.unref();
      answer({ code: 0, said: "", restarts: true });
    });
    child.once("close", (code, signal) => answer({ code, said: (code === null ? `${said}\nits helper was stopped by ${signal}` : said + decoder.end()).trim() }));
  });
}

/**
 * What installs a downloaded release over *app* with *helper*, surogate-helper, for this app's process *pid*: the
 * release unpacked and staged beside the app, and checked; then the helper's apply, which waits for the app to quit,
 * checks the staged bundle again, swaps it in, and opens the app. Once the helper waits, the app quits. Nothing is
 * staged for an app this user cannot update where it is (heldAt). *env*: what the helper is started with.
 */
export function updateRun(helper: string, app: string, pid: number = process.pid, env: NodeJS.ProcessEnv = ENV): (files: Staged) => Promise<Applied> {
  return async (files) => {
    let release: { version: string; stateSchema: number };
    let staged: string | undefined;
    try {
      const held = heldAt(app);
      if (held) throw new Error("why" in held ? held.why : `this user may not change ${dirname(app)}: an administrator installs the update`);
      release = manifestOf(files.manifest);
      staged = await stage(files.tarball, app, release.version);
      await checkStaged(staged, app, release, helper, env);
    } catch (error) {
      // A bundle the app's check refused is not left beside the app.
      if (staged !== undefined) rmSync(staged, { recursive: true, force: true });
      return { code: null, said: error instanceof Error ? error.message : String(error) };
    }
    const args = ["apply", "--wait", String(pid), "--staged", staged, "--app", app, "--version", release.version];
    return started(helper, args, env, join(dirname(files.tarball), RESULT));
  };
}

/**
 * What removes the app from this Mac with *helper*, once the app has quit, for this app's process *pid*: the key in this
 * user's Keychain that seals the app's tokens, then the user's own folders it is given, *forget*, and *app* moved to the
 * Trash, where this user may change its folder (heldAt). Another user's data, and every chat's folder, are never named.
 */
export function removeRun(helper: string, app: string | null, pid: number = process.pid, env: NodeJS.ProcessEnv = ENV): (forget: string[]) => Promise<Applied> {
  return (forget) => {
    const trash = app !== null && heldAt(app) === null ? ["--app", app] : [];
    return started(helper, ["remove", "--wait", String(pid), ...trash, ...forget.flatMap((folder) => ["--forget", folder])], env);
  };
}

/** The removal's question, as the app is where it is: what goes, and what stays. */
export function removalAsked(app: string | null): { message: string; detail: string; checkbox: string } {
  const held = app === null ? { why: "" } : heldAt(app);
  const itself = held === null ? "Surogate quits and goes to the Trash, and its login item and its key in your Keychain are removed."
    : "download" in held ? `Surogate's login item and its key in your Keychain are removed. Surogate itself stays in ${dirname(app!)}: an administrator removes it there.`
    : "Surogate's login item and its key in your Keychain are removed. Surogate itself runs from where it cannot be changed: drag it to the Trash once it has quit.";
  return {
    message: "Remove Surogate from this Mac?",
    detail: `${itself} The folders your chats work on are left as they are.`,
    checkbox: "Also delete your Surogate data on this Mac: your sign-in, your settings, the sandbox's disks and the updates it downloaded",
  };
}

/** The user's own folders of the app's that a removal deletes where asked: its data, and its cache. */
export const forgotten = (places: Places): string[] => [places.root, places.cache];

// What an update leaves beside the app, each by its name: the version it replaced, and a staged bundle or an unpack
// that never became the app.
const LEFT = /^\.Surogate-(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:previous|staging|unpack)$/;

/**
 * Once a start of the app *app* has reached its main window: what an update left beside it goes, the version it
 * replaced too. Only this user's own, *uid*'s, real folders of those names; nothing else beside the app is touched. A
 * start that fails before its window leaves the version replaced where it is.
 */
export async function reachedWindow(app: string | null, uid: number = process.getuid?.() ?? -1): Promise<void> {
  if (app === null) return;
  const parent = dirname(app);
  let names: string[];
  try {
    names = readdirSync(parent);
  } catch {
    return; // a folder that cannot be listed holds nothing of the app's to remove
  }
  for (const name of names.filter((named) => LEFT.test(named))) {
    const path = join(parent, name);
    const found = lstatSync(path, { throwIfNoEntry: false });
    if (found?.isDirectory() && found.uid === uid) await rm(path, { recursive: true, force: true });
  }
}

/**
 * An installed app's updates on macOS: the record an administrator placed, or the public base and channel without
 * one; the release keys in the bundle *app*, which also marks the version installed; the release unpacked and swapped
 * in beside it by *helper*; and nothing staged, or downloaded, where this user cannot update it (heldAt).
 */
export function installedUpdates(version: string, cache: string, fetch: Fetch, signal: AbortSignal, app: string | null = bundleOf(), helper: string = HELPER): UpdatesOptions {
  const bundle = app ?? `/${APP}`;
  const resources = join(bundle, "Contents", "Resources");
  return {
    version, record: MACHINE_RECORD, rootOwned: true, helper: join(resources, "release-keys"), installed: join(resources, "app", "package.json"), cache, fetch, signal,
    apply: updateRun(helper, bundle),
    held: () => (app === null ? { why: "it runs from no app bundle" } : heldAt(app)),
  };
}
