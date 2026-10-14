// The guest image's delivery (spec, Section 11, "The image"): the files the app's
// manifest names, downloaded from where the app was installed from into this user's
// data, each checked by its hash as downloaded and again unpacked. A download that
// stops, or that nothing comes for, is resumed from its .partial file. Each file is on
// disk before its rename, and the image's folder is renamed into place only once every
// file in it is checked; its last step is its completion mark, so a folder with that
// mark and its files' sizes is a whole image, and one without is downloaded again. On Windows, the
// image's disk is a fixed VHD: its footer is given once its hash is checked, and every later check of
// it reads its bytes before the footer, which the manifest hashes, and the footer by them.

import { createReadStream, existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, type Stats, statSync, writeFileSync } from "node:fs";
import { type FileHandle, open, statfs } from "node:fs/promises";
import { Writable } from "node:stream";
import { pipeline } from "node:stream/promises";
// Its zstd read as the unpack runs, not at the import: the tests' Node 22.14 has none (zstd came in 22.15 and 23.8).
import * as zlib from "node:zlib";

import { spawnClean } from "../clean-child.js";
import { download, type DownloadOptions, hashOf, sizeOf } from "../download.js";
import { dirname, join } from "../host-path.js";
import { forThisOs, notBuiltWords } from "../platform.js";
import { addFooter, endsInFooter, FOOTER_SIZE } from "./vhd.js";

// Each file the image has, as the manifest names it: unpacked, and as downloaded.
export interface ImageFile {
  name: string;
  size: number;
  sha256: string;
  download: string;
  downloadSize: number;
  downloadSha256: string;
}

// The arches an image is built for, as Docker and Debian name them (images/guest/inputs.sh).
export type ImageArch = "amd64" | "arm64";
const ARCHES: readonly ImageArch[] = ["amd64", "arm64"];

// resources/vm/manifest.json, which images/guest/build.sh writes and the app's tarball carries.
export interface ImageManifest {
  key: string;
  arch: ImageArch;
  files: ImageFile[];
}

const HASH = /^[0-9a-f]{64}$/;
// The files the VM boots from: a manifest names exactly these. The initrd is Hyper-V's: Linux and macOS
// boot without it, and download it with the image, which is one for every OS of its arch.
const NAMES = ["initrd", "rootfs.img", "vmlinuz"];
const size = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value > 0;
// The files the VM attaches as disks where its hypervisor opens a disk only as a VHD: Hyper-V's, on Windows. Each is
// the manifest's bytes, then their footer (vhd.ts). The release ships them raw, for every OS.
const VHDS: readonly string[] = forThisOs({ win32: ["rootfs.img"] }) ?? [];
// *file*'s size in the image's folder: with its footer, where it has one.
const held = (file: ImageFile) => file.size + (VHDS.includes(file.name) ? FOOTER_SIZE : 0);

/** This computer's own image arch, by Node's name for its arch (*arch*), or null for one no image is built for. */
export function imageArch(arch: NodeJS.Architecture = process.arch): ImageArch | null {
  return arch === "x64" ? "amd64" : arch === "arm64" ? "arm64" : null;
}

/**
 * The manifest at *path* of the image for *arch*, this computer's; throws when it is not one, and
 * when it is another arch's, so that no app downloads an image its computer cannot boot. Its names are never paths.
 */
export function readManifest(path: string, arch: ImageArch): ImageManifest {
  let manifest: Partial<ImageManifest> = {};
  try {
    manifest = JSON.parse(readFileSync(path, "utf8")) as Partial<ImageManifest>;
  } catch {
    // Not there, or not JSON: as one that names nothing.
  }
  const files = Array.isArray(manifest.files) ? manifest.files : [];
  const valid = typeof manifest.key === "string" && HASH.test(manifest.key) && ARCHES.includes(manifest.arch as ImageArch)
    && files.map((file) => file?.name).sort().join() === NAMES.join()
    && files.every((file) => size(file.size) && size(file.downloadSize) && HASH.test(String(file.sha256)) && HASH.test(String(file.downloadSha256))
      && file.download === `${file.name}.zst`);
  if (!valid) throw new Error(`${path} is not the guest image's manifest`);
  if (manifest.arch !== arch) throw new Error(`${path} is the manifest of the guest image for ${manifest.arch}, and this computer runs ${arch}'s`);
  return manifest as ImageManifest;
}

/**
 * The arch of the image in *folder*, as the manifest beside it names it, which images/guest/build.sh
 * writes: a test's or a development build's. Null where none is named.
 */
