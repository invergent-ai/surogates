// Updates inside the app (spec, Section 9, "In-app updates"): at its start and every 6 hours,
// the signed latest.json of the base the install script recorded, checked here against the
// release keys this computer trusts, the ones the helper pkexec runs lists; a newer release for
// this computer and the recorded channel downloaded as the user into their cache, resumed; then
// Update available. Installing it runs the root helper on the downloaded files, under polkit in
// an installed app; the helper checks all of them again before it applies anything.
// Electron-free.

import { createPublicKey, type KeyObject, verify } from "node:crypto";
import {
  closeSync, constants, existsSync, fchmodSync, lstatSync, mkdirSync, openSync, readdirSync, readFileSync, readlinkSync, realpathSync, renameSync, rmSync, writeFileSync,
} from "node:fs";
import { basename, dirname, join } from "node:path";

import { download, type Fetch, hashOf, sizeOf } from "../download.js";
import { adminsOwn } from "../files/admins-own-win32.js";
import { ntat } from "../files/ntat.js";
import { forThisOs, type Target, type TargetFiles, TARGETS, targetOf } from "../platform.js";
import { installBase, installersOwn, rootsOwn } from "../vm/image.js";

export const CHECK_MS = 6 * 60 * 60 * 1000;
// A check that failed is tried again sooner: after a minute, and each time it fails again after
// twice as long, to the six hours of any other. The network that comes up after the app, a
// download a sleep cut, and a base asked between a release's two uploads are each a failure.
const RETRY_MS = 60 * 1000;

/** How long after a check the next is due, *failed* being how many have failed in a row. */
export function nextCheckIn(failed: number): number {
  return failed === 0 ? CHECK_MS : Math.min(CHECK_MS, RETRY_MS * 2 ** (failed - 1));
}

/**
 * *updates* checked now, and again whenever the next check is due (nextCheckIn), until *signal*,
 * the app's quit. A check that takes none says why to *report*; one the quit cut short says
 * nothing. What it returns checks at once, as when the computer wakes: a wait counts no time asleep.
 */
export function keepChecked(updates: { check(): Promise<void> }, signal: AbortSignal, report: (error: unknown) => void): () => void {
  let failed = 0;
  let due: NodeJS.Timeout | undefined;
  let running = false;
  const check = (): void => {
    clearTimeout(due);
    if (signal.aborted || running) return;
    running = true;
    void updates.check().then(() => {
      failed = 0;
    }, (error: unknown) => {
      failed += 1;
      if (!signal.aborted) report(error);
    }).finally(() => {
      running = false;
      if (!signal.aborted) due = setTimeout(check, nextCheckIn(failed)).unref();
    });
  };
  signal.addEventListener("abort", () => clearTimeout(due), { once: true });
  check();
  return check;
}
// How a tarball begins: a gzip member's magic number.
const GZIP_MAGIC = Buffer.from([0x1f, 0x8b]);
// Windows's payload, an MSI: a compound file's header.
const MSI_MAGIC = Buffer.from([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1]);
// A manifest is one short line, and its signature Ed25519's 64 bytes: anything longer is not one.
const MANIFEST_MAX = 4096;
const SIGNATURE_MAX = 64;
// What jq makes of a byte that is no UTF-8: the root helper takes no manifest that holds one.
const REPLACEMENT = "\uFFFD";
// A manifest as JSON spells one: its tokens, with JSON's own blanks between them, and each number
// in 17 digits at most. It is the root helper's own pattern (one_object in release/install.sh),
// which holds jq to JSON: jq also reads +1, 01, 1., .5, nan and infinity, and rounds a number of
// more digits to 17 before it makes a number of it, so that 137438953472.000015 is whole to
// JSON.parse and not to jq.
const SPELLED = /^(?:[ \t\r\n[\]{}:,]|"(?:[^"\\]|\\[^\n])*"|(?:true|false|null|-?(?!(?:[0-9]\.?){18})(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)(?![^ \t\r\n[\]{}:,"]))*$/;
// How many fields and places down a manifest's values go at most: jq reads to a depth of its own,
// which changes with jq, and the helper to this one.
const DOWN_MAX = 64;
// How long the base may take to answer for either.
const ASK_MS = 30_000;
// A version: x.y.z in the ten digits, and no part with a zero before it, as the root helper has it:
// dpkg reads 1.2.03 as 1.2.3, and a version's folder is named as it is written.
const VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
// A release, as its signed manifest names it (release/publish.sh sign).
export interface Release {
  version: string;
  channel: string;
  platform: TargetFiles["platform"];
  arch: TargetFiles["arch"];
  url: string; // relative to <base>/desktop/
  sha256: string;
  size: number;
  stateSchema: number;
}

// A downloaded release's files, in the user's cache, as the root helper is handed them. Its payload is
// the tarball, or on Windows the MSI, release.msi, which surogate-admin.exe installs.
export interface Staged {
  manifest: string;
  signature: string;
  tarball: string;
}

export type UpdateState =
  | { state: "none" }
  | { state: "available"; version: string; files: Staged }
  // The root helper runs, after the administrator's approval it waits for.
  | { state: "installing"; version: string }
  // Polkit refused it: the user is no administrator, or none approved.
  | { state: "refused"; version: string; files: Staged }
  | { state: "failed"; version: string; files: Staged; why: string }
  // Installed for every user of this computer: the app runs it once it restarts.
  | { state: "installed"; version: string }
  // Approved by an administrator on Windows: the app quits for the MSI to replace it, and is started again after.
  | { state: "restarting"; version: string }
  // The helper pkexec would run is not one the app can take: nothing is installed until the
  // install script has put it right.
  | { state: "broken" }
  // The base's newest release is signed by no key the helper lists, and has been for a day of
  // failed checks: nothing of it is taken.
  | { state: "unsigned" };

