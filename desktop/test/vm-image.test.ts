// The guest image's delivery: its manifest, the install record's base, and the download,
// served by a local HTTP server as the install's base would serve it.

import { spawnSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import {
  chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, symlinkSync, truncateSync, writeFileSync,
} from "node:fs";
import { statfs } from "node:fs/promises";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { deliver, ImageDelivery, type ImageManifest, installBase, readManifest } from "../src/vm/image.js";

const sha256 = (data: Buffer) => createHash("sha256").update(data).digest("hex");
const KEY = "a".repeat(64);

let dir: string;
let server: Server;
let base: string;
// What the server holds, by path, and how it answers each request: the default serves a Range.
let served: Map<string, Buffer>;
let heard: Array<{ url: string; range: string | undefined }>;
let answer: (request: IncomingMessage, response: ServerResponse, body: Buffer) => void;
let manifest: ImageManifest;
let rootfs: Buffer;
let kernel: Buffer;

const ranged = (request: IncomingMessage, response: ServerResponse, body: Buffer) => {
  const from = Number(/^bytes=(\d+)-$/.exec(request.headers.range ?? "")?.[1] ?? 0);
  if (from > 0) response.writeHead(206, { "content-range": `bytes ${from}-${body.length - 1}/${body.length}`, "content-length": body.length - from });
  else response.writeHead(200, { "content-length": body.length });
  response.end(body.subarray(from));
};

// *data*, as build.sh publishes it: its download zstd's, and both hashed in the manifest.
function publish(name: string, data: Buffer): ImageManifest["files"][number] {
  const raw = join(dir, "src", name);
  mkdirSync(join(dir, "src"), { recursive: true });
  writeFileSync(raw, data);
  expect(spawnSync("zstd", ["-q", "-f", raw, "-o", `${raw}.zst`]).status).toBe(0);
  const download = readFileSync(`${raw}.zst`);
  served.set(`/desktop/vm/${KEY}/${name}.zst`, download);
  return { name, size: data.length, sha256: sha256(data), download: `${name}.zst`, downloadSize: download.length, downloadSha256: sha256(download) };
}

beforeEach(async () => {
  dir = mkdtempSync(join(tmpdir(), "vm-image-"));
  served = new Map();
  heard = [];
  answer = ranged;
  // A disk with a run of zeros, which the unpack leaves as a hole, and data either side that does not compress away.
  rootfs = Buffer.concat([randomBytes(4096), Buffer.alloc(16 * 1024 * 1024), randomBytes(300_000)]);
  kernel = randomBytes(200_000);
  manifest = { key: KEY, files: [publish("rootfs.img", rootfs), publish("vmlinuz", kernel)] };
  server = createServer((request, response) => {
    heard.push({ url: request.url ?? "", range: request.headers.range });
    const body = served.get(request.url ?? "");
    if (!body) return void response.writeHead(404).end();
    answer(request, response, body);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

afterEach(async () => {
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
  rmSync(dir, { recursive: true, force: true });
});

const images = () => join(dir, "data", "vm", "images");
const options = () => ({ manifest, base, images: images() });
// As the app's delivery takes them: the base read at each start.
const delivering = () => ({ ...options(), base: () => base });

describe("the guest image's delivery", () => {
  it("downloads each file, checks it as downloaded and unpacked, leaves the zeros a hole, and puts the image's folder in place whole", async () => {
    const told: Array<[number, number]> = [];
    const folder = await deliver({ ...options(), progress: (done, total) => told.push([done, total]) });
    expect(folder).toBe(join(images(), KEY));
    expect(readFileSync(join(folder, "rootfs.img")).equals(rootfs)).toBe(true);
    expect(readFileSync(join(folder, "vmlinuz")).equals(kernel)).toBe(true);
    // Only the unpacked files are kept, nothing partial, and the mark of a whole image, written last.
    expect(readdirSync(images())).toEqual([KEY]);
    expect(readdirSync(folder).sort()).toEqual(["complete", "rootfs.img", "vmlinuz"]);
    expect(statSync(join(folder, "rootfs.img")).blocks * 512).toBeLessThan(rootfs.length / 2);
    const total = manifest.files.reduce((sum, file) => sum + file.downloadSize, 0);
    expect(told.at(-1)).toEqual([total, total]);
    // A folder that is there with its mark and its files' sizes is a whole image: asked again, nothing is fetched.
    heard = [];
    expect(await deliver(options())).toBe(folder);
    expect(heard).toEqual([]);
  });

  it("takes an image's folder that a crash left with a short file, or without its last step, for missing, and delivers it again", async () => {
    const folder = await deliver(options());
    for (const broken of [
      () => truncateSync(join(folder, "rootfs.img"), 4096),
      () => rmSync(join(folder, "complete")),
    ]) {
      broken();
      expect(new ImageDelivery(delivering()).state).toMatchObject({ state: "downloading" });
      heard = [];
      expect(await deliver(options())).toBe(folder);
      expect(readFileSync(join(folder, "rootfs.img")).equals(rootfs)).toBe(true);
      expect(heard.map(({ url }) => url).sort()).toEqual([`/desktop/vm/${KEY}/rootfs.img.zst`, `/desktop/vm/${KEY}/vmlinuz.zst`]);
    }
  });

  it("removes a download that a crash left beside its unpacked file, and never carries it into the image's folder", async () => {
    // Killed between the unpack's rename and the download's removal: both files are there.
    const work = join(images(), `${KEY}.partial`);
    mkdirSync(work, { recursive: true });
    writeFileSync(join(work, "rootfs.img"), rootfs);
    writeFileSync(join(work, "rootfs.img.zst"), served.get(`/desktop/vm/${KEY}/rootfs.img.zst`)!);
    const folder = await deliver(options());
    expect(readdirSync(folder).sort()).toEqual(["complete", "rootfs.img", "vmlinuz"]);
    expect(heard.map(({ url }) => url)).toEqual([`/desktop/vm/${KEY}/vmlinuz.zst`]);
  });

  it("resumes a download that stopped from its .partial, with a Range", async () => {
    const cut = 100_000;
    answer = (request, response, body) => {
      if (request.headers.range || !request.url?.endsWith("rootfs.img.zst")) return ranged(request, response, body);
      // The connection drops partway through, as a laptop's Wi-Fi does.
      response.writeHead(200, { "content-length": body.length });
      response.write(body.subarray(0, cut));
      setTimeout(() => response.socket?.destroy(), 200);
    };
    await expect(deliver(options())).rejects.toThrow(/^the download of rootfs\.img\.zst stopped: /);
    expect(statSync(join(images(), `${KEY}.partial`, "rootfs.img.zst.partial")).size).toBe(cut);
    const folder = await deliver(options());
    expect(readFileSync(join(folder, "rootfs.img")).equals(rootfs)).toBe(true);
    expect(heard.filter(({ url }) => url.endsWith("rootfs.img.zst")).map(({ range }) => range)).toEqual([undefined, `bytes=${cut}-`]);
  });

  it("stops a download that nothing more comes for, as through a proxy that holds it or after a sleep, and keeps what came for the next try", async () => {
    const cut = 100_000;
    let quiet: "headers" | "body" = "headers";
    answer = (request, response, body) => {
      if (request.headers.range || !request.url?.endsWith("rootfs.img.zst")) return ranged(request, response, body);
      // Nothing at all, or the headers and the first bytes; and then nothing, the connection kept open.
      if (quiet === "headers") return;
      response.writeHead(200, { "content-length": body.length });
      response.write(body.subarray(0, cut));
    };
    for (const at of ["headers", "body"] as const) {
      quiet = at;
      const begun = performance.now();
      await expect(deliver({ ...options(), stallMs: 500 })).rejects.toThrow("the download of rootfs.img.zst stopped: nothing came for 0.5 s");
      expect(performance.now() - begun).toBeLessThan(3_000);
    }
    expect(statSync(join(images(), `${KEY}.partial`, "rootfs.img.zst.partial")).size).toBe(cut);
    const folder = await deliver(options());
    expect(readFileSync(join(folder, "rootfs.img")).equals(rootfs)).toBe(true);
    expect(heard.filter(({ url }) => url.endsWith("rootfs.img.zst")).map(({ range }) => range)).toEqual([undefined, undefined, `bytes=${cut}-`]);
  });

  it("keeps what came of a download whose answer ended cleanly but short, as a server that caps its ranges sends, and asks for the rest", async () => {
    const cut = 100_000;
    const size = served.get(`/desktop/vm/${KEY}/rootfs.img.zst`)!.length;
    answer = (request, response, body) => {
      if (request.headers.range || !request.url?.endsWith("rootfs.img.zst")) return ranged(request, response, body);
      // Chunked, and its end sent after its first 100 000 bytes.
      response.writeHead(200).end(body.subarray(0, cut));
    };
    await expect(deliver(options())).rejects.toThrow(`the download of rootfs.img.zst stopped: it ended after ${cut} of ${size} bytes`);
    expect(statSync(join(images(), `${KEY}.partial`, "rootfs.img.zst.partial")).size).toBe(cut);
    expect(readFileSync(join(await deliver(options()), "rootfs.img")).equals(rootfs)).toBe(true);
    expect(heard.filter(({ url }) => url.endsWith("rootfs.img.zst")).map(({ range }) => range)).toEqual([undefined, `bytes=${cut}-`]);
  });

  it("gives a download's first bytes the whole idle bound once its headers have come", async () => {
    answer = (request, response, body) => {
      if (!request.url?.endsWith("rootfs.img.zst")) return ranged(request, response, body);
      // The headers 0.7 s on, and the body 0.7 s after them: each within the bound, and both past it from the request.
      setTimeout(() => {
        response.writeHead(200, { "content-length": body.length }).flushHeaders();
        setTimeout(() => response.end(body), 700);
      }, 700);
    };
    expect(readFileSync(join(await deliver({ ...options(), stallMs: 1_000 }), "rootfs.img")).equals(rootfs)).toBe(true);
  });

  it("starts a download again when a server sends the whole file for a Range", async () => {
    const partial = join(images(), `${KEY}.partial`, "rootfs.img.zst.partial");
    mkdirSync(join(images(), `${KEY}.partial`), { recursive: true });
    writeFileSync(partial, served.get(`/desktop/vm/${KEY}/rootfs.img.zst`)!.subarray(0, 500));
    answer = (_request, response, body) => response.writeHead(200, { "content-length": body.length }).end(body);
    const folder = await deliver(options());
    expect(readFileSync(join(folder, "rootfs.img")).equals(rootfs)).toBe(true);
  });

  it("refuses a download that is not the file the manifest names, as a captive portal's page, and keeps nothing of it", async () => {
    answer = (_request, response) => response.writeHead(200, { "content-type": "text/html" }).end("<html>Sign in to the Wi-Fi</html>");
    await expect(deliver(options())).rejects.toThrow("rootfs.img.zst was not the file the app expects");
    expect(existsSync(join(images(), `${KEY}.partial`, "rootfs.img.zst.partial"))).toBe(false);
    expect(existsSync(join(images(), KEY))).toBe(false);
    answer = ranged;
    expect(readFileSync(join(await deliver(options()), "vmlinuz")).equals(kernel)).toBe(true);
  });

  it("keeps what it downloaded when a captive portal's page answers its resume, and resumes once the page has gone", async () => {
    const partial = join(images(), `${KEY}.partial`, "rootfs.img.zst.partial");
    mkdirSync(join(images(), `${KEY}.partial`), { recursive: true });
    const head = served.get(`/desktop/vm/${KEY}/rootfs.img.zst`)!.subarray(0, 500);
    writeFileSync(partial, head);
    answer = (_request, response) => response.writeHead(200, { "content-type": "text/html" }).end("<html>Sign in to the Wi-Fi</html>");
    await expect(deliver(options())).rejects.toThrow("rootfs.img.zst was not the file the app expects");
    expect(readFileSync(partial).equals(head)).toBe(true);
    answer = ranged;
    expect(readFileSync(join(await deliver(options()), "rootfs.img")).equals(rootfs)).toBe(true);
    expect(heard.filter(({ url }) => url.endsWith("rootfs.img.zst")).map(({ range }) => range)).toEqual(["bytes=500-", "bytes=500-"]);
  });

  it("starts a download again from its start when its resume is answered from another offset", async () => {
    const partial = join(images(), `${KEY}.partial`, "rootfs.img.zst.partial");
    mkdirSync(join(images(), `${KEY}.partial`), { recursive: true });
    writeFileSync(partial, served.get(`/desktop/vm/${KEY}/rootfs.img.zst`)!.subarray(0, 500));
    answer = (request, response, body) => {
      if (!request.headers.range) return ranged(request, response, body);
      // A range, but not the one asked for: the file from its start.
      response.writeHead(206, { "content-range": `bytes 0-${body.length - 1}/${body.length}`, "content-length": body.length }).end(body);
    };
    await expect(deliver(options())).rejects.toThrow("the download of rootfs.img.zst did not resume where it stopped");
    expect(existsSync(partial)).toBe(false);
    expect(readFileSync(join(await deliver(options()), "rootfs.img")).equals(rootfs)).toBe(true);
    expect(heard.filter(({ url }) => url.endsWith("rootfs.img.zst")).map(({ range }) => range)).toEqual(["bytes=500-", undefined]);
  });

  it("refuses a resume whose answer says it is the rest but is not, and stops a body past the file's size there, keeping neither", async () => {
    const partial = join(images(), `${KEY}.partial`, "rootfs.img.zst.partial");
    mkdirSync(join(images(), `${KEY}.partial`), { recursive: true });
    writeFileSync(partial, served.get(`/desktop/vm/${KEY}/rootfs.img.zst`)!.subarray(0, 500));
    // A 206 that names the offset asked for, and sends the file from its start.
    answer = (request, response, body) => {
      if (!request.headers.range) return ranged(request, response, body);
      response.writeHead(206, { "content-range": `bytes 500-${body.length - 1}/${body.length}`, "content-length": body.length - 500 }).end(body.subarray(0, body.length - 500));
    };
    await expect(deliver(options())).rejects.toThrow("rootfs.img.zst was not the file the app expects");
    expect(existsSync(partial)).toBe(false);
    // The file, and then more for as long as the connection lasts.
    answer = (_request, response, body) => {
      response.writeHead(200).write(body);
      const more = setInterval(() => response.write(Buffer.alloc(64 * 1024)), 10);
      response.once("close", () => clearInterval(more));
    };
    await expect(deliver(options())).rejects.toThrow("rootfs.img.zst was not the file the app expects");
    expect(existsSync(partial)).toBe(false);
    answer = ranged;
    expect(readFileSync(join(await deliver(options()), "rootfs.img")).equals(rootfs)).toBe(true);
  });

  it("refuses an unpacked file that is not the one the manifest names, and keeps neither it nor its download", async () => {
    manifest = { ...manifest, files: manifest.files.map((file) => (file.name === "vmlinuz" ? { ...file, sha256: "0".repeat(64) } : file)) };
    await expect(deliver(options())).rejects.toThrow("vmlinuz was not the file the app expects");
    expect(readdirSync(join(images(), `${KEY}.partial`)).sort()).toEqual(["rootfs.img"]);
  });

  it("unpacks a download already in its folder without fetching it, and when zstd cannot, keeps it only while it is still the download", async () => {
    const work = join(images(), `${KEY}.partial`);
    mkdirSync(work, { recursive: true });
    // The download, whole, and zstd unable to write beside it, as on a full disk: it is kept, and unpacked once zstd can.
    writeFileSync(join(work, "rootfs.img.zst"), served.get(`/desktop/vm/${KEY}/rootfs.img.zst`)!);
    chmodSync(work, 0o500);
    try {
      await expect(deliver(options())).rejects.toThrow(/^zstd could not unpack rootfs\.img\.zst: /);
    } finally {
      chmodSync(work, 0o700);
    }
    expect(existsSync(join(work, "rootfs.img.zst"))).toBe(true);
    expect(readFileSync(join(await deliver(options()), "rootfs.img")).equals(rootfs)).toBe(true);
    expect(heard.map(({ url }) => url)).toEqual([`/desktop/vm/${KEY}/vmlinuz.zst`]);
    // One that is no longer the download, as after bit rot, goes, and the next try downloads it again.
    rmSync(join(images(), KEY), { recursive: true });
    mkdirSync(work);
    writeFileSync(join(work, "rootfs.img.zst"), randomBytes(1000));
    await expect(deliver(options())).rejects.toThrow(/^zstd could not unpack rootfs\.img\.zst: /);
    expect(existsSync(join(work, "rootfs.img.zst"))).toBe(false);
    expect(readFileSync(join(await deliver(options()), "rootfs.img")).equals(rootfs)).toBe(true);
  });

  it("unpacks with nothing of the app's environment, and stops its unpack at its signal", { timeout: 30_000 }, async () => {
    // A disk whose unpack runs a while: 4 GB, all of it a hole.
    const raw = join(dir, "src", "large.img");
    writeFileSync(raw, "");
    truncateSync(raw, 4 * 1024 ** 3);
    expect(spawnSync("zstd", ["-q", "-f", "--rm", raw, "-o", `${raw}.zst`]).status).toBe(0);
    const download = readFileSync(`${raw}.zst`);
    served.set(`/desktop/vm/${KEY}/rootfs.img.zst`, download);
    manifest = { ...manifest, files: [
      { name: "rootfs.img", size: 4 * 1024 ** 3, sha256: "0".repeat(64), download: "rootfs.img.zst", downloadSize: download.length, downloadSha256: sha256(download) },
      manifest.files[1]!,
    ] };
    const stop = new AbortController();
    const delivered = deliver({ ...options(), signal: stop.signal });
    delivered.catch(() => {});
    const unpacking = () => Number(spawnSync("pgrep", ["-f", `^/usr/bin/zstd .*${join(images(), `${KEY}.partial`, "rootfs.img.zst")}`], { encoding: "utf8" }).stdout.trim() || 0);
    await expect.poll(unpacking, { timeout: 10_000, interval: 20 }).toBeGreaterThan(0);
    const zstd = unpacking();
    // Its variables' names alone, so a failure prints none of their values.
    expect(readFileSync(`/proc/${zstd}/environ`, "utf8").split("\0").filter(Boolean).map((entry) => entry.split("=")[0])).toEqual([]);
    stop.abort(new Error("the app quit"));
    await expect(delivered).rejects.toThrow("the app quit");
    await expect.poll(unpacking, { timeout: 2_000 }).toBe(0);
  });

  it("takes nothing in its folder by its name alone: an unpacked file of another size is made again, and a link there is not followed", async () => {
    const work = join(images(), `${KEY}.partial`);
    mkdirSync(work, { recursive: true });
    writeFileSync(join(work, "rootfs.img"), "not the disk\n");
    const outside = join(dir, "the user's file");
    writeFileSync(outside, "the user's own\n");
    symlinkSync(outside, join(work, "vmlinuz.zst.partial"));
    const folder = await deliver(options());
    expect(readFileSync(join(folder, "rootfs.img")).equals(rootfs)).toBe(true);
    expect(readFileSync(join(folder, "vmlinuz")).equals(kernel)).toBe(true);
    expect(readFileSync(outside, "utf8")).toBe("the user's own\n");
  });

  it("checks the free space before it fetches anything, once what a killed unpack left no longer holds any of it", async () => {
    // zstd's output when the app was killed mid-unpack: up to the image's size, and never taken.
    const work = join(images(), `${KEY}.partial`);
    mkdirSync(work, { recursive: true });
    writeFileSync(join(work, "rootfs.img.partial"), randomBytes(100_000));
    manifest = { ...manifest, files: manifest.files.map((file) => ({ ...file, size: 2 ** 52 })) };
    await expect(deliver(options())).rejects.toThrow(/^there is not enough free disk space: it needs \d+\.\d GB, and \d+\.\d GB is free$/);
    expect(heard).toEqual([]);
    expect(readdirSync(work)).toEqual([]);
  });

  it("lets an older version's image go first when it alone stands between the delivery and the free space it needs", async () => {
    // 64 MiB on disk of an image this version never boots.
    const older = join(images(), "b".repeat(64));
    mkdirSync(older, { recursive: true });
    writeFileSync(join(older, "rootfs.img"), randomBytes(64 * 1024 * 1024));
    // Needed: 32 MiB more than is free, which the older image's room covers. Its size is not the disk's, so the unpack then refuses it.
    const { bavail, bsize } = await statfs(images());
    const others = manifest.files.reduce((sum, file) => sum + file.downloadSize, 0) + manifest.files[1]!.size;
    manifest = { ...manifest, files: [{ ...manifest.files[0]!, size: bavail * bsize + 32 * 1024 * 1024 - others }, manifest.files[1]!] };
    await expect(deliver(options())).rejects.toThrow("rootfs.img was not the file the app expects");
    expect(existsSync(older)).toBe(false);
  });

  it("says what the server answered when it does not serve the file", async () => {
    served.clear();
    await expect(deliver(options())).rejects.toThrow(`${new URL(base).host} answered 404 for rootfs.img.zst`);
  });

  it("lets go of an answer it does not take, so its connection is not held open", async () => {
    let closed = false;
    answer = (_request, response) => {
      response.writeHead(500).write("x".repeat(1000));
      response.once("close", () => {
        closed = true;
      });
    };
    await expect(deliver(options())).rejects.toThrow(`${new URL(base).host} answered 500 for rootfs.img.zst`);
    await expect.poll(() => closed, { timeout: 2_000 }).toBe(true);
  });

  it("stops at its signal, and keeps what it downloaded for the next try", async () => {
    const stop = new AbortController();
    answer = (_request, response, body) => {
      response.writeHead(200, { "content-length": body.length });
      response.write(body.subarray(0, 100_000));
      setTimeout(() => stop.abort(new Error("the app quit")), 200);
    };
    await expect(deliver({ ...options(), signal: stop.signal })).rejects.toThrow("the app quit");
    expect(statSync(join(images(), `${KEY}.partial`, "rootfs.img.zst.partial")).size).toBe(100_000);
  });
});

describe("the image's manifest and the install record", () => {
  it("reads the manifest build.sh writes, and refuses one that is not JSON, whose names are not the image's files, or whose key is not a hash", () => {
    const path = join(dir, "manifest.json");
    writeFileSync(path, JSON.stringify(manifest));
    expect(readManifest(path)).toEqual(manifest);
    for (const bad of [
      { ...manifest, key: "../x" },
      { ...manifest, files: [manifest.files[0]] },
      { ...manifest, files: manifest.files.map((file) => ({ ...file, download: "../../.bashrc" })) },
      { ...manifest, files: manifest.files.map((file) => (file.name === "vmlinuz" ? { ...file, name: "../vmlinuz" } : file)) },
      { ...manifest, files: manifest.files.map((file) => ({ ...file, size: -1 })) },
    ]) {
      writeFileSync(path, JSON.stringify(bad));
      expect(() => readManifest(path)).toThrow("is not the guest image's manifest");
    }
    writeFileSync(path, "{");
    expect(() => readManifest(path)).toThrow(`${path} is not the guest image's manifest`);
  });

  it("reads where the app was installed from, and says when it was not installed by its script", () => {
    const path = join(dir, "install.json");
    writeFileSync(path, JSON.stringify({ base: "https://surogate.ai/" }));
    expect(installBase(path)).toBe("https://surogate.ai");
    writeFileSync(path, JSON.stringify({ base: "file:///etc" }));
    expect(() => installBase(path)).toThrow("names no web address to download the sandbox from");
    expect(() => installBase(join(dir, "missing.json"))).toThrow("Surogate was not installed by its install script");
    // One that is there but cannot be read, or is not JSON, is not a missing install.
    writeFileSync(path, "{");
    expect(() => installBase(path)).toThrow(`${path} names no web address to download the sandbox from`);
    chmodSync(path, 0o000);
    expect(() => installBase(path)).toThrow(`${path} could not be read: EACCES`);
  });

  it("takes an installed app's record only when root alone may write it, as the install script leaves it", () => {
    const path = join(dir, "install.json");
    writeFileSync(path, JSON.stringify({ base: "https://surogate.ai" }));
    expect(installBase(path)).toBe("https://surogate.ai");
    expect(() => installBase(path, true)).toThrow(`${path} is not the install script's: only root may write it`);
    // Root's own, as /etc/passwd is: taken, and read for its base.
    expect(() => installBase("/etc/passwd", true)).toThrow("/etc/passwd names no web address to download the sandbox from");
    expect(() => installBase(join(dir, "missing.json"), true)).toThrow("Surogate was not installed by its install script");
  });
});

describe("ImageDelivery", () => {
  it("is ready at once for an image that is there, and follows a download to ready, telling each whole percent", async () => {
    let told = 0;
    const delivery = new ImageDelivery(delivering(), () => (told += 1));
    expect(delivery.state).toMatchObject({ state: "downloading", done: 0 });
    delivery.start();
    await delivery.wait(new AbortController().signal);
    expect(delivery.state).toEqual({ state: "ready", folder: join(images(), KEY) });
    expect(told).toBeGreaterThan(1);
    expect(told).toBeLessThanOrEqual(102);
    expect(new ImageDelivery(delivering()).state).toEqual({ state: "ready", folder: join(images(), KEY) });
  });

  it("says it unpacks once a download is here, through the unpack, its hash and its syncs, rather than holding at its last percent", async () => {
    const states: string[] = [];
    const delivery: ImageDelivery = new ImageDelivery(delivering(), () => states.push(delivery.state.state));
    delivery.start();
    await delivery.wait(new AbortController().signal);
    // rootfs.img's unpack, vmlinuz's download, then its unpack.
    expect(states.filter((state, n) => state !== states[n - 1])).toEqual(["downloading", "unpacking", "downloading", "unpacking", "ready"]);
  });

  it("checks the image's files by their hashes again when asked, as after a boot of it did not start, and downloads it again once one differs", async () => {
    const delivery = new ImageDelivery(delivering());
    delivery.start();
    await delivery.wait(new AbortController().signal);
    heard = [];
    delivery.start(true);
    expect(delivery.state).toEqual({ state: "checking" });
    await delivery.wait(new AbortController().signal);
    expect(delivery.state).toEqual({ state: "ready", folder: delivery.folder });
    expect(heard).toEqual([]);
    // A bit of the kernel flipped, its size the same: whole by its sizes, not by its hashes.
    const flipped = Buffer.from(kernel);
    flipped[100]! ^= 1;
    writeFileSync(join(delivery.folder, "vmlinuz"), flipped);
    expect(new ImageDelivery(delivering()).state.state).toBe("ready");
    delivery.start(true);
    await delivery.wait(new AbortController().signal);
    expect(delivery.state.state).toBe("ready");
    expect(readFileSync(join(delivery.folder, "vmlinuz")).equals(kernel)).toBe(true);
    expect(heard.map(({ url }) => url).sort()).toEqual([`/desktop/vm/${KEY}/rootfs.img.zst`, `/desktop/vm/${KEY}/vmlinuz.zst`]);
  });

  // Each of the agent's operations waits on it before it goes to the VM.
  it("answers each wait at once once the image is here, with no fresh look on disk", async () => {
    const delivery = new ImageDelivery(delivering());
    delivery.start();
    await delivery.wait(new AbortController().signal);
    heard = [];
    // Gone from under it: a fresh look would find no image.
    rmSync(images(), { recursive: true, force: true });
    let answered = 0;
    for (let n = 0; n < 3; n += 1) {
      void delivery.wait(new AbortController().signal).then(() => {
        answered += 1;
      });
    }
    await new Promise((resolve) => setImmediate(resolve));
    expect(answered).toBe(3);
    expect(delivery.state).toEqual({ state: "ready", folder: delivery.folder });
    expect(heard).toEqual([]);
  });

  it("keeps only the image of this version once it has booted", () => {
    for (const name of [KEY, "b".repeat(64), `${"c".repeat(64)}.partial`]) mkdirSync(join(images(), name), { recursive: true });
    new ImageDelivery(delivering()).prune();
    expect(readdirSync(images())).toEqual([KEY]);
  });

  it("keeps going past an older image it cannot remove, and throws nothing into the boot that let them go", () => {
    const stuck = join(images(), "b".repeat(64), "held");
    mkdirSync(stuck, { recursive: true });
    writeFileSync(join(stuck, "file"), "");
    chmodSync(stuck, 0o500);
    for (const name of [KEY, "c".repeat(64)]) mkdirSync(join(images(), name), { recursive: true });
    try {
      expect(() => new ImageDelivery(delivering()).prune()).not.toThrow();
      expect(readdirSync(images()).sort()).toEqual([KEY, "b".repeat(64)]);
    } finally {
      chmodSync(stuck, 0o700);
    }
  });

  it("fails at once without an install record to download from, and reads it again at the next start", async () => {
    let record = (): string => {
      throw new Error("Surogate was not installed by its install script, so it does not know where to download its sandbox from");
    };
    const delivery = new ImageDelivery({ ...options(), base: () => record() });
    delivery.start();
    expect(delivery.state).toEqual({ state: "failed", why: "Surogate was not installed by its install script, so it does not know where to download its sandbox from" });
    await expect(delivery.wait(new AbortController().signal)).rejects.toThrow("was not installed by its install script");
    record = () => base;
    delivery.start();
    await delivery.wait(new AbortController().signal);
    expect(delivery.state).toEqual({ state: "ready", folder: delivery.folder });
  });

  it("answers what waits with why its download failed, and delivers once started again", async () => {
    served.clear();
    const delivery = new ImageDelivery(delivering());
    delivery.start();
    await expect(delivery.wait(new AbortController().signal)).rejects.toThrow("answered 404 for rootfs.img.zst");
    expect(delivery.state).toMatchObject({ state: "failed" });
    // Asked while it has failed: at once.
    await expect(delivery.wait(new AbortController().signal)).rejects.toThrow("answered 404");
    manifest.files.forEach((file) => publish(file.name, file.name === "vmlinuz" ? kernel : rootfs));
    delivery.start();
    expect(delivery.state).toMatchObject({ state: "downloading" });
    await delivery.wait(new AbortController().signal);
    expect(delivery.state.state).toBe("ready");
  });

  it("stops waiting at the signal, and leaves the download running", async () => {
    let release = () => {};
    answer = (request, response, body) => {
      release = () => ranged(request, response, body);
    };
    const delivery = new ImageDelivery(delivering());
    delivery.start();
    const cancel = new AbortController();
    const waiting = delivery.wait(cancel.signal);
    cancel.abort(new Error("cancelled"));
    await expect(waiting).rejects.toThrow("cancelled");
    expect(delivery.state.state).toBe("downloading");
    await expect.poll(() => heard.length).toBe(1);
    release();
    answer = ranged;
    await delivery.wait(new AbortController().signal);
    expect(delivery.state.state).toBe("ready");
  });
});
