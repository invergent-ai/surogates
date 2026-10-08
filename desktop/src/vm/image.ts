// The guest image's delivery (spec, Section 11, "The image"): the files the app's
// manifest names, downloaded from where the app was installed from into this user's
// data, each checked by its hash as downloaded and again unpacked. A download that
// stops, or that nothing comes for, is resumed from its .partial file. Each file is on
// disk before its rename, and the image's folder is renamed into place only once every
// file in it is checked; its last step is its completion mark, so a folder with that
// mark and its files' sizes is a whole image, and one without is downloaded again.

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
  closeSync, createReadStream, existsSync, fsyncSync, mkdirSync, openSync, readdirSync, readFileSync, renameSync, rmSync, statSync, truncateSync, writeFileSync,
} from "node:fs";
import { open, statfs } from "node:fs/promises";
import { dirname, join } from "node:path";

// Each file the image has, as the manifest names it: unpacked, and as downloaded.
export interface ImageFile {
  name: string;
  size: number;
  sha256: string;
  download: string;
  downloadSize: number;
  downloadSha256: string;
}

// resources/vm/manifest.json, which images/guest/build.sh writes and the app's tarball carries.
export interface ImageManifest {
  key: string;
  files: ImageFile[];
}

const HASH = /^[0-9a-f]{64}$/;
// The files the VM boots from: a manifest names exactly these.
const NAMES = ["rootfs.img", "vmlinuz"];
const size = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value > 0;

/** The manifest at *path*; throws when it is not one. Its names are never paths. */
export function readManifest(path: string): ImageManifest {
  let manifest: Partial<ImageManifest> = {};
  try {
    manifest = JSON.parse(readFileSync(path, "utf8")) as Partial<ImageManifest>;
  } catch {
    // Not there, or not JSON: as one that names nothing.
  }
  const files = Array.isArray(manifest.files) ? manifest.files : [];
  const valid = typeof manifest.key === "string" && HASH.test(manifest.key)
    && files.map((file) => file?.name).sort().join() === NAMES.join()
    && files.every((file) => size(file.size) && size(file.downloadSize) && HASH.test(String(file.sha256)) && HASH.test(String(file.downloadSha256))
      && file.download === `${file.name}.zst`);
  if (!valid) throw new Error(`${path} is not the guest image's manifest`);
  return manifest as ImageManifest;
}

/**
 * The base URL in the install record at *path*, which the install script writes:
 * {"base": "https://surogate.ai"}. Throws when there is none to read.
 */
export function installBase(path: string): string {
  let text: string;
  try {
    text = readFileSync(path, "utf8");
  } catch (error) {
    const { code } = error as NodeJS.ErrnoException;
    if (code !== "ENOENT") throw new Error(`${path} could not be read: ${code ?? String(error)}`);
    throw new Error("Surogate was not installed by its install script, so it does not know where to download its sandbox from");
  }
  let base: unknown;
  try {
    base = (JSON.parse(text) as { base?: unknown }).base;
  } catch {
    // Not JSON: it names no base.
  }
  const url = typeof base === "string" && URL.canParse(base) ? new URL(base) : null;
  if (!url || !["https:", "http:"].includes(url.protocol)) throw new Error(`${path} names no web address to download the sandbox from`);
  return url.href.replace(/\/+$/, "");
}

export type Fetch = (url: string, init: { headers: Record<string, string>; signal?: AbortSignal }) => Promise<Response>;

export interface DeliverOptions {
  manifest: ImageManifest;
  base: string; // where the app was installed from: the files are at <base>/desktop/vm/<key>/
  images: string; // <data>/vm/images: one folder per image, by its key
  fetch?: Fetch;
  // Told how many of the downloads' bytes are here, of how many.
  progress?: (done: number, total: number) => void;
  signal?: AbortSignal;
  stallMs?: number; // how long nothing may come, headers or bytes, before the download stops: STALL_MS
}

// Chromium's network bounds no body that stops coming, as from a peer gone over a sleep or a
// proxy that holds a large download: a download that nothing comes for in this long stops.
const STALL_MS = 30_000;
// An image's folder's last step, written once all of it is on disk.
const COMPLETE = "complete";
const gigabytes = (bytes: number) => `${(bytes / 1e9).toFixed(1)} GB`;
const sizeOf = (path: string) => (existsSync(path) ? statSync(path).size : 0);