// What the root helper's run came to: its exit code, null when it did not run or a signal ended
// it; and the last of what it said, with how it ended where no exit code says. *restarts*: the
// install goes on once the app has quit, as Windows's does, which starts the app again after.
export interface Applied {
  code: number | null;
  said: string;
  restarts?: boolean;
}

// pkexec's own answers where no administrator approved: 126, the prompt was dismissed; and 127
// with a line that begins with these words, authorization was refused or there was no one to ask.
// It is started with no locale, so its words are English. They are looked for on every line: the
// helper is not run at a refusal, so no one else writes them, and another program may write a
// line of its own first, as the loader does of a library it cannot preload. Its 127 is also what
// it answers when it cannot run the helper at all (the helper gone or no program, pkexec not
// set-id, no system bus), in other words: no administrator can help then, and it is a failure,
// said as pkexec says it.
const DISMISSED = 126;
const NOT_AUTHORIZED = 127;
const AS_ANOTHER_USER = "Error executing command as another user:";
// Windows's answer where UAC's prompt had no administrator's approval (ERROR_CANCELLED), which no
// exit code on Linux can be.
const UAC_DISMISSED = 1223;
const refusal = (code: number | null, said: string) => code === DISMISSED || code === UAC_DISMISSED
  || (code === NOT_AUTHORIZED && said.split("\n").some((line) => line.startsWith(AS_ANOTHER_USER)));
// How the helper begins each line it says (fail and say in release/install.sh).
const HELPER_SAYS = "Surogate Desktop: ";
// The most of a failure's reason that the line shows, by the sidebar's own width: at its narrowest
// (240 px, of which the line's text has 200 at 12.5 px) a line holds some 31 characters, so this
// is about eight lines there, and five at its widest (370 px). The helper's own lines fit, but for
// one that names a long path; the whole of what was said is in the log.
const WHY_MAX = 240;

// Why an install failed, of all that was said: one line. How the run ended, where no exit code
// says: the system's words for a helper that could not be started, or a signal's name. Else the
// helper's own last line, by its name, whatever was written after it, as a cleanup's complaint is.
// Else pkexec's first line, for its own failure, or the last line said.
function reason(code: number | null, said: string): string {
  const lines = said.split("\n").map((line) => line.trim()).filter((line) => line !== "");
  if (code === null) return shown(lines.at(-1) ?? "its helper did not run");
  const own = lines.findLast((line) => line.startsWith(HELPER_SAYS));
  if (own !== undefined) return shown(own.slice(HELPER_SAYS.length));
  return shown((code === NOT_AUTHORIZED ? lines[0] : lines.at(-1)) ?? `its helper exited ${code}`);
}

// *why* as the line shows it: WHY_MAX characters at most, counted as letters, its end marked where
// it was cut. But for the helper's refusal of a release no trusted key signed, which is shown
// whole, as the app's own line for one is: what mends it is the sentence's end.
function shown(why: string): string {
  if (why === HELPER_UNSIGNED) return why;
  const letters = [...why];
  return letters.length <= WHY_MAX ? why : `${letters.slice(0, WHY_MAX - 1).join("")}\u2026`;
}
// The update's line in the sidebar: what the user is told, and the button they act on it with.
export interface UpdateLine {
  text: string;
  button: string | null;
}

// What follows the name of a release that no key this computer trusts has signed, in the install
// script's own sentence for an install and an apply (not_signed in release/install.sh): what is
// so, that nothing was installed and the version here stays, and the one way on that this
// computer's own keys check. A computer that missed the release which brought a new release key
// takes nothing the new key signs, and no one at it can know why. Nothing here names a removal:
// that throws the computer's keys away, and what they did not sign may be a forgery as well.
export const NOT_SIGNED = " is not signed by a release key this computer trusts: nothing was installed, and Surogate Desktop stays at its version. "
  + "If Surogate's release key has changed since this computer's last update, run Surogate Desktop's install script with --version of the release that brought the new key";
// The helper's refusal of the release it was handed, in that sentence.
const HELPER_UNSIGNED = `the release's manifest${NOT_SIGNED}`;

// A release that none of the keys asked has signed.
export class Unsigned extends Error {}
// When the line says so: not at the first such check, which a release caught between two of its
// uploads, a server's bad hour or a forgery each make, but once it has stood for a day of failed
// checks. Both hold: this many checks in a row found it so, and the first of them is this old.
// By the checks' own series (nextCheckIn) an app that runs through the day makes twelve in it.
export const UNSIGNED_CHECKS = 8;
export const UNSIGNED_MS = 24 * 60 * 60 * 1000;

// What a person does on this OS: on Windows the next try asks UAC for an administrator itself, and an
// install is put right by the MSI; elsewhere by the install script.
const TRY_AGAIN = forThisOs({ win32: "Install as an administrator…" }) ?? "Try again";
const BROKEN = forThisOs({ win32: "Surogate cannot update itself. Install Surogate again." }) ?? "Surogate cannot update itself. Run the install script again.";

