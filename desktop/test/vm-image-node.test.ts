// The guest image's delivery where the app unpacks it with Node's own zstd (src/vm/image.ts): on Windows, where Hyper-V
// opens a disk only as a VHD, and on a Mac; neither has a zstd program. Run on a Node whose zlib has zstd, as this
// package's tests' own Node 22.14 has not (test/image-probe.mjs): on Windows and on a Mac, this package's Electron, its
// main process as the app's, which unpacks the image there; on Linux, the app's own node, bin/node (npm run build
// first), with the module imported as Windows's and a Mac's app import it. Each download is made by that Node's zstd,
// and served by a local HTTP server as the install's base would serve it.

import { spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, truncateSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { Delivery, ImageManifest } from "../src/vm/image.js";
import { vhdFooter } from "../src/vm/vhd.js";
import { built } from "./built.js";
import { importedOn } from "./platforms.js";

const PACKAGE = fileURLToPath(new URL("..", import.meta.url));
const PROBE = join(PACKAGE, "test", "image-probe.mjs");
const KEY = "a".repeat(64);
const sha256 = (data: Buffer) => createHash("sha256").update(data).digest("hex");
// What the app unpacks its image on: on Windows and on a Mac its Electron's main process; on Linux, whose app unpacks
// with the zstd program, the app's own node, which has zstd too.
const RUNTIME = process.platform === "linux" ? join(PACKAGE, "bin", "node") : (createRequire(import.meta.url)("electron") as string);
// Whether this computer's file system leaves a hole where the unpack skips a block of zeros, as ext4 does on Linux. APFS
// keeps a file sparse by criteria of its own, which a file this small does not meet on a runner's Mac; NTFS fills the
// gap unless the file is marked sparse.
const HOLES = process.platform === "linux";

let dir: string;
let server: Server;
let base: string;
let served: Map<string, Buffer>;
let heard: string[];
let manifest: ImageManifest;
let rootfs: Buffer;
let kernel: Buffer;
let initrd: Buffer;

type Probed = { folder?: string; state?: Delivery; error?: string; left?: string[] };

// *args* run by the probe on the runtime, imported as *platform*'s app imports the image's module: its answer.
function probe(platform: NodeJS.Platform, ...args: string[]): Promise<Probed> {
  if (!existsSync(RUNTIME)) throw new Error(`${RUNTIME} is not there: run npm run build first`);
  built(join(PACKAGE, "dist", "vm", "image.js"));
  return new Promise((resolve, reject) => {
    const child = spawn(RUNTIME, [PROBE, platform, ...args], { stdio: ["ignore", "pipe", "pipe"] });
    let output = "";
    child.stdout.on("data", (chunk: Buffer) => (output += chunk.toString()));
    child.stderr.on("data", (chunk: Buffer) => (output += chunk.toString()));
    child.on("exit", () => {
      const line = output.split(/\r?\n/).find((said) => said.startsWith("PROBE "));
      if (line) resolve(JSON.parse(line.slice("PROBE ".length)) as Probed);
      else reject(new Error(output));
    });
  });
}

// *data* by each name, as build.sh publishes it: its download zstd's, made by the runtime's own zstd, and both hashed.
async function publish(files: Record<string, Buffer>, level = 19): Promise<ImageManifest["files"]> {
  const folder = join(dir, "src");
  mkdirSync(folder, { recursive: true });
  for (const [name, data] of Object.entries(files)) writeFileSync(join(folder, name), data);
  expect(await probe(process.platform, "pack", folder, String(level), ...Object.keys(files))).toEqual({ packed: Object.keys(files) });
  return Object.entries(files).map(([name, data]) => {
    const download = readFileSync(join(folder, `${name}.zst`));
    served.set(`/desktop/vm/${KEY}/${name}.zst`, download);
    return { name, size: data.length, sha256: sha256(data), download: `${name}.zst`, downloadSize: download.length, downloadSha256: sha256(download) };
  });
}

beforeEach(async () => {
  dir = mkdtempSync(join(tmpdir(), "vm-image-node-"));
  served = new Map();
  heard = [];
  // A disk with a run of zeros, which the unpack leaves as a hole, and data either side that does not compress away; and
  // zeros at its end, which the unpack writes none of but counts in its length. In whole sectors, as a disk is.
  rootfs = Buffer.concat([randomBytes(4096), Buffer.alloc(16 * 1024 * 1024), randomBytes(300 * 1024), Buffer.alloc(200 * 1024)]);
  kernel = randomBytes(200_000);
  initrd = randomBytes(50_000);
  server = createServer((request, response) => {
    heard.push(request.url ?? "");
    const body = served.get(request.url ?? "");
    if (!body) return void response.writeHead(404).end();
    response.writeHead(200, { "content-length": body.length });
    response.end(body);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  manifest = { key: KEY, arch: "amd64", files: await publish({ "rootfs.img": rootfs, vmlinuz: kernel, initrd }) };
}, 60_000);

afterEach(async () => {
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
  rmSync(dir, { recursive: true, force: true });
});

const images = () => join(dir, "data", "vm", "images");
const options = () => JSON.stringify({ manifest, base, images: images() });
const delivered = async (platform: NodeJS.Platform) => {
  const { folder, error } = await probe(platform, "deliver", options());
  if (!folder) throw new Error(error);
  return folder;
};

describe.skipIf(process.platform === "darwin")("the guest image's delivery on Windows, where Hyper-V opens a disk only as a VHD", { timeout: 60_000 }, () => {
  // The image as Windows attaches it: its bytes, as the manifest hashes them, then their fixed VHD footer.
  const footed = () => Buffer.concat([rootfs, vhdFooter(rootfs.length, sha256(rootfs))]);

  it("unpacks each download with Node's own zstd, gives the image its footer once its hash is checked, and the kernel and the initrd none, and writes none of its zeros", async () => {
    const folder = await delivered("win32");
    expect(readFileSync(join(folder, "rootfs.img")).equals(footed())).toBe(true);
    expect(readFileSync(join(folder, "vmlinuz")).equals(kernel)).toBe(true);
    expect(readFileSync(join(folder, "initrd")).equals(initrd)).toBe(true);
    if (HOLES) expect(statSync(join(folder, "rootfs.img")).blocks * 512).toBeLessThan(rootfs.length / 2);
    // Whole by its sizes, the footer's counted: asked again, nothing is fetched, and a delivery is ready at once.
    heard = [];
    expect(await delivered("win32")).toBe(folder);
    expect(heard).toEqual([]);
    const { ImageDelivery } = await importedOn("win32", () => import("../src/vm/image.js"));
    expect(new ImageDelivery({ manifest, images: images(), base: () => base }).state).toEqual({ state: "ready", folder });
  });

  it("takes the image a crash left unpacked with its footer, and unpacks again one left without", async () => {
    const work = join(images(), `${KEY}.partial`);
    for (const [left, fetched] of [[footed(), []], [rootfs, [`/desktop/vm/${KEY}/rootfs.img.zst`]]] as const) {
      rmSync(images(), { recursive: true, force: true });
      mkdirSync(work, { recursive: true });
      writeFileSync(join(work, "rootfs.img"), left);
      heard = [];
      const folder = await delivered("win32");
      expect(readFileSync(join(folder, "rootfs.img")).equals(footed())).toBe(true);
      expect(heard.filter((url) => url.includes("rootfs"))).toEqual(fetched);
    }
  });

  it("checks the image by its bytes before the footer when asked, and the footer by them, and delivers it again once either differs", async () => {
    const ready = await probe("win32", "delivery", options());
    expect(ready.state?.state).toBe("ready");
    const folder = (ready.state as { folder: string }).folder;
    const disk = join(folder, "rootfs.img");
    heard = [];
    expect(await probe("win32", "delivery", options(), "check")).toEqual({ state: { state: "ready", folder } });
    expect(heard).toEqual([]);
    // A bit flipped in the footer's id, and then in the disk's bytes, each at the same size.
    for (const at of [rootfs.length + 70, 100]) {
      const flipped = footed();
      flipped[at]! ^= 1;
      writeFileSync(disk, flipped);
      heard = [];
      expect(await probe("win32", "delivery", options(), "check"), String(at)).toEqual({ state: { state: "ready", folder } });
      expect(readFileSync(disk).equals(footed()), String(at)).toBe(true);
      expect(heard, String(at)).toContain(`/desktop/vm/${KEY}/rootfs.img.zst`);
    }
  });

  it("stops its unpack at its signal, as at the app's quit, and leaves the download alone of it [slow]", async () => {
    await quits("win32");
  });
});

describe.skipIf(process.platform === "win32")("the guest image's delivery on a Mac", { timeout: 60_000 }, () => {
  it("unpacks each download with Node's own zstd, gives the image no footer, and writes none of its zeros", async () => {
    const folder = await delivered("darwin");
    expect(readFileSync(join(folder, "rootfs.img")).equals(rootfs)).toBe(true);
    expect(readFileSync(join(folder, "vmlinuz")).equals(kernel)).toBe(true);
    expect(readFileSync(join(folder, "initrd")).equals(initrd)).toBe(true);
    if (HOLES) expect(statSync(join(folder, "rootfs.img")).blocks * 512).toBeLessThan(rootfs.length / 2);
  });

  it("stops its unpack at its signal, as at the app's quit, and leaves the download alone of it [slow]", async () => {
    await quits("darwin");
  });
});

// A disk whose unpack runs a while, 4 GB of zeros, delivered on *platform* and stopped as its unpack is under way: the
// delivery rejects with the quit's words, and its folder keeps the download, to be unpacked at the next start, and
// nothing of the unpack.
async function quits(platform: NodeJS.Platform): Promise<void> {
  const raw = join(dir, "src", "large.img");
  writeFileSync(raw, "");
  truncateSync(raw, 4 * 1024 ** 3);
  expect(await probe(process.platform, "pack", join(dir, "src"), "3", "large.img")).toEqual({ packed: ["large.img"] });
  rmSync(raw);
  const download = readFileSync(`${raw}.zst`);
  served.set(`/desktop/vm/${KEY}/rootfs.img.zst`, download);
  manifest = { ...manifest, files: [
    { name: "rootfs.img", size: 4 * 1024 ** 3, sha256: "0".repeat(64), download: "rootfs.img.zst", downloadSize: download.length, downloadSha256: sha256(download) },
    manifest.files[1]!, manifest.files[2]!,
  ] };
  expect(await probe(platform, "quit", options())).toEqual({ error: "the app quit", left: ["rootfs.img.zst"] });
}