// *path*'s data, or a folder's entries, on disk.
function sync(path: string): void {
  const fd = openSync(path, "r");
  try {
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

// *from* renamed to *to* so that a power cut finds the new name only with all of its bytes,
// which ext4 commits apart from a rename: its data and the folder it was made in first.
function settle(from: string, to: string): void {
  sync(from);
  sync(dirname(from));
  renameSync(from, to);
  sync(dirname(to));
}

// Whether *folder* is the whole image: its last step there, and each file at its manifest's size.
function whole(folder: string, manifest: ImageManifest): boolean {
  return existsSync(join(folder, COMPLETE)) && manifest.files.every((file) => sizeOf(join(folder, file.name)) === file.size);
}

// Whether each of *folder*'s files has its manifest's size and sha256.
async function intact(folder: string, manifest: ImageManifest): Promise<boolean> {
  for (const file of manifest.files) {
    const path = join(folder, file.name);
    if (sizeOf(path) !== file.size || (await hashOf(path)).digest("hex") !== file.sha256) return false;
  }
  return true;
}

// The sha256 of *path*'s first *bytes*, or all of it.
async function hashOf(path: string, bytes?: number): Promise<ReturnType<typeof createHash>> {
  const hash = createHash("sha256");
  if (bytes === 0) return hash;
  for await (const chunk of createReadStream(path, bytes === undefined ? {} : { end: bytes - 1 })) hash.update(chunk as Buffer);
  return hash;
}

/**
 * The folder holding the image's unpacked files, <images>/<key>, once each is there and
 * checked. Downloads what is missing, after checking there is room for it. Rejects
 * with why it could not, in words for the user; what it downloaded stays for the next try.
 */
export async function deliver(options: DeliverOptions): Promise<string> {
  const { manifest, images, signal } = options;
  const folder = join(images, manifest.key);
  if (whole(folder, manifest)) return folder;
  // Left short of a file or of its last step, as by a power cut: not an image.
  rmSync(folder, { recursive: true, force: true });
  const work = `${folder}.partial`;
  mkdirSync(work, { recursive: true, mode: 0o700 });
  const total = manifest.files.reduce((sum, file) => sum + file.downloadSize, 0);
  // What is here already: a file unpacked, a download whole or in part.
  const here = (file: ImageFile) => (existsSync(join(work, file.name)) ? file.downloadSize
    : sizeOf(join(work, file.download)) || Math.min(sizeOf(join(work, `${file.download}.partial`)), file.downloadSize));
  const needed = manifest.files.filter((file) => !existsSync(join(work, file.name)))
    .reduce((sum, file) => sum + file.size + file.downloadSize - here(file), 0);
  const { bavail, bsize } = await statfs(work);
  if (bavail * bsize < needed) {
    throw new Error(`there is not enough free disk space: it needs ${gigabytes(needed)}, and ${gigabytes(bavail * bsize)} is free`);
  }
  for (const file of manifest.files) {
    const unpacked = join(work, file.name);
    if (existsSync(unpacked)) continue;
    const downloaded = join(work, file.download);
    if (!existsSync(downloaded)) {
      const others = manifest.files.filter((other) => other !== file).reduce((sum, other) => sum + here(other), 0);
      await download(options, file, `${downloaded}.partial`, (have) => options.progress?.(others + have, total));
      settle(`${downloaded}.partial`, downloaded);
    }
    signal?.throwIfAborted();
    await unpack(downloaded, unpacked, file);
    rmSync(downloaded);
  }
  settle(work, folder);
  writeFileSync(join(folder, COMPLETE), `${manifest.key}\n`);
  sync(join(folder, COMPLETE));
  sync(folder);
  return folder;
}

// *file*'s download into *partial*, resumed from what it holds, and checked by its hash.
// *got* is told how many of its bytes are here.
async function download(options: DeliverOptions, file: ImageFile, partial: string, got: (have: number) => void): Promise<void> {
  let have = sizeOf(partial);
  if (have > file.downloadSize) {
    truncateSync(partial, 0);
    have = 0;
  }
  let hash = await hashOf(partial, have);
  got(have);
  if (have < file.downloadSize) {
    const url = `${options.base}/desktop/vm/${options.manifest.key}/${file.download}`;
    const said = (error: unknown) => (error instanceof Error ? error.message : String(error));
    // Nothing for stallMs, its headers or its next bytes, stops it, whatever the fetch bounds.
    const stallMs = options.stallMs ?? STALL_MS;
    const quiet = new AbortController();
    let timer: NodeJS.Timeout | undefined;
    const heard = () => {
      clearTimeout(timer);
      timer = setTimeout(() => quiet.abort(new Error(`nothing came for ${stallMs / 1000} s`)), stallMs);
    };
    const stalled = new Promise<never>((_resolve, reject) => quiet.signal.addEventListener("abort", () => reject(quiet.signal.reason), { once: true }));
    stalled.catch(() => {});
    const signal = options.signal ? AbortSignal.any([options.signal, quiet.signal]) : quiet.signal;
    heard();
    try {
      let response: Response;
      try {
        response = await Promise.race([(options.fetch ?? fetch)(url, { headers: have > 0 ? { range: `bytes=${have}-` } : {}, signal }), stalled]);
      } catch (error) {
        options.signal?.throwIfAborted();
        if (quiet.signal.aborted) throw new Error(`the download of ${file.download} stopped: ${said(quiet.signal.reason)}`);
        throw new Error(`could not reach ${new URL(url).host}: ${said(error)}`);
      }
      if (response.status === 200 && have > 0) {
        // Not the rest of the file: the whole of it, from a server that ignores a Range, starts it
        // again. Anything else, as a captive portal's page, is not the file, and leaves what is here.
        if (Number(response.headers.get("content-length")) !== file.downloadSize) {
          void response.body?.cancel().catch(() => {});
          throw new Error(`${file.download} was not the file the app expects`);
        }
        truncateSync(partial, 0);
        have = 0;
        hash = createHash("sha256");
        got(have);
      } else if (response.status === 206 && !response.headers.get("content-range")?.startsWith(`bytes ${have}-`)) {
        // A range, but not from where it stopped: asked again, it would be the same, so the next try starts it afresh.
        void response.body?.cancel().catch(() => {});
        rmSync(partial, { force: true });
        throw new Error(`the download of ${file.download} did not resume where it stopped`);
      } else if (response.status !== 200 && response.status !== 206) {
        throw new Error(`${new URL(url).host} answered ${response.status} for ${file.download}`);
      }
      const out = await open(partial, have > 0 ? "a" : "w", 0o600);
      const reader = response.body?.getReader();
      const next = () => reader && Promise.race([reader.read(), stalled]);
      try {
        for (let read = await next(); read && !read.done; read = await next()) {
          heard();
          // Past the size the manifest names: it is not the file.
          if (have + read.value.length > file.downloadSize) break;
          hash.update(read.value);
          // A write may take less than it is given: what is hashed is what is on disk.
          for (let at = 0; at < read.value.length;) at += (await out.write(read.value, at)).bytesWritten;
          have += read.value.length;
          got(have);
        }
      } catch (error) {
        options.signal?.throwIfAborted();
        throw new Error(`the download of ${file.download} stopped: ${said(error)}`);
      } finally {
        // Not waited for: a body that stalled need not answer its cancel.
        void reader?.cancel().catch(() => {});
        await out.close();
      }
    } finally {
      clearTimeout(timer);
    }
  }
  if (have !== file.downloadSize || hash.digest("hex") !== file.downloadSha256) {
    rmSync(partial, { force: true });
    throw new Error(`${file.download} was not the file the app expects`);
  }
}

// The downloaded *from*, unpacked by zstd into *to*, sparse, and checked by its hash.
async function unpack(from: string, to: string, file: ImageFile): Promise<void> {
  const partial = `${to}.partial`;
  const said = await new Promise<string | null>((resolve) => {
    const zstd = spawn("/usr/bin/zstd", ["-q", "-d", "-f", "--sparse", from, "-o", partial], { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    zstd.stderr.on("data", (chunk: Buffer) => {
      stderr = (stderr + chunk.toString()).slice(-2000);
    });
    zstd.once("error", (error) => resolve(error.message));
    zstd.once("close", (code) => resolve(code === 0 ? null : stderr.trim() || `exit code ${code}`));
  });
  if (said !== null) {
    rmSync(partial, { force: true });
    // Kept while it is the download, as when the disk is full or zstd is missing; one that no longer
    // is, as after bit rot, goes, so the next try downloads it again.
    if (sizeOf(from) !== file.downloadSize || (await hashOf(from)).digest("hex") !== file.downloadSha256) rmSync(from, { force: true });
    throw new Error(`zstd could not unpack ${file.download}: ${said}`);
  }
  if (sizeOf(partial) !== file.size || (await hashOf(partial)).digest("hex") !== file.sha256) {
    rmSync(partial, { force: true });
    rmSync(from, { force: true });
    throw new Error(`${file.name} was not the file the app expects`);
  }
  settle(partial, to);
}

export type Delivery =
  | { state: "downloading"; done: number; total: number }
  | { state: "checking" } // the image's files, by their hashes, after a boot of it did not start
  | { state: "ready"; folder: string }
  | { state: "failed"; why: string };

/**
 * The app's one delivery of its image: started at its launch, and again by Retry
 * after a failure. Each change is told, so the status line follows it.
 */
export class ImageDelivery {
  state: Delivery;
  // The image's folder, once it is here.
  readonly folder: string;
  private running: Promise<string> | null = null;
  private readonly total: number;

  // *base* is read at each start: an install record put right is read at the next Retry.
  constructor(private readonly options: Omit<DeliverOptions, "progress" | "base"> & { base: () => string }, private readonly changed: () => void = () => {}) {
    this.total = options.manifest.files.reduce((sum, file) => sum + file.downloadSize, 0);
    this.folder = join(options.images, options.manifest.key);
    this.state = whole(this.folder, options.manifest) ? { state: "ready", folder: this.folder } : { state: "downloading", done: 0, total: this.total };
  }

  /**
   * Starts the delivery, unless one runs or the image is here. *check*, as at a Retry after
   * a boot of the image did not start: an image that is here has its files checked by their
   * hashes first, and is downloaded again once one differs.
   */
  start(check = false): void {
    if (this.running) return;
    if (this.state.state === "ready") {
      if (check) this.run(this.checked());
      return;
    }
    let base: string;
    try {
      base = this.options.base();
    } catch (error) {
      return this.set({ state: "failed", why: error instanceof Error ? error.message : String(error) });
    }
    if (this.state.state === "failed") this.set({ state: "downloading", done: 0, total: this.total });
    this.run(this.download(base));
  }

  private download(base: string): Promise<string> {
    return deliver({ ...this.options, base, progress: (done, total) => this.set({ state: "downloading", done, total }) });
  }

  private async checked(): Promise<string> {
    this.set({ state: "checking" });
    if (await intact(this.folder, this.options.manifest)) return this.folder;
    rmSync(this.folder, { recursive: true, force: true });
    this.set({ state: "downloading", done: 0, total: this.total });
    return this.download(this.options.base());
  }

  private run(running: Promise<string>): void {
    this.running = running;
    running.then((folder) => this.set({ state: "ready", folder }), (error: unknown) => {
      this.set({ state: "failed", why: error instanceof Error ? error.message : String(error) });
    }).finally(() => {
      this.running = null;
    });
  }

  /** Every other image goes, an older version's among them: called once this one has booted. */
  prune(): void {
    let names: string[] = [];
    try {
      names = readdirSync(this.options.images);
    } catch {
      return;
    }
    for (const name of names) if (name !== this.options.manifest.key) rmSync(join(this.options.images, name), { recursive: true, force: true });
  }

  /** Resolves once the image is here; rejects with why not once the delivery fails, or at *signal*. */
  async wait(signal: AbortSignal): Promise<void> {
    signal.throwIfAborted();
    const aborted = new Promise<never>((_resolve, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true }));
    aborted.catch(() => {});
    while (this.state.state === "downloading" || this.state.state === "checking") {
      if (!this.running) throw new Error("its download has not started");
      await Promise.race([this.running.catch(() => {}), aborted]);
    }
    if (this.state.state === "failed") throw new Error(this.state.why);
  }

  // Progress is told at each whole percent, not at each of its chunks.
  private set(state: Delivery): void {
    const was = this.state;
    this.state = state;
    const percent = (delivery: Delivery) => (delivery.state === "downloading" ? Math.floor((delivery.done * 100) / delivery.total) : -1);
    if (was.state === "downloading" && state.state === "downloading" && percent(was) === percent(state)) return;
    this.changed();
  }
}