export function imageArchOf(folder: string): ImageArch | null {
  try {
    const { arch } = JSON.parse(readFileSync(join(folder, "manifest.json"), "utf8")) as { arch?: unknown };
    return ARCHES.includes(arch as ImageArch) ? (arch as ImageArch) : null;
  } catch {
    return null;
  }
}

/**
 * Throws unless *path* is root's own word: root's, with no other who may write it, and a file and
 * no link, wherever one leads. At whatever mode: an administrator may keep one read-only. With
 * *run*, as the helper must be, it is a program too, since pkexec could run no other, and one
 * that others may read, since the app reads its release keys from it as its user: the install
 * script asks its helper the same (roots_program in release/install.sh), so the two never answer
 * otherwise. A set-id or a sticky bit is not looked at, there or here: neither changes who may
 * write the file, and no script is run as its owner. *found* is what the file is, where a reader
 * holds it open and asks of what it reads, not of what its name leads to by then.
 */
export function rootsOwn(path: string, run = false, found: Stats = lstatSync(path)): void {
  if (found.isSymbolicLink()) throw new Error(`${path} is not the install script's: it is a link`);
  if (found.uid !== 0 || (found.mode & 0o022) !== 0) throw new Error(`${path} is not the install script's: only root may write it`);
  if (!found.isFile()) throw new Error(`${path} is not the install script's: it is no file`);
  if (run && (found.mode & 0o111) === 0) throw new Error(`${path} is not the install script's: it cannot be run`);
  if (run && (found.mode & 0o004) === 0) throw new Error(`${path} is not the install script's: its user cannot read it`);
}

/**
 * The base URL in the install record at *path*, which the install script writes:
 * {"base": "https://surogate.ai"}. Throws when there is none to read. *rootOwned*, as an
 * installed app's /etc/surogate/install.json is: a record that is not root's own word
 * (rootsOwn) is not taken, as it would say where each of this computer's users downloads from.
 */
export function installBase(path: string, rootOwned = false): string {
  let text: string;
  try {
    if (rootOwned) rootsOwn(path);
    text = readFileSync(path, "utf8");
  } catch (error) {
    const { code } = error as NodeJS.ErrnoException;
    if (code === undefined) throw error;
    if (code !== "ENOENT") throw new Error(`${path} could not be read: ${code}`);
    throw new Error("Surogate was not installed by its install script, so it does not know where it was installed from");
  }
  let base: unknown;
  try {
    base = (JSON.parse(text) as { base?: unknown }).base;
  } catch {
    // Not JSON: it names no base.
  }
  const url = typeof base === "string" && URL.canParse(base) ? new URL(base) : null;
  if (!url || !["https:", "http:"].includes(url.protocol)) throw new Error(`${path} names no web address that Surogate was installed from`);
  // A user and a password in it would go out with every request, to every address a redirect names; and
  // the app's own fetch refuses such an address in words of the system's. Refused here, in the app's own.
  if (url.username !== "" || url.password !== "") {
    throw new Error(`${path} names a web address with a user or a password in it, which Surogate does not send: run the install script again with a --base that has none`);
  }
  return url.href.replace(/\/+$/, "");
}

export interface DeliverOptions extends DownloadOptions {
  manifest: ImageManifest;
  base: string; // where the app was installed from: the files are at <base>/desktop/vm/<key>/
  images: string; // <data>/vm/images: one folder per image, by its key
  // Told how many of the downloads' bytes are here, of how many; and as each unpack begins, which
  // its hash and its syncs follow.
  progress?: (done: number, total: number) => void;
  unpacking?: () => void;
}

// How every download begins: a zstd frame's magic number.
const ZSTD_MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);
// An image's folder's last step, written once all of it is on disk.
const COMPLETE = "complete";
const gigabytes = (bytes: number) => `${(bytes / 1e9).toFixed(1)} GB`;

// How a file's data and a folder's entries reach the disk on this OS. Windows flushes a file only through a handle that
// may write it (FlushFileBuffers: "The file handle must have the GENERIC_WRITE access right"), and a folder not at all:
// NTFS logs a rename itself, after the data of the file renamed, which was flushed before it.
const FLUSHED = forThisOs({ win32: { mode: "r+", folders: false } }) ?? { mode: "r", folders: true };

// *path*'s data, or a folder's entries, on disk. Not on the main thread, which is the windows': a
// slow disk's flush of 2.9 GB would hold them.
async function sync(path: string): Promise<void> {
  if (!FLUSHED.folders && statSync(path).isDirectory()) return;
  const handle = await open(path, FLUSHED.mode);
  try {
    await handle.sync();
  } finally {
    await handle.close();
  }
}

