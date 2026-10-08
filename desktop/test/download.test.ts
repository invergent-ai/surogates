// A download's bounds, whatever the fetch it is given does: a fetch that answers nothing, a body
// that stops coming, and one that keeps coming for longer than a bound. The image's delivery and
// the update each test the rest of it through their own downloads.

import { createHash, randomBytes } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { type Download, download, type Fetch } from "../src/download.js";

let dir: string;
let partial: string;
const body = Buffer.concat([Buffer.from([0x1f, 0x8b]), randomBytes(80_000)]);
const file: Download = { url: "http://base.invalid/release.tar.gz", name: "the release", size: body.length, sha256: createHash("sha256").update(body).digest("hex"), magic: Buffer.from([0x1f, 0x8b]) };

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "download-"));
  partial = join(dir, "release.tar.gz.partial");
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

// A body of *parts*, one every *everyMs*; and, with *then*, what follows the last: nothing more, for ever.
function answered(parts: Buffer[], everyMs: number, then: "ends" | "hangs" = "ends"): Response {
  let at = 0;
  return new Response(new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (at === parts.length) return then === "ends" ? controller.close() : new Promise<void>(() => {});
      await new Promise((resolve) => setTimeout(resolve, everyMs));
      controller.enqueue(parts[at++]!);
    },
    // A body that does not answer its cancel, as one held by a proxy.
    cancel: () => new Promise<void>(() => {}),
  }), { status: 200 });
}
const parts = (count: number) => Array.from({ length: count }, (_none, index) => body.subarray(Math.floor((index * body.length) / count), Math.floor(((index + 1) * body.length) / count)));

describe("a download", () => {
  it("stops when its fetch answers nothing within the headers' bound, though the fetch takes no notice of its signal", { timeout: 5_000 }, async () => {
    const deaf: Fetch = () => new Promise<Response>(() => {});
    await expect(download(file, partial, { fetch: deaf, headersMs: 200, stallMs: 5_000 })).rejects.toThrow("the download of the release stopped: nothing came for 0.2 s");
  });

  it("stops when its body brings nothing more within its bound, though the body takes no notice of its signal, and keeps what came", { timeout: 5_000 }, async () => {
    const stalls: Fetch = () => Promise.resolve(answered(parts(4).slice(0, 1), 0, "hangs"));
    await expect(download(file, partial, { fetch: stalls, headersMs: 5_000, stallMs: 200 })).rejects.toThrow("the download of the release stopped: nothing came for 0.2 s");
    expect(statSync(partial).size).toBe(parts(4)[0]!.length);
  });

  it("goes on for as long as bytes keep coming: its bound is of the time nothing comes, not of the download", async () => {
    // Eight parts, 150 ms apart: 1.2 s in all, twice the bound.
    const slow: Fetch = () => Promise.resolve(answered(parts(8), 150));
    const told: number[] = [];
    await download(file, partial, { fetch: slow, headersMs: 5_000, stallMs: 600 }, (have) => told.push(have));
    expect(readFileSync(partial).equals(body)).toBe(true);
    expect(told.at(-1)).toBe(body.length);
  });
});
