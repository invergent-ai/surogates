// Updates inside the app (spec, Section 9, "In-app updates"): at its start and every 6 hours,
// the signed latest.json of the base the install script recorded, checked here against the
// release keys this computer trusts, the ones the helper pkexec runs lists; a newer release for
// this computer and the recorded channel downloaded as the user into their cache, resumed; then
// Update available. Installing it runs the root helper on the downloaded files, under polkit in
// an installed app; the helper checks all of them again before it applies anything.
// Electron-free.

import { spawn } from "node:child_process";
import { createPublicKey, type KeyObject, verify } from "node:crypto";
import {
  closeSync, constants, existsSync, fchmodSync, lstatSync, mkdirSync, openSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, writeFileSync,
} from "node:fs";
import { basename, dirname, join } from "node:path";
import { StringDecoder } from "node:string_decoder";

import { download, type Fetch, hashOf, sizeOf } from "../download.js";
import { installBase, rootsOwn } from "../vm/image.js";

export const CHECK_MS = 6 * 60 * 60 * 1000;
// How a tarball begins: a gzip member's magic number.
const GZIP_MAGIC = Buffer.from([0x1f, 0x8b]);
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
// What the install script leaves an installed app, each root's alone: its record of where it was
// installed from; the helper pkexec runs, whose list of release keys is the one this computer
// trusts; and the mark of the version installed for every user now.
export const ROOT_RECORD = "/etc/surogate/install.json";
export const ROOT_HELPER = "/opt/surogate/bin/surogate-apply-update";
const INSTALLED = "/opt/surogate/current/release.json";

// A release, as its signed manifest names it (release/publish.sh sign).
export interface Release {
  version: string;
  channel: string;
  platform: "linux";
  arch: "x64";
  url: string; // relative to <base>/desktop/
  sha256: string;
  size: number;
  stateSchema: number;
}

// A downloaded release's files, in the user's cache, as the root helper is handed them.
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
  // The helper pkexec would run is not one the app can take: nothing is installed until the
  // install script has put it right.
  | { state: "broken" };