// *from* renamed to *to* so that a power cut finds the new name only with all of its bytes,
// which ext4 commits apart from a rename: its data and the folder it was made in first.
async function settle(from: string, to: string): Promise<void> {
  await sync(from);
  await sync(dirname(from));
  renameSync(from, to);
  await sync(dirname(to));
}

// What *folder*'s files hold of the disk, an image's or one's .partial: they are its entries alone.
function allocated(folder: string): number {
  try {
    return readdirSync(folder).reduce((sum, name) => {
      const entry = lstatSync(join(folder, name));
      return sum + (entry.isFile() ? entry.blocks * 512 : 0);
    }, 0);
  } catch {
    return 0;
  }
}

// Whether *folder* is the whole image: its last step there, and each file at its manifest's size, and its footer's.
function whole(folder: string, manifest: ImageManifest): boolean {
  return existsSync(join(folder, COMPLETE)) && manifest.files.every((file) => sizeOf(join(folder, file.name)) === held(file));
}

// Whether each of *folder*'s files has its manifest's size and sha256, over its bytes before any footer, and
// whether that footer is theirs.
async function intact(folder: string, manifest: ImageManifest): Promise<boolean> {
  for (const file of manifest.files) {
    const path = join(folder, file.name);
    if (sizeOf(path) !== held(file) || (await hashOf(path, file.size)).digest("hex") !== file.sha256) return false;
    if (VHDS.includes(file.name) && !(await endsInFooter(path, file.sha256))) return false;
  }
  return true;
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
  // What is there is taken only as a regular file, never through a link, and an unpacked file only at
  // its manifest's size, with its footer where it has one. What an unpack killed partway left is never
  // taken, and holds disk the delivery needs.
  for (const name of readdirSync(work)) if (!lstatSync(join(work, name)).isFile()) rmSync(join(work, name), { recursive: true, force: true });
  for (const file of manifest.files) {
    rmSync(`${join(work, file.name)}.partial`, { force: true });
    if (sizeOf(join(work, file.name)) !== held(file)) rmSync(join(work, file.name), { force: true });
  }
  const total = manifest.files.reduce((sum, file) => sum + file.downloadSize, 0);
  // What is here already: a file unpacked, a download whole or in part.
  const here = (file: ImageFile) => (existsSync(join(work, file.name)) ? file.downloadSize
    : sizeOf(join(work, file.download)) || Math.min(sizeOf(join(work, `${file.download}.partial`)), file.downloadSize));
  const needed = manifest.files.filter((file) => !existsSync(join(work, file.name)))
    .reduce((sum, file) => sum + file.size + file.downloadSize - here(file), 0);
  const { bavail, bsize } = await statfs(work);
  let free = bavail * bsize;
  if (free < needed) {
    // An older version's image, which this one never boots, goes now when only it stands in the way,
    // rather than at this one's first boot.
    const others = readdirSync(images).filter((name) => name !== manifest.key && name !== `${manifest.key}.partial`);
    const held = others.reduce((sum, name) => sum + allocated(join(images, name)), 0);
    if (free + held >= needed) {
      for (const name of others) rmSync(join(images, name), { recursive: true, force: true });
      free += held;
    }
  }
  if (free < needed) throw new Error(`there is not enough free disk space: it needs ${gigabytes(needed)}, and ${gigabytes(free)} is free`);
  for (const file of manifest.files) {
    const unpacked = join(work, file.name);
    const downloaded = join(work, file.download);
    if (existsSync(unpacked)) {
      // Left by a crash between the unpack's rename and this removal: never carried into the image's folder.
      rmSync(downloaded, { force: true });
      continue;
    }
    if (!existsSync(downloaded)) {
      const others = manifest.files.filter((other) => other !== file).reduce((sum, other) => sum + here(other), 0);
      const url = `${options.base}/desktop/vm/${manifest.key}/${file.download}`;
      await download({ url, name: file.download, size: file.downloadSize, sha256: file.downloadSha256, magic: ZSTD_MAGIC }, `${downloaded}.partial`, options,
        (have) => options.progress?.(others + have, total));
      await settle(`${downloaded}.partial`, downloaded);
    }
    signal?.throwIfAborted();
    options.unpacking?.();
    await unpack(downloaded, unpacked, file, signal);
    rmSync(downloaded);
  }
  await settle(work, folder);
  writeFileSync(join(folder, COMPLETE), `${manifest.key}\n`);
  await sync(join(folder, COMPLETE));
  await sync(folder);
  return folder;
}

