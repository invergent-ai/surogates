// Updates inside the app (spec, Section 9, "In-app updates"): at its start and every 6 hours,
// the signed latest.json of the base the install script recorded, checked here against the
// release keys this computer trusts, the ones the helper pkexec runs lists; a newer release for
// this computer and the recorded channel downloaded as the user into their cache, resumed; then
// Update available. The root helper checks all of it again before it applies anything.
// Electron-free.

import { createPublicKey, type KeyObject, verify } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { download, type Fetch, hashOf, sizeOf } from "../download.js";
import { installBase } from "../vm/image.js";

export const CHECK_MS = 6 * 60 * 60 * 1000;
// How a tarball begins: a gzip member's magic number.
const GZIP_MAGIC = Buffer.from([0x1f, 0x8b]);
// A manifest is one short line, and its signature Ed25519's 64 bytes: anything longer is not one.
const MANIFEST_MAX = 4096;
const SIGNATURE_MAX = 64;
// How long the base may take to answer for either.
const ASK_MS = 30_000;
const VERSION = /^(\d+)\.(\d+)\.(\d+)$/;
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
  // Installed for every user of this computer: the app runs it once it restarts.
  | { state: "installed"; version: string };

// The update's line in the sidebar: what the user is told, and the button they act on it with.
export interface UpdateLine {
  text: string;
  button: string | null;
}

/** The sidebar's line for *state*, or null for none. */
export function updateLine(state: UpdateState | null): UpdateLine | null {
  if (state?.state === "available") return { text: `Update available: Surogate ${state.version}`, button: null };
  if (state?.state === "installed") return { text: `Surogate ${state.version} is installed`, button: null };
  return null;
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
 * skips it. With *rootOwned*, a helper that another than root may write is not read.
 */
export function releaseKeys(helper: string, rootOwned = false): KeyObject[] {
  const { uid, mode } = statSync(helper);
  if (rootOwned && (uid !== 0 || (mode & 0o022) !== 0)) throw new Error(`${helper} is not the install script's: only root may write it`);
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

/**
 * The release *manifest*, from *url*, names: once one of *keys* signed its exact bytes, in
 * *signature*, and its fields are a release of *channel* for this computer, as the root helper's
 * own check has them. Throws why not.
 */
export function signedRelease(url: string, manifest: Buffer, signature: Buffer, keys: KeyObject[], channel: string): Release {
  if (!keys.some((key) => verify(null, manifest, key, signature))) throw new Error(`${url} is not signed by Surogate's release key`);
  let release: Partial<Release> = {};
  try {
    release = JSON.parse(manifest.toString("utf8")) as Partial<Release>;
  } catch {
    // Not JSON: no release.
  }
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

export interface UpdatesOptions {
  version: string; // the running app's
  record: string; // the install script's record: the base, and the channel
  rootOwned: boolean; // as an installed app's record must be
  helper: string; // the root helper that installs an update: its release keys are trusted, and its channel is the one taken
  installed: string | null; // the installed version's release.json (current/release.json); none in a development build
  cache: string; // <cache>/surogate/updates
  fetch: Fetch;
  signal: AbortSignal; // the quit: a check or a download under way stops with the app
}

/** An installed app's updates: what the install script left it, and where the user's downloads go. */
export function installedUpdates(version: string, cache: string, fetch: Fetch, signal: AbortSignal): UpdatesOptions {
  return { version, record: ROOT_RECORD, rootOwned: true, helper: ROOT_HELPER, installed: INSTALLED, cache, fetch, signal };
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

  private async look(): Promise<void> {
    // Installed for every user already, as another user's update leaves it: a restart runs it.
    const installed = this.installedVersion();
    if (installed && newer(installed, this.options.version)) return this.set({ state: "installed", version: installed });
    const { record, rootOwned, helper, cache } = this.options;
    const base = installBase(record, rootOwned);
    const keys = releaseKeys(helper, rootOwned);
    const channel = channelOf(record, helper);
    const latest = `${base}/desktop/latest.json`;
    const manifest = await this.small(latest, MANIFEST_MAX);
    const signature = await this.small(`${latest}.sig`, SIGNATURE_MAX);
    const release = signedRelease(latest, manifest, signature, keys, channel);
    if (!newer(release.version, this.options.version)) {
      // Nothing newer, as once updated to it: what was downloaded for an earlier offer goes.
      rmSync(cache, { recursive: true, force: true });
      return this.set({ state: "none" });
    }
    const folder = join(cache, release.version);
    mkdirSync(folder, { recursive: true, mode: 0o700 });
    const files = { manifest: join(folder, "manifest.json"), signature: join(folder, "manifest.json.sig"), tarball: join(folder, "release.tar.gz") };
    writeFileSync(files.manifest, manifest);
    writeFileSync(files.signature, signature);
    // One downloaded already is taken by its size and hash: the user's processes can change it.
    // What replaces it takes its name by one rename, once it is all here and is the manifest's.
    if (sizeOf(files.tarball) !== release.size || (await hashOf(files.tarball)).digest("hex") !== release.sha256) {
      await download({ url: `${base}/desktop/${release.url}`, name: `Surogate ${release.version}`, size: release.size, sha256: release.sha256, magic: GZIP_MAGIC },
        `${files.tarball}.partial`, { fetch: this.options.fetch, signal: this.options.signal });
      renameSync(`${files.tarball}.partial`, files.tarball);
    }
    // One release's download is kept: an older offer's goes once this one is here.
    for (const name of readdirSync(cache)) if (name !== release.version) rmSync(join(cache, name), { recursive: true, force: true });
    this.set({ state: "available", version: release.version, files });
  }

  // The version installed for every user now, as its root helper recorded it; null when none can be read.
  private installedVersion(): string | null {
    if (!this.options.installed || !existsSync(this.options.installed)) return null;
    try {
      const { version } = JSON.parse(readFileSync(this.options.installed, "utf8")) as { version?: unknown };
      return typeof version === "string" && VERSION.test(version) ? version : null;
    } catch {
      return null;
    }
  }

  // A small file of the base's, whole, within *max* bytes: a page in its place is refused unread.
  private async small(url: string, max: number): Promise<Buffer> {
    const response = await this.options.fetch(url, { headers: {}, signal: AbortSignal.any([this.options.signal, AbortSignal.timeout(ASK_MS)]) });
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