// What the root helper's run came to: its exit code, null when it did not run or a signal ended
// it; and the last of what it said, with how it ended where no exit code says.
export interface Applied {
  code: number | null;
  said: string;
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
const refusal = (code: number | null, said: string) => code === DISMISSED || (code === NOT_AUTHORIZED && said.split("\n").some((line) => line.startsWith(AS_ANOTHER_USER)));
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

// *why* as the line shows it: WHY_MAX characters at most, counted as letters, its end marked where it was cut.
function shown(why: string): string {
  const letters = [...why];
  return letters.length <= WHY_MAX ? why : `${letters.slice(0, WHY_MAX - 1).join("")}\u2026`;
}
// How an installed app runs the root helper: pkexec, by its whole path, on the helper at the path
// with no link in it that the install script's polkit action names. Never with pkexec's own agent:
// started from a terminal in a session with no polkit agent, pkexec would ask for a password on
// that terminal and wait there, with the app's line at "Installing". Without it, pkexec ends at
// once with 127, and the line says an administrator is needed.
export const AS_ROOT = ["/usr/bin/pkexec", "--disable-internal-agent", ROOT_HELPER];

// The update's line in the sidebar: what the user is told, and the button they act on it with.
export interface UpdateLine {
  text: string;
  button: string | null;
}

/** The sidebar's line for *state*, or null for none. */
export function updateLine(state: UpdateState | null): UpdateLine | null {
  switch (state?.state) {
    case "available":
      return { text: `Update available: Surogate ${state.version}`, button: "Restart to update" };
    case "installing":
      return { text: `Installing Surogate ${state.version}…`, button: null };
    case "refused":
      return { text: "An administrator needs to install this update.", button: "Try again" };
    case "failed":
      return { text: `Surogate could not install its update: ${state.why}`, button: "Try again" };
    case "installed":
      return { text: `Surogate ${state.version} is installed.`, button: "Restart" };
    case "broken":
      return { text: "Surogate cannot update itself. Run the install script again.", button: null };
    default:
      return null;
  }
}

/**
 * What runs the root helper on a downloaded release: *command*, then --apply and the files'
 * paths, with no more of the app's environment than a PATH. In an installed app, pkexec and
 * the helper's path; polkit asks an administrator, and the helper checks the files again as root.
 */
export function helperRun(command: string[]): (files: Staged) => Promise<Applied> {
  return (files) => new Promise((resolve) => {
    const [program, ...args] = command;
    let child: ReturnType<typeof spawn>;
    try {
      child = spawn(program!, [...args, "--apply", files.manifest, files.signature, files.tarball], {
        stdio: ["ignore", "ignore", "pipe"], env: { PATH: "/usr/bin:/bin" },
      });
    } catch (error) {
      return resolve({ code: null, said: error instanceof Error ? error.message : String(error) });
    }
    // Before anything else is asked of it: one that could not be started, as for want of file
    // descriptors, tells its error later, and has no output to read.
    child.once("error", (error) => resolve({ code: null, said: error.message }));
    // What it says is one stream, read as it comes: a letter may come in two reads.
    const decoder = new StringDecoder("utf8");
    let said = "";
    child.stderr?.on("data", (chunk: Buffer) => {
      said = (said + decoder.write(chunk)).slice(-4000);
    });
    // One a signal ended has no exit code: how it ended is the last of what is said of it.
    child.once("close", (code, signal) => {
      said = (said + decoder.end()).slice(-4000);
      resolve({ code, said: (code === null ? `${said.trim()}\nits helper was stopped by ${signal}` : said).trim() });
    });
  });
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
 * none from anywhere else in the script. An entry that is no such key is skipped, as the helper
 * skips it. With *rootOwned*, a helper that is not root's own word, or that pkexec could not run
 * (rootsOwn), is not read.
 */
export function releaseKeys(helper: string, rootOwned = false): KeyObject[] {
  if (rootOwned) rootsOwn(helper, true);
  const list = /^[ \t]*RELEASE_KEYS=\(\n([^)]*)\)/m.exec(readFileSync(helper, "utf8"))?.[1] ?? "";
  const keys = (list.match(/-----BEGIN PUBLIC KEY-----\n[A-Za-z0-9+/=\n]+-----END PUBLIC KEY-----/g) ?? []).flatMap((pem) => {
    try {
      const key = createPublicKey(pem);
      return key.asymmetricKeyType === "ed25519" ? [key] : [];
    } catch {
      return []; // not a key OpenSSL loads
    }
  });
  if (keys.length === 0) throw new Error(`${helper} trusts no release key`);
  return keys;
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
 * and no value more than 64 fields and places down.
 */
function oneObject(bytes: Buffer): Record<string, unknown> | null {
  if (bytes.length > MANIFEST_MAX || bytes.indexOf(0x0a) !== bytes.length - 1) return null;
  try {
    const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
    if (text.includes(REPLACEMENT) || !SPELLED.test(text) || halfAlone(text)) return null;
    const named: unknown = JSON.parse(text);
    // JSON's null is null here too.
    return typeof named === "object" && !Array.isArray(named) && within(named, DOWN_MAX) ? named as Record<string, unknown> | null : null;
  } catch {
    return null; // not UTF-8, or not one JSON document
  }
}

/**
 * The release *manifest*, from *url*, names: once one of *keys* signed its exact bytes, in
 * *signature*, and it is a release of *channel* for this computer, as the root helper's own check
 * has one (release_of in release/install.sh). The app takes only what the helper will take: what
 * it took and the helper refused would be offered as an update, and then refused. Throws why not.
 */
export function signedRelease(url: string, manifest: Buffer, signature: Buffer, keys: KeyObject[], channel: string): Release {
  if (!keys.some((key) => verify(null, manifest, key, signature))) throw new Error(`${url} is not signed by Surogate's release key`);
  const release: Partial<Release> = oneObject(manifest) ?? {};
  // Whole numbers below 10^15, as the helper's own check has them.
  const counted = (value: unknown, least: number) => Number.isSafeInteger(value) && (value as number) >= least && (value as number) < 1e15;
  const { version } = release;
  if (!(typeof version === "string" && VERSION.test(version) && release.channel === channel && release.platform === "linux" && release.arch === "x64"
    && release.url === `releases/${version}/surogate-desktop-${version}-linux-x64.tar.gz`
    && typeof release.sha256 === "string" && /^[0-9a-f]{64}$/.test(release.sha256) && counted(release.size, 1) && counted(release.stateSchema, 1))) {
    throw new Error(`${url} is not a release of Surogate Desktop for this computer`);
  }
  return release as Release;
}

// The channel the install script recorded beside the base: only its releases are taken. It is the
// one *helper* installs, or nothing is asked of the base: the helper would refuse what came.
function channelOf(record: string, helper: string): string {
  const { channel } = JSON.parse(readFileSync(record, "utf8")) as { channel?: unknown };
  if (typeof channel !== "string" || channel === "") throw new Error(`${record} names no update channel`);
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
function ownFolder(path: string, make: boolean): boolean {
  const found = lstatSync(path, { throwIfNoEntry: false });
  const own = found !== undefined && found.isDirectory() && found.uid === process.getuid?.();
  if (found && !own) rmSync(path, { recursive: true, force: true });
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

// The updates folder that *cache* names, <cache home>/surogate/updates, by its real path; null when
// it is not there and is not to be made. The cache home may be a link, as one moved to another
// disk is: its real path is taken once, here. Below it, surogate and updates are each the user's
// own real folder (ownFolder).
function updatesFolder(cache: string, make: boolean): string | null {
  const home = dirname(dirname(cache));
  if (make) mkdirSync(home, { recursive: true, mode: 0o700 });
  else if (!existsSync(home)) return null;
  const surogate = join(realpathSync(home), basename(dirname(cache)));
  const updates = join(surogate, basename(cache));
  return ownFolder(surogate, make) && ownFolder(updates, make) ? updates : null;
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
function staged(cache: string, files: Staged): boolean {
  const updates = updatesFolder(cache, false);
  const folder = dirname(files.tarball);
  return updates !== null && dirname(folder) === updates && ownFolder(folder, false) && [files.manifest, files.signature, files.tarball].every(regular);
}

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
  apply: (files: Staged) => Promise<Applied>; // the root helper's run: helperRun's
  log?: (words: string) => void; // told, whole, what pkexec and the helper said of an install that did not end well
}

/**
 * An installed app's updates: what the install script left it, where the user's downloads go, and
 * the root helper run under polkit.
 */
export function installedUpdates(version: string, cache: string, fetch: Fetch, signal: AbortSignal): UpdatesOptions {
  return { version, record: ROOT_RECORD, rootOwned: true, helper: ROOT_HELPER, installed: INSTALLED, cache, fetch, signal, apply: helperRun(AS_ROOT) };
}

/** The app's updates: one check at a time, each change told. */
export class Updates {
  state: UpdateState = { state: "none" };
  private checking: Promise<void> | null = null;

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
    if (!staged(this.options.cache, files)) {
      this.set({ state: "none" });
      return this.check();
    }
    this.set({ state: "installing", version });
    // A helper whose run could not even be begun is a failure with its reason: the line is never
    // left at Installing.
    const { code, said } = await Promise.resolve().then(() => this.options.apply(files))
      .catch((error: unknown) => ({ code: null, said: error instanceof Error ? error.message : String(error) }));
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
    const base = installBase(record, rootOwned);
    let keys: KeyObject[];
    try {
      keys = releaseKeys(helper, rootOwned);
    } catch (error) {
      // The helper is not one the app can take, and at an install it would say so itself, or
      // pkexec would for it. The app is not silent meanwhile: its line says what to do, and its
      // log, where this check's failure goes, says why.
      this.set({ state: "broken" });
      throw error;
    }
    // Put right since, as the install script leaves it: the line goes.
    if (this.state.state === "broken") this.set({ state: "none" });
    const channel = channelOf(record, helper);
    const latest = `${base}/desktop/latest.json`;
    const manifest = await this.small(latest, MANIFEST_MAX);
    const signature = await this.small(`${latest}.sig`, SIGNATURE_MAX);
    const release = signedRelease(latest, manifest, signature, keys, channel);
    // Nor by one that was asking its base when the install began: the root helper reads the cache
    // while it runs, and nothing in it is changed or removed under it.
    if (this.settled()) return;
    if (!newer(release.version, this.options.version)) {
      // Nothing newer, as once updated to it: what was downloaded for an earlier offer goes.
      const earlier = updatesFolder(this.options.cache, false);
      if (earlier) rmSync(earlier, { recursive: true, force: true });
      return this.set({ state: "none" });
    }
    const cache = updatesFolder(this.options.cache, true)!;
    const folder = join(cache, release.version);
    ownFolder(folder, true);
    // In it, what is not a regular file goes before anything is read or written, as in the image's
    // folder: nothing there is taken by its name alone.
    for (const name of readdirSync(folder)) if (!regular(join(folder, name))) rmSync(join(folder, name), { recursive: true, force: true });
    const files = { manifest: join(folder, "manifest.json"), signature: join(folder, "manifest.json.sig"), tarball: join(folder, "release.tar.gz") };
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
      await download({ url: `${base}/desktop/${release.url}`, name: `Surogate ${release.version}`, size: release.size, sha256: release.sha256, magic: GZIP_MAGIC },
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
    this.set({ state: "available", version: release.version, files });
  }

  // Whether an install runs, or is done: from then on a check changes neither the line nor the cache.
  private settled(): boolean {
    return this.state.state === "installing" || this.state.state === "installed";
  }

  // The version installed for every user now, as its root helper recorded it; null when none can be
  // read. Its mark is read as the helper reads one: root's own in an installed app, and one object.
  private installedVersion(): string | null {
    const { installed, rootOwned } = this.options;
    if (!installed || !existsSync(installed)) return null;
    try {
      if (rootOwned) rootsOwn(installed);
      const version = oneObject(readFileSync(installed))?.version;
      return typeof version === "string" && VERSION.test(version) ? version : null;
    } catch {
      return null;
    }
  }

  // A small file of the base's, whole, within *max* bytes: a page in its place is refused unread.
  private async small(url: string, max: number): Promise<Buffer> {
    const response = await this.options.fetch(url, { headers: {}, signal: AbortSignal.any([this.options.signal, AbortSignal.timeout(this.options.askMs ?? ASK_MS)]) });
    if (response.status !== 200) {
      void response.body?.cancel().catch(() => {});
      throw new Error(`${new URL(url).host} answered ${response.status} for ${url}`);
    }
    const chunks: Buffer[] = [];
    let size = 0;
    const reader = response.body?.getReader();
    for (let read = await reader?.read(); read && !read.done; read = await reader!.read()) {
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