// The zstd program's unpack of *from* into *to*, sparse: Linux's. Run by its path, it is given nothing of the app's
// environment, and ends at *signal*. Why it could not, or null.
function byProgram(from: string, to: string, signal?: AbortSignal): Promise<string | null> {
  return new Promise<string | null>((resolve) => {
    const zstd = spawnClean("/usr/bin/zstd", ["-q", "-d", "-f", "--sparse", from, "-o", to], { stdio: ["ignore", "ignore", "pipe"], env: {}, signal });
    let stderr = "";
    zstd.stderr?.on("data", (chunk: Buffer) => {
      stderr = (stderr + chunk.toString()).slice(-2000);
    });
    zstd.once("error", (error) => resolve(error.message));
    zstd.once("close", (code) => resolve(code === 0 ? null : stderr.trim() || `exit code ${code}`));
  });
}

// The blocks the unpack in Node writes, or skips where they are all zeros, as the zstd program's --sparse does.
const BLOCK = 64 * 1024;
const ZEROS = Buffer.alloc(BLOCK);

// What Node's own zstd unpacks, written to *file* a block at a time: a block of zeros is not written but passed over,
// which leaves a hole where the file system makes one there (APFS does; NTFS fills it, as its file is not marked
// sparse), and the file's length is set at the end, the zeros it ends with counted.
class Sparse extends Writable {
  private readonly block = Buffer.alloc(BLOCK);
  private held = 0;
  private at = 0;

  constructor(private readonly file: FileHandle) {
    super();
  }

  private async put(): Promise<void> {
    const bytes = this.block.subarray(0, this.held);
    if (!bytes.equals(ZEROS.subarray(0, this.held))) await this.file.write(bytes, 0, bytes.length, this.at);
    this.at += this.held;
    this.held = 0;
  }

  override _write(chunk: Buffer, _encoding: BufferEncoding, done: (error?: Error | null) => void): void {
    void (async () => {
      for (let from = 0; from < chunk.length;) {
        const copied = chunk.copy(this.block, this.held, from);
        from += copied;
        this.held += copied;
        if (this.held === BLOCK) await this.put();
      }
    })().then(() => done(), done);
  }

  override _final(done: (error?: Error | null) => void): void {
    this.put().then(() => this.file.truncate(this.at)).then(() => done(), done);
  }
}

// Node's own zstd's unpack of *from* into *to*, sparse: a Mac's and Windows's, which have no zstd program. In the app's
// process, Electron's main, whose Node has zstd; it ends at *signal*. Why it could not, or null.
async function byNode(from: string, to: string, signal?: AbortSignal): Promise<string | null> {
  try {
    const file = await open(to, "w");
    try {
      await pipeline(createReadStream(from), zlib.createZstdDecompress(), new Sparse(file), { signal });
    } finally {
      await file.close();
    }
    return null;
  } catch (error) {
    return error instanceof Error ? error.message : String(error);
  }
}

// How this OS unpacks a download: Linux by the zstd program, which its install script installs; a Mac and Windows by
// Node's own zstd.
const UNPACK = forThisOs({ linux: byProgram, darwin: byNode, win32: byNode });

// The downloaded *from*, unpacked by zstd into *to*, sparse, and checked by its hash; then a disk that
// is a VHD here given its footer. The unpack ends at *signal*, as at the app's quit.
async function unpack(from: string, to: string, file: ImageFile, signal?: AbortSignal): Promise<void> {
  const partial = `${to}.partial`;
  const said = UNPACK ? await UNPACK(from, partial, signal) : notBuiltWords("The guest image's unpack");
  if (said !== null) {
    rmSync(partial, { force: true });
    signal?.throwIfAborted();
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
  if (VHDS.includes(file.name)) await addFooter(partial, file.sha256);
  await settle(partial, to);
}

export type Delivery =
  | { state: "downloading"; done: number; total: number }
  | { state: "unpacking" } // a download, into the image's file, then its hash and its syncs
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
  constructor(private readonly options: Omit<DeliverOptions, "progress" | "unpacking" | "base"> & { base: () => string }, private readonly changed: () => void = () => {}) {
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
    return deliver({
      ...this.options, base, progress: (done, total) => this.set({ state: "downloading", done, total }), unpacking: () => this.set({ state: "unpacking" }),
    });
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
    for (const name of names) {
      if (name === this.options.manifest.key) continue;
      try {
        rmSync(join(this.options.images, name), { recursive: true, force: true });
      } catch {
        // Not this app's to remove now: tried again at the next boot.
      }
    }
  }

  /** Resolves once the image is here; rejects with why not once the delivery fails, or at *signal*. */
  async wait(signal: AbortSignal): Promise<void> {
    signal.throwIfAborted();
    const aborted = new Promise<never>((_resolve, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true }));
    aborted.catch(() => {});
    while (this.state.state === "downloading" || this.state.state === "unpacking" || this.state.state === "checking") {
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
