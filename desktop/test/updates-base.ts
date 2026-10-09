// A base an update's tests install from: a local HTTP server that serves what a release job
// publishes, signed with a release key of the tests' own, and an install record and a root helper
// that name it. One a test, in a folder of its own that goes after it.

import { createHash, generateKeyPairSync, type KeyObject, randomBytes, sign } from "node:crypto";
import { mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

import { afterEach, beforeEach } from "vitest";

import { Updates, type UpdatesOptions } from "../src/shell/updates.js";

export const sha256 = (data: Buffer) => createHash("sha256").update(data).digest("hex");
export const keys = generateKeyPairSync("ed25519");
export const next = generateKeyPairSync("ed25519");
export const pem = (key: KeyObject) => key.export({ type: "spki", format: "pem" }).toString().trim();
// The root helper: the install script, with *trusted* in its release keys' place, which installs *channel*'s releases.
const SCRIPT = fileURLToPath(new URL("../release/install.sh", import.meta.url));
export const helperWith = (trusted: KeyObject[], channel = "stable") => readFileSync(SCRIPT, "utf8")
  .replace(/RELEASE_KEYS=\(\n[^)]*\)/, `RELEASE_KEYS=(\n${trusted.map((key) => `    '${pem(key)}'`).join("\n")}\n  )`)
  .replace(/^  CHANNEL=stable$/m, `  CHANNEL=${channel}`);

export type Answer = (request: IncomingMessage, response: ServerResponse, body: Buffer) => void;

// The file, or the rest of it from where a Range asks.
export const ranged: Answer = (request, response, body) => {
  const from = Number(/^bytes=(\d+)-$/.exec(request.headers.range ?? "")?.[1] ?? 0);
  if (from > 0) response.writeHead(206, { "content-range": `bytes ${from}-${body.length - 1}/${body.length}`, "content-length": body.length - from });
  else response.writeHead(200, { "content-length": body.length });
  response.end(body.subarray(from));
};

export interface Base {
  dir: string; // the test's own folder, by its real path
  url: string;
  served: Map<string, Buffer>;
  heard: Array<{ url: string; range: string | undefined }>;
  answer: Answer;
  record: string;
  helper: string;
  /** <dir>/cache/surogate/updates: the cache home is <dir>/cache. */
  cache(): string;
  /** Where the base serves *version*'s tarball. */
  tarballAt(version: string): string;
  /** Where it serves the signature of *version*'s manifest: the one place a signature is. */
  signatureAt(version: string): string;
  /**
   * Release *version* on the base, as the release job publishes it: its tarball, and latest.json
   * signed by *key*, with *fields* in place of its own. The tarball.
   */
  publish(version: string, fields?: Record<string, unknown>, key?: KeyObject): Buffer;
  /** latest.json as *manifest*'s bytes, signed by *key* where the version it names says its signature is. */
  offer(manifest: Buffer, key?: KeyObject): void;
  /** An app at 1.2.3 that reads the test's record and helper, with *options* in place of its own. */
  updates(options?: Partial<UpdatesOptions>, changed?: () => void): Updates;
}

/** The base of the test that runs: made before each, and gone after it. */
export function servedBase(): Base {
  let server: Server;
  const base: Base = {
    dir: "", url: "", served: new Map(), heard: [], answer: ranged, record: "", helper: "",
    cache: () => join(base.dir, "cache", "surogate", "updates"),
    tarballAt: (version) => `/desktop/releases/${version}/surogate-desktop-${version}-linux-x64.tar.gz`,
    signatureAt: (version) => `/desktop/releases/${version}/manifest.json.sig`,
    publish(version, fields = {}, key = keys.privateKey) {
      const tarball = gzipSync(randomBytes(300_000));
      const url = `releases/${version}/surogate-desktop-${version}-linux-x64.tar.gz`;
      base.served.set(`/desktop/${url}`, tarball);
      base.offer(Buffer.from(`${JSON.stringify({
        version, channel: "stable", platform: "linux", arch: "x64", url, sha256: sha256(tarball), size: tarball.length, stateSchema: 1, ...fields,
      })}\n`), key);
      return tarball;
    },
    offer(manifest, key = keys.privateKey) {
      base.served.set("/desktop/latest.json", manifest);
      // Its signature is its release's own, where the version it names says: beside latest.json there is none.
      const version = /"version":"([0-9.]+)"/.exec(manifest.toString())?.[1];
      if (version) base.served.set(base.signatureAt(version), sign(null, manifest, key));
    },
    updates: (options = {}, changed = () => {}) => new Updates({
      version: "1.2.3", record: base.record, rootOwned: false, helper: base.helper, installed: null,
      cache: base.cache(), fetch: (url, init) => fetch(url, init), signal: new AbortController().signal, apply: () => Promise.resolve({ code: 0, said: "" }), ...options,
    }, changed),
  };
  beforeEach(async () => {
    base.dir = realpathSync(mkdtempSync(join(tmpdir(), "updates-")));
    base.served = new Map();
    base.heard = [];
    base.answer = ranged;
    server = createServer((request, response) => {
      base.heard.push({ url: request.url ?? "", range: request.headers.range });
      const body = base.served.get(request.url ?? "");
      if (!body) return void response.writeHead(404).end();
      base.answer(request, response, body);
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    base.url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    base.record = join(base.dir, "install.json");
    base.helper = join(base.dir, "surogate-apply-update");
    writeFileSync(base.record, JSON.stringify({ base: base.url, channel: "stable" }));
    writeFileSync(base.helper, helperWith([keys.publicKey]));
  });
  afterEach(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    rmSync(base.dir, { recursive: true, force: true });
  });
  return base;
}