/** The sidebar's line for *state*, or null for none. */
export function updateLine(state: UpdateState | null): UpdateLine | null {
  switch (state?.state) {
    case "available":
      return { text: `Update available: Surogate ${state.version}`, button: "Restart to update" };
    case "installing":
      return { text: `Installing Surogate ${state.version}…`, button: null };
    case "refused":
      return { text: "An administrator needs to install this update.", button: TRY_AGAIN };
    case "failed":
      return { text: `Surogate could not install its update: ${state.why}`, button: "Try again" };
    case "installed":
      return { text: `Surogate ${state.version} is installed.`, button: "Restart" };
    case "restarting":
      return { text: `Surogate restarts to install ${state.version}…`, button: null };
    case "broken":
      return { text: BROKEN, button: null };
    case "unsigned":
      return { text: `Surogate's newest release${NOT_SIGNED}`, button: null };
    default:
      return null;
  }
}

/** Whether version *a* comes after *b*, each x.y.z, compared part by part as numbers. */
export function newer(a: string, b: string): boolean {
  const [x, y] = [VERSION.exec(a), VERSION.exec(b)];
  if (!x || !y) return false;
  for (let at = 1; at <= 3; at += 1) if (Number(x[at]) !== Number(y[at])) return Number(x[at]) > Number(y[at]);
  return false;
}

/**
 * The release keys *helper* lists, the ones this computer trusts when it is the helper pkexec
 * runs: each Ed25519 public key in its RELEASE_KEYS list, as release/install.sh writes it, and
 * none from anywhere else in the script. A list with an entry that is no such key is no list, as
 * it is none to the helper. With *rootOwned*, a helper that is not root's own word, or that pkexec could not run
 * (rootsOwn), is not read.
 */
export function releaseKeys(helper: string, rootOwned = false): KeyObject[] {
  if (rootOwned) keysOwn(helper);
  const keys = listedKeys(readFileSync(helper, "utf8"));
  if (keys.length === 0) throw new Error(`${helper} trusts no release key`);
  return keys;
}

// Whose word the release keys must be: on Windows the install's resources\release-keys, an
// administrator's (adminsOwn); elsewhere the helper pkexec runs, root's and a program.
const keysOwn: (path: string) => void = forThisOs<(path: string) => void>({ win32: adminsOwn }) ?? ((path) => rootsOwn(path, true));

