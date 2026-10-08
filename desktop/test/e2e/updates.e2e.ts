// Updates inside the app, through the real app: a development build given an install record
// and a root helper of the test's own (SUROGATE_INSTALL_JSON, SUROGATE_UPDATE_HELPER), as an
// installed app has /etc/surogate/install.json and its version's bin/surogate-apply-update. The
// base is a local HTTP server; the release key is the test's own.

import { createHash, generateKeyPairSync, sign } from "node:crypto";
import { readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connect, FakeAgent, signedInAndAdded } from "./fake-agent.js";
import { dataHome, launch, quit, shellPage, stubNative } from "./launch.js";

const sha256 = (data: Buffer) => createHash("sha256").update(data).digest("hex");
const keys = generateKeyPairSync("ed25519");
const PUBLIC = keys.publicKey.export({ type: "spki", format: "pem" }).toString().trim();

let home: string;
let server: Server;
let base: string;
let served: Map<string, Buffer>;
let app: ElectronApplication | undefined;
let agent: FakeAgent;

beforeEach(async () => {
  home = dataHome();
  agent = new FakeAgent();
  served = new Map();
  server = createServer((request, response) => {
    const body = served.get(request.url ?? "");
    if (!body) return void response.writeHead(404).end();
    response.writeHead(200, { "content-length": body.length }).end(body);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  writeFileSync(join(home, "install.json"), JSON.stringify({ base, channel: "stable" }));
  // The test's root helper: its channel and its release keys, written as install.sh writes them.
  writeFileSync(join(home, "surogate-apply-update"), ["#!/usr/bin/env bash", "CHANNEL=stable", "RELEASE_KEYS=(", `    '${PUBLIC}'`, "  )", ""].join("\n"), { mode: 0o755 });
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  await agent.stop();
  await agent.link.stop();
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
  rmSync(home, { recursive: true, force: true });
});

// Release *version* on the base, as the release job publishes it: its tarball, and latest.json signed with the test's key.
function publish(version: string): Buffer {
  const tarball = gzipSync(Buffer.from(`Surogate ${version}\n`.repeat(10_000)));
  const url = `releases/${version}/surogate-desktop-${version}-linux-x64.tar.gz`;
  const manifest = Buffer.from(`${JSON.stringify({
    version, channel: "stable", platform: "linux", arch: "x64", url, sha256: sha256(tarball), size: tarball.length, stateSchema: 1,
  })}\n`);
  served.set(`/desktop/${url}`, tarball);
  served.set("/desktop/latest.json", manifest);
  served.set("/desktop/latest.json.sig", sign(null, manifest, keys.privateKey));
  return tarball;
}

// The app launched with the test's install record and helper, signed in to the fake agent: the sidebar,
// where the update's line is, shows once an agent is added and signed in to. The window's page.
async function launched(): Promise<Page> {
  const origin = await agent.start();
  app = await launch(home, { SUROGATE_INSTALL_JSON: join(home, "install.json"), SUROGATE_UPDATE_HELPER: join(home, "surogate-apply-update") });
  await stubNative(app);
  const page = await shellPage(app);
  await connect(page, origin);
  await signedInAndAdded(app, page, agent);
  return page;
}

describe("updates, through the app", () => {
  it("finds a newer release where it was installed from at its start, downloads it into the user's cache, and says Update available", async () => {
    // The package's own version is 0.0.0.
    const tarball = publish("0.0.1");
    const page = await launched();
    await expect.poll(() => page.locator("#update-text").textContent({ timeout: 1_000 }).catch(() => null), { timeout: 30_000 }).toBe("Update available: Surogate 0.0.1");
    await expect.poll(() => page.isVisible("#update"), { timeout: 10_000 }).toBe(true);
    // The test's session puts XDG_CACHE_HOME at <home>/k.
    expect(readFileSync(join(home, "k", "surogate", "updates", "0.0.1", "release.tar.gz")).equals(tarball)).toBe(true);
  });
});