const BEGIN_KEY = "-----BEGIN PUBLIC KEY-----";
const END_KEY = "-----END PUBLIC KEY-----";
// A key's one line between them, as the script's reader has it (listed in release/install.sh): an
// Ed25519 public key as OpenSSL writes one, twelve bytes that say so and the key's 32 in base64,
// whose last letter holds four bits of a byte and none of its own.
const ED25519_LINE = /^MCowBQYDK2VwAyEA[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$/;

/**
 * The release keys that *script*, an install script, lists: read as the script's own reader
 * reads a helper's list (listed in release/install.sh), line by line and by its one form, and
 * never run. The list is one `RELEASE_KEYS=(` on a line of its own, then each key as OpenSSL
 * writes an Ed25519 key, its first line behind a quote, its one line of letters, and its last
 * before a quote, then `)` on a line of its own. Any other spelling is no list, and no key of it
 * is read, whatever bash or OpenSSL would make of it: a comment in it, other quotes, one line, an
 * indented key, a key's letters over two lines, a key of another kind, a second list. It never
 * throws, and answers the keys an install would trust, each as it is written.
 */
export function listedKeys(script: string): KeyObject[] {
  const entries: string[] = [];
  let at: "before" | "open" | "key" | "closed" | "wrong" = "before";
  let entry = "";
  const lines = script.split("\n");
  // As the script reads: a last line with no newline is a line, and the end behind a newline is none.
  if (lines.at(-1) === "") lines.pop();
  for (const line of lines) {
    // A line that sets the list: the first, in its form, opens it; any other, a second list or one
    // added to it among them, leaves no list at all.
    if (/^[ \t]*RELEASE_KEYS\+?=/.test(line)) {
      if (at === "before" && /^[ \t]*RELEASE_KEYS=\($/.test(line)) {
        at = "open";
        continue;
      }
      at = "wrong";
    }
    if (at === "open") {
      if (/^[ \t]*'-----BEGIN PUBLIC KEY-----$/.test(line)) [entry, at] = [BEGIN_KEY, "key"];
      else at = /^[ \t]*\)$/.test(line) ? "closed" : "wrong";
    } else if (at === "key") {
      if (entry === BEGIN_KEY && ED25519_LINE.test(line)) entry += `\n${line}`;
      else if (line === `${END_KEY}'` && entry !== BEGIN_KEY) [entries[entries.length], at] = [`${entry}\n${END_KEY}`, "open"];
      else at = "wrong";
    }
  }
  // Each entry is an Ed25519 key in its one spelling, by the line's own form: any 32 bytes load as one.
  return at === "closed" ? entries.map((pem) => createPublicKey(pem)) : [];
}

// Whether *text* escapes the first half of a pair with no escape of its second half behind it: jq
// refuses such a text, and JSON.parse takes the half alone. Each escape is found from the text's
// start, so that the backslash an escape writes begins none.
function halfAlone(text: string): boolean {
  for (const [, first, second] of text.matchAll(/\\(?:u([0-9a-f]{4})(?=(?:\\u([0-9a-f]{4}))?)|[^u])/gi)) {
    if (first !== undefined && /^d[89ab]/i.test(first) && !(second !== undefined && /^d[c-f]/i.test(second))) return true;
  }
  return false;
}

// Whether a field is named twice in any one object of *text*, which is JSON: of the two, each
// reader keeps the last and says nothing of the first, and where the first is deeper than one jq
// reads at all, that jq refuses the text and JSON.parse drops it unread. Asked of the text
// itself, word by word, before any field has replaced another. A name is compared as jq reads
// it: its escapes read, and the second half of a pair that is escaped alone as the replacement
// character jq makes of it.
function namedTwice(text: string): boolean {
  // The names so far of each object that is open; a list, which has none, has an empty one.
  const open: Array<Set<string>> = [];
  let word = "";
  for (const [token] of text.matchAll(/"(?:[^"\\]|\\[^\n])*"|[{}[\]:]/g)) {
    if (token === "{" || token === "[") open.push(new Set());
    else if (token === "}" || token === "]") open.pop();
    else if (token !== ":") word = token;
    else {
      // The word before a colon is a field's name, in the object that is open.
      const name = (JSON.parse(word) as string).replace(/(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g, REPLACEMENT);
      const names = open.at(-1)!;
      if (names.has(name)) return true;
      names.add(name);
    }
  }
  return false;
}

// Whether none of *value*'s values is more than *steps* fields and places down, as jq counts a path.
function within(value: unknown, steps: number): boolean {
  if (typeof value !== "object" || value === null) return true;
  return Object.values(value).every((held) => steps > 0 && within(held, steps - 1));
}

/**
 * The one JSON object that *bytes* are, as the root helper reads a manifest and a mark (one_object
 * in release/install.sh): 4096 bytes at most, on one line whose newline is their last byte, and
 * one JSON document, which is an object. Null where they are not. The helper reads with jq and
 * the app with JSON.parse, and each takes what the other takes and nothing else: UTF-8 or nothing,
 * with no replacement character, which is what jq makes of any other byte; JSON as it is spelled,
 * each number in 17 digits at most (SPELLED); no first half of a pair escaped alone (halfAlone);
 * no field named twice in any one object (namedTwice); and no value more than 64 fields and places
 * down.
 */
function oneObject(bytes: Buffer): Record<string, unknown> | null {
  if (bytes.length > MANIFEST_MAX || bytes.indexOf(0x0a) !== bytes.length - 1) return null;
  try {
    const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
    if (text.includes(REPLACEMENT) || !SPELLED.test(text) || halfAlone(text)) return null;
    const named: unknown = JSON.parse(text);
    if (namedTwice(text)) return null;
    // JSON's null is null here too.
    return typeof named === "object" && !Array.isArray(named) && within(named, DOWN_MAX) ? named as Record<string, unknown> | null : null;
  } catch {
    return null; // not UTF-8, or not one JSON document
  }
}

/**
 * The release *manifest*, from *url*, names: once one of *keys* signed its exact bytes, in
 * *signature*, and it is a release of *channel* for *target*, this computer's, as the root helper's
 * own check has one (release_of in release/install.sh). The app takes only what the helper will
 * take: what it took and the helper refused would be offered as an update, and then refused. A
 * computer the app is built for none of takes none. Throws why not.
 */
export function signedRelease(url: string, manifest: Buffer, signature: Buffer, keys: KeyObject[], channel: string, target: Target | null = targetOf()): Release {
  if (!keys.some((key) => verify(null, manifest, key, signature))) throw new Unsigned(`${url} is not signed by Surogate's release key`);
  const release: Partial<Release> = oneObject(manifest) ?? {};
  // Whole numbers below 10^15, as the helper's own check has them.
  const counted = (value: unknown, least: number) => Number.isSafeInteger(value) && (value as number) >= least && (value as number) < 1e15;
  const { version } = release;
  const files = target === null ? null : TARGETS[target];
  if (!(files !== null && typeof version === "string" && VERSION.test(version) && release.channel === channel && release.platform === files.platform && release.arch === files.arch
    && release.url === `${files.releaseDir(version)}/${files.archive(version)}`
    && typeof release.sha256 === "string" && /^[0-9a-f]{64}$/.test(release.sha256) && counted(release.size, 1) && counted(release.stateSchema, 1))) {
    throw new Error(`${url} is not a release of Surogate Desktop for this computer`);
  }
  return release as Release;
}

// The channel the install script recorded beside the base: only its releases are taken. It is the
// one *helper* installs, or nothing is asked of the base: the helper would refuse what came. On
// Windows the MSI's record alone names it, and surogate-admin.exe reads the same.
// Whether the helper names the channel it installs: Linux's root helper does; Windows's MSI writes it in the record alone.
const HELPER_NAMES_CHANNEL = forThisOs({ win32: false }) ?? true;

function channelOf(record: string, helper: string): string {
  const { channel } = JSON.parse(readFileSync(record, "utf8")) as { channel?: unknown };
  if (typeof channel !== "string" || channel === "") throw new Error(`${record} names no update channel`);
  if (!HELPER_NAMES_CHANNEL) return channel;
  const installs = /^[ \t]*CHANNEL=([A-Za-z0-9_-]+)$/m.exec(readFileSync(helper, "utf8"))?.[1];
  if (channel !== installs) throw new Error(`${record} names the channel ${channel}, and ${helper} installs ${installs ?? "no channel it names"}`);
  return channel;
}

// The update's cache is the app's own, and nothing in it is reached through a link: the app is not
// confined, so a link that a program of the user's put there would have it write, or remove, what
// the link leads to. Each folder and each file below the cache home is looked at before it is used.

// Whether *path* is a regular file of one name: what the update reads, or takes, as its own. A
// link in its place is neither, wherever it leads; nor is a file with a second name elsewhere.
function regular(path: string): boolean {
  const found = lstatSync(path, { throwIfNoEntry: false });
  return found !== undefined && found.isFile() && found.nlink === 1;
}

// Whether *path* is a real folder of this user's own, which is then set to 0700. What has its name
// and is not one is removed, a link by itself and never what it leads to; with *make*, the folder
// is then made afresh.
function ownFolder(path: string, make: boolean, said?: (words: string) => void): boolean {
  return OWN_FOLDER(path, make, said);
}

function ownFolderPosix(path: string, make: boolean, said?: (words: string) => void): boolean {
  const found = lstatSync(path, { throwIfNoEntry: false });
  const own = found !== undefined && found.isDirectory() && found.uid === process.getuid?.();
  if (found && !own) {
    // A link is what a person leaves who moved the folder alone to a larger disk: it goes, as
    // itself, and what it led to stays as it was. Said, since the update then lands elsewhere.
    if (found.isSymbolicLink()) said?.(`${path} was a link to ${readlinkSync(path)}, and Surogate keeps its updates in a folder of its own: the link is removed, and what it led to is left as it was`);
    rmSync(path, { recursive: true, force: true });
  }
  if (!own && !make) return false;
  if (!own) mkdirSync(path, { mode: 0o700 });
  // Its mode is set on the folder that is opened, never through a link put there since: that fails the open.
  const folder = openSync(path, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try {
    fchmodSync(folder, 0o700);
  } finally {
    closeSync(folder);
  }
  return true;
}

// The same on Windows: a real folder, no reparse point (Node's lstat takes a junction for a link), owned
// by this user, as ntat reads it through no link; the user's profile already lets no other user in, so
// no access list is set. What has its name and is not one goes as on Linux.
function ownFolderWin32(path: string, make: boolean, said?: (words: string) => void): boolean {
  const found = lstatSync(path, { throwIfNoEntry: false });
  const mine = (): boolean => {
    let fd: number | undefined;
    let held: number | undefined;
    try {
      fd = ntat().openRoot(`\\\\?\\${path}`);
      held = ntat().reopen(fd, ntat().constants.READ_CONTROL | ntat().constants.SYNCHRONIZE, ntat().constants.FILE_SHARE_READ | ntat().constants.FILE_SHARE_WRITE);
      return ntat().owners(held).owner === ntat().userSid();
    } catch {
      return false; // a link, no folder, or one not to be read: not this user's own
    } finally {
      if (held !== undefined) closeSync(held);
      if (fd !== undefined) closeSync(fd);
    }
  };
  const own = found !== undefined && found.isDirectory() && !found.isSymbolicLink() && mine();
  if (found && !own) {
    if (found.isSymbolicLink()) said?.(`${path} was a link to ${readlinkSync(path)}, and Surogate keeps its updates in a folder of its own: the link is removed, and what it led to is left as it was`);
    rmSync(path, { recursive: true, force: true });
  }
  if (!own && !make) return false;
  if (!own) mkdirSync(path);
  return own || mine();
}

// This OS's look at a folder of the cache.
const OWN_FOLDER: (path: string, make: boolean, said?: (words: string) => void) => boolean = forThisOs({ win32: ownFolderWin32 }) ?? ownFolderPosix;

// The updates folder that *cache* names, <cache home>/surogate/updates, by its real path; null when
// it is not there and is not to be made. The cache home may be a link, as one moved to another
// disk is: its real path is taken once, here. Below it, surogate and updates are each the user's
// own real folder (ownFolder). *took* keeps where the cache home led when it was first taken: one
// that leads elsewhere later, as after a link on its way was pointed somewhere else, is not
// followed there, where the app would remove and write what it finds. *said*: the log.
function updatesFolder(cache: string, make: boolean, took: { home: string | null } = { home: null }, said?: (words: string) => void): string | null {
  const home = dirname(dirname(cache));
  if (make) mkdirSync(home, { recursive: true, mode: 0o700 });
  else if (!existsSync(home)) return null;
  const real = realpathSync(home);
  took.home ??= real;
  if (real !== took.home) throw new Error(`${home} led to ${took.home} when Surogate looked for its first update, and leads to ${real} now: no update is kept there until Surogate starts again`);
  const surogate = join(real, basename(dirname(cache)));
  const updates = join(surogate, basename(cache));
  return ownFolder(surogate, make, said) && ownFolder(updates, make) ? updates : null;
}

// *data* as the new file *path*, this user's alone: what has the name goes first, a link by itself,
// and the name is then made, never opened: not through a link put there since, and not into a
// file that has another name.
function keep(path: string, data: Buffer): void {
  rmSync(path, { recursive: true, force: true });
  const file = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try {
    writeFileSync(file, data);
  } finally {
    closeSync(file);
  }
}

// Whether *files* are still as a check left them in the updates folder *cache* names, by a check's
// own rule (updatesFolder): the cache home may be a link, and its real path is taken again here;
// below it, surogate, updates and the version's folder are each the user's own real folder, and
// each file is a regular file of one name. Files that a cache home moved since now leads to
// through a link are not these.
function staged(cache: string, files: Staged, took: { home: string | null }): boolean {
  let updates: string | null;
  try {
    updates = updatesFolder(cache, false, took);
  } catch {
    return false; // the cache home leads elsewhere now
  }
  const folder = dirname(files.tarball);
  return updates !== null && dirname(folder) === updates && ownFolder(folder, false) && [files.manifest, files.signature, files.tarball].every(regular);
}

// Where Windows's helper keeps how an install ended (surogate-helper update, native/windows/helper):
// the app has quit by then, and reads it at its next start.
export const RESULT = "result.json";

// What surogate-admin.exe's exit says, as the helper kept it: 3, the release was not this app's; 4,
// Windows Installer failed and rolled back; 5, the app did not quit within its minute.
const ADMIN_SAYS: Readonly<Record<number, string>> = {
  3: "it is not a release of Surogate that this install takes, so nothing was installed",
  4: "Windows Installer could not install it, and nothing was changed",
  5: "Surogate did not quit in time for it, and nothing was changed",
};

// How the install of *version* that the result file *path* keeps ended: refused, failed with why, or
// null for none to say. Read once: the file goes. One that is no regular file is not read.
function applied(path: string, version: string): { state: "refused" } | { state: "failed"; why: string } | null {
  if (!regular(path)) return null;
  let read: unknown;
  try {
    read = JSON.parse(readFileSync(path, "utf8"));
  } catch {
    read = null;
  } finally {
    rmSync(path, { force: true });
  }
  const { version: of, code } = (typeof read === "object" && read !== null ? read : {}) as { version?: unknown; code?: unknown };
  if (of !== version || !Number.isSafeInteger(code) || code === 0) return null;
  if (code === UAC_DISMISSED) return { state: "refused" };
  return { state: "failed", why: ADMIN_SAYS[code as number] ?? `its install ended ${code as number}` };
}

// How the installed version's mark is read: as the root helper reads one, or on Windows the MSI's record, JSON.
const MARK: (bytes: Buffer) => Record<string, unknown> | null = forThisOs({ win32: (bytes: Buffer) => JSON.parse(bytes.toString("utf8")) as Record<string, unknown> | null }) ?? oneObject;

export interface UpdatesOptions {
  version: string; // the running app's
  record: string; // the install script's record: the base, and the channel
  rootOwned: boolean; // as an installed app's record must be
  helper: string; // the root helper that installs an update: its release keys are trusted, and its channel is the one taken
  installed: string | null; // the installed version's release.json (current/release.json); none in a development build
  askMs?: number; // how long the base may take to answer for the manifest, and for its signature: ASK_MS
  cache: string; // <cache>/surogate/updates
  fetch: Fetch;
  signal: AbortSignal; // the quit: a check or a download under way stops with the app
  apply: (files: Staged) => Promise<Applied>; // the root helper's run: helperRun's (installer-linux.ts)
  log?: (words: string) => void; // told, whole, what pkexec and the helper said of an install that did not end well
  standing?: string; // a file of the app's own, where it keeps how long the newest release has been unsigned; in memory without
  now?: () => number; // the clock: Date.now
  target?: Target | null; // the releases taken, and where on the base they are: this computer's (targetOf)
}

/** The app's updates: one check at a time, each change told. */
export class Updates {
  state: UpdateState = { state: "none" };
  private checking: Promise<void> | null = null;
  // Where the cache home led when it was first taken.
  private readonly took: { home: string | null } = { home: null };
  // The failed checks that stand where no file keeps them (stands).
  private stood: { since: number; count: number } | null = null;

  constructor(private readonly options: UpdatesOptions, private readonly changed: () => void = () => {}) {}

  /** Looks for a newer release and downloads it. Rejects with why it took none; what it had stays. */
  check(): Promise<void> {
    this.checking ??= this.look().finally(() => {
      this.checking = null;
    });
    return this.checking;
  }

  /**
   * Installs the release downloaded: the root helper applies it for every user of this computer.
   * Refused by polkit, or failed, it can be asked again; the files stay. Rejects where the files
   * were no longer the app's own and the release could not be downloaded again, with why.
   */
  async install(): Promise<void> {
    const shown = this.state;
    if (shown.state !== "available" && shown.state !== "refused" && shown.state !== "failed") return;
    const { version, files } = shown;
    // A check left the files hours ago, perhaps, and root's helper is handed their paths: they are
    // looked at once more, and nothing runs between this look and the helper's start. Where one is
    // no longer the app's own file, no helper is run: the offer goes, and the release is looked
    // for again, for another click. From here on the helper alone decides: it reads each file
    // once, as this user and through no link, and checks its own copies.
    if (!staged(this.options.cache, files, this.took)) {
      this.set({ state: "none" });
      return this.check();
    }
    this.set({ state: "installing", version });
    // A helper whose run could not even be begun is a failure with its reason: the line is never
    // left at Installing.
    const { code, said, restarts } = await Promise.resolve().then(() => this.options.apply(files))
      .catch((error: unknown): Applied => ({ code: null, said: error instanceof Error ? error.message : String(error) }));
    // Approved, on Windows: the MSI installs it once the app has quit, and starts the app again.
    if (code === 0 && restarts) return this.set({ state: "restarting", version });
    if (code === 0) {
      // Installed is what the installed version's mark says, where there is one to read. The helper
      // ends 0 for whichever release it was handed, and the files may have been changed after the
      // app's look: to the installed release's own, or to another that a trusted key signed.
      const marked = this.options.installed === null ? version : this.installedVersion();
      if (marked === version) return this.set({ state: "installed", version });
      // Nothing the helper said tells of it: the log has what was offered, and what the mark says.
      this.options.log?.(`Surogate ${version} was not installed, though its helper ended 0: the installed version's mark ${marked ? `names ${marked}` : "cannot be read"}`);
      return this.set({ state: "failed", version, files, why: marked ? `the installed version is ${marked}, not ${version}` : "the installed version's mark cannot be read" });
    }
    // What was said goes to the log whole: the line shows one line of a failure, and of a refusal none.
    this.options.log?.(`Surogate ${version} was not installed${code === null ? "" : ` (exit ${code})`}: ${said}`);
    if (refusal(code, said)) return this.set({ state: "refused", version, files });
    this.set({ state: "failed", version, files, why: reason(code, said) });
  }

  private async look(): Promise<void> {
    // An install under way, or done, is not undone by a later check, and its line stays its own
    // to the helper's end: the helper's last rename shows the installed version's mark changed
    // before the helper has ended, and how it ends is what the line then says.
    if (this.settled()) return;
    // Installed for every user already, as another user's update leaves it: a restart runs it.
    const installed = this.installedVersion();
    if (installed && newer(installed, this.options.version)) return this.set({ state: "installed", version: installed });
    const { record, rootOwned, helper } = this.options;
    let base: string;
    let keys: KeyObject[];
    let channel: string;
    try {
      base = installBase(record, rootOwned);
      keys = releaseKeys(helper, rootOwned);
      channel = channelOf(record, helper);
    } catch (error) {
      // The record or the helper is not one the app can take. Of the helper, an install would say
      // so itself, or pkexec would for it; of the record no one would, and updates would stop
      // with nothing said. The app is not silent: its line says what to do, which is the same for
      // both, since the install script writes each afresh; and its log, where this check's
      // failure goes, says why.
      this.set({ state: "broken" });
      throw error;
    }
    // Put right since, as the install script leaves them: the line goes.
    if (this.state.state === "broken") this.set({ state: "none" });
    const target = this.options.target === undefined ? targetOf() : this.options.target;
    if (target === null) throw new Error("Surogate Desktop has no releases for this computer");
    const at = TARGETS[target];
    const latest = `${base}/desktop/${at.latest}`;
    const manifest = await this.small(latest, MANIFEST_MAX);
    // Its signature is its release's own, at the release's place, which is sent before latest.json
    // names the release and never again: no moment has a manifest beside another's signature. The
    // version that names the place is the manifest's own word, read before any key is asked of
    // it, and a version is all it may be.
    const named = oneObject(manifest)?.version;
    if (typeof named !== "string" || !VERSION.test(named)) throw new Error(`${latest} is not a release of Surogate Desktop for this computer`);
    const signature = await this.small(`${base}/desktop/${at.releaseDir(named)}/manifest.json.sig`, SIGNATURE_MAX);
    let release: Release;
    try {
      release = signedRelease(latest, manifest, signature, keys, channel, target);
    } catch (error) {
      // Signed by no key the helper lists: forged, or signed by a key that a release this computer
      // never took brought. The helper would refuse it too. The app keeps its version and checks
      // on, and its log has this check's failure; its line says so only once that has stood
      // (stands), beside no update that is here and offered. Any other refusal is of a manifest
      // that a trusted key did sign.
      if (!(error instanceof Unsigned)) this.signed();
      else if (this.stands() && !this.settled() && (this.state.state === "none" || this.state.state === "unsigned")) this.set({ state: "unsigned" });
      throw error;
    }
    // One its keys take: what stood is forgotten, and the line goes.
    this.signed();
    // Nor by one that was asking its base when the install began: the root helper reads the cache
    // while it runs, and nothing in it is changed or removed under it.
    if (this.settled()) return;
    if (!newer(release.version, this.options.version)) {
      // Nothing newer, as once updated to it: what was downloaded for an earlier offer goes.
      const earlier = updatesFolder(this.options.cache, false, this.took, this.options.log);
      if (earlier) rmSync(earlier, { recursive: true, force: true });
      return this.set({ state: "none" });
    }
    const cache = updatesFolder(this.options.cache, true, this.took, this.options.log)!;
    const folder = join(cache, release.version);
    ownFolder(folder, true);
    // In it, what is not a regular file goes before anything is read or written, as in the image's
    // folder: nothing there is taken by its name alone.
    for (const name of readdirSync(folder)) if (!regular(join(folder, name))) rmSync(join(folder, name), { recursive: true, force: true });
    const windows = at.platform === "win32";
    const files = { manifest: join(folder, "manifest.json"), signature: join(folder, "manifest.json.sig"), tarball: join(folder, windows ? "release.msi" : "release.tar.gz") };
    // How an earlier install of it ended, as Windows's helper kept it once the app had quit for it: said
    // once, at the first check after, and gone.
    const ended = windows ? applied(join(folder, RESULT), release.version) : null;
    keep(files.manifest, manifest);
    keep(files.signature, signature);
    // One downloaded already is taken by its size and hash: the user's processes can change it.
    // Only a regular file is: a link in its place is not read, wherever it leads.
    const here = regular(files.tarball) && sizeOf(files.tarball) === release.size && (await hashOf(files.tarball)).digest("hex") === release.sha256;
    // Each folder looked at again, wherever time has passed since its look: nothing after it then
    // follows a link put in a folder's place meanwhile.
    const own = (): boolean => [dirname(cache), cache, folder].every((path) => ownFolder(path, false));
    const replaced = new Error(`${folder} was replaced while Surogate ${release.version} was downloaded: nothing in it is taken`);
    if (!here) {
      // The base may take as long as its headers' bound to answer: once it has, and before the
      // partial file is opened, the download stops where a folder is no longer the app's own.
      const stop = new AbortController();
      const asked: Fetch = async (url, init) => {
        const response = await this.options.fetch(url, init);
        if (own()) return response;
        void response.body?.cancel().catch(() => {});
        stop.abort(replaced);
        throw replaced;
      };
      await download({ url: `${base}/desktop/${release.url}`, name: `Surogate ${release.version}`, size: release.size, sha256: release.sha256, magic: windows ? MSI_MAGIC : GZIP_MAGIC },
        `${files.tarball}.partial`, { fetch: asked, signal: AbortSignal.any([this.options.signal, stop.signal]) });
    }
    // Its hash, or its download, took a while: again before anything in a folder is renamed or removed.
    if (!own()) throw replaced;
    // What replaces it takes its name by one rename, once it is all here and is the manifest's: a
    // rename replaces a link, and follows none.
    if (!here) renameSync(`${files.tarball}.partial`, files.tarball);
    // An install that began while this one downloaded keeps its line and the files it was handed:
    // what was downloaded stays in its own folder, for the check after the restart.
    if (this.settled()) return;
    // One release's download is kept: an older offer's goes once this one is here.
    for (const name of readdirSync(cache)) if (name !== release.version) rmSync(join(cache, name), { recursive: true, force: true });
    // A release that was refused, or whose install failed, is not offered afresh for being found
    // again: its line stays until its user tries again, or a newer release is found.
    const shown = this.state;
    if ((shown.state === "refused" || shown.state === "failed") && shown.version === release.version) return;
    if (ended?.state === "refused") return this.set({ state: "refused", version: release.version, files });
    if (ended?.state === "failed") return this.set({ state: "failed", version: release.version, files, why: ended.why });
    this.set({ state: "available", version: release.version, files });
  }

  // One more check has found the base's newest release signed by no trusted key. Whether that has
  // now stood long enough to be said: UNSIGNED_CHECKS such checks in a row, the first of them
  // UNSIGNED_MS ago or more. In a row: with no answer between them that a trusted key signed; a
  // base that did not answer is neither. What stands is kept in a file, so that an app that is
  // started once a day counts on. One that cannot be what stood begins the count again: no such
  // record, or a first failure in the future, as after the clock was set back.
  private stands(): boolean {
    const now = (this.options.now ?? Date.now)();
    const { standing } = this.options;
    let stood = this.stood;
    if (standing !== undefined) {
      stood = null;
      try {
        const read: unknown = regular(standing) ? JSON.parse(readFileSync(standing, "utf8")) : null;
        const { since, count } = (typeof read === "object" && read !== null && !Array.isArray(read) ? read : {}) as { since?: unknown; count?: unknown };
        if (Number.isSafeInteger(since) && Number.isSafeInteger(count) && (count as number) >= 1) stood = { since: since as number, count: count as number };
      } catch {
        // not JSON: no record
      }
    }
    if (stood === null || stood.since > now) stood = { since: now, count: 0 };
    stood = { since: stood.since, count: stood.count + 1 };
    this.stood = stood;
    if (standing !== undefined) keep(standing, Buffer.from(JSON.stringify(stood)));
    return stood.count >= UNSIGNED_CHECKS && now - stood.since >= UNSIGNED_MS;
  }

  // An answer that a trusted key signed: nothing stands, and the line for it goes.
  private signed(): void {
    this.stood = null;
    if (this.options.standing !== undefined) rmSync(this.options.standing, { recursive: true, force: true });
    if (this.state.state === "unsigned") this.set({ state: "none" });
  }

  // Whether an install runs, or is done: from then on a check changes neither the line nor the cache.
  private settled(): boolean {
    return this.state.state === "installing" || this.state.state === "installed" || this.state.state === "restarting";
  }

  // The version installed for every user now, as its root helper recorded it; null when none can be
  // read. Its mark is read as the helper reads one: root's own in an installed app, and one object.
  private installedVersion(): string | null {
    const { installed, rootOwned } = this.options;
    if (!installed || !existsSync(installed)) return null;
    try {
      if (rootOwned) installersOwn(installed);
      const version = MARK(readFileSync(installed))?.version;
      return typeof version === "string" && VERSION.test(version) ? version : null;
    } catch {
      return null;
    }
  }

  // A small file of the base's, whole, within *max* bytes: a page in its place is refused unread.
  private async small(url: string, max: number): Promise<Buffer> {
    const { host } = new URL(url);
    const ms = this.options.askMs ?? ASK_MS;
    const late = AbortSignal.timeout(ms);
    // What the base did not do, in the app's own words and with what was asked of it: the system's
    // words for a timeout or a lost connection name neither. The app's quit is said as it is.
    const asked = async <Answer>(answer: Promise<Answer>): Promise<Answer> => {
      try {
        return await answer;
      } catch (error) {
        if (this.options.signal.aborted) throw error;
        throw new Error(late.aborted ? `${host} did not answer for ${url} in ${ms / 1000} s` : `could not reach ${host}: ${error instanceof Error ? error.message : String(error)}`);
      }
    };
    const response = await asked(this.options.fetch(url, { headers: {}, signal: AbortSignal.any([this.options.signal, late]) }));
    if (response.status !== 200) {
      void response.body?.cancel().catch(() => {});
      throw new Error(`${host} answered ${response.status} for ${url}`);
    }
    const chunks: Buffer[] = [];
    let size = 0;
    const reader = response.body?.getReader();
    for (let read = reader && await asked(reader.read()); read && !read.done; read = await asked(reader!.read())) {
      size += read.value.length;
      if (size > max) {
        void reader!.cancel().catch(() => {});
        throw new Error(`${url} is not Surogate's: it is larger than ${max} bytes`);
      }
      chunks.push(Buffer.from(read.value));
    }
    return Buffer.concat(chunks);
  }

  private set(state: UpdateState): void {
    this.state = state;
    this.changed();
  }
}
