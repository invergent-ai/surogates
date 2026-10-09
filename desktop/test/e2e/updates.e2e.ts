// Updates inside the app, through the real app: a development build given an install record
// and a root helper of the test's own (SUROGATE_INSTALL_JSON, SUROGATE_UPDATE_HELPER), as an
// installed app has /etc/surogate/install.json and its version's bin/surogate-apply-update. The
// base is a local HTTP server; the release key is the test's own.

import { createHash, generateKeyPairSync, sign } from "node:crypto";
import { existsSync, readdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, ELECTRON, launch, MAIN, quit, shellPage, stubNative } from "./launch.js";

const sha256 = (data: Buffer) => createHash("sha256").update(data).digest("hex");
const keys = generateKeyPairSync("ed25519");
const PUBLIC = keys.publicKey.export({ type: "spki", format: "pem" }).toString().trim();

let home: string;
let server: Server;
let base: string;
let served: Map<string, Buffer>;
let app: ElectronApplication | undefined;
let agent: FakeAgent;
let origin: string;

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
  // The test's root helper: its release keys, listed as install.sh lists them; and, run with --apply,
  // what it was given in <home>/applied, then the exit code and the words <home>/answer holds.
  writeFileSync(join(home, "surogate-apply-update"), [
    "#!/usr/bin/env bash", "CHANNEL=stable", "RELEASE_KEYS=(", `    '${PUBLIC}'`, "  )", `printf '%s\\n' "$@" >${join(home, "applied")}`, `read -r code words <${join(home, "answer")}`,
    `[ -z "$words" ] || echo "Surogate Desktop: $words" >&2`, `exit "$code"`, "",
  ].join("\n"), { mode: 0o755 });
  writeFileSync(join(home, "answer"), "0\n");
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  // An app the test's restart started, outside Playwright, goes with the test.
  for (const { pid } of relaunched()) {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // gone meanwhile
    }
  }
  await expect.poll(() => relaunched(), { timeout: 10_000 }).toEqual([]);
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

// The app a restart started, outside Playwright: its main, this package's Electron on this main and
// nothing more, as Playwright never starts it; and Chromium's processes for the test's data home.
// Chromium writes its command line back joined by spaces.
function relaunched(): Array<{ pid: number; argv: string[] }> {
  return readdirSync("/proc").filter((entry) => /^\d+$/.test(entry)).flatMap((entry) => {
    try {
      const argv = readFileSync(`/proc/${entry}/cmdline`, "utf8").split(/[\0 ]/).filter(Boolean);
      const main = argv.length === 2 && argv[0] === ELECTRON && argv[1] === MAIN;
      return main || argv.includes(`--user-data-dir=${join(home, "surogate", "electron")}`) ? [{ pid: Number(entry), argv }] : [];
    } catch {
      return []; // gone meanwhile
    }
  });
}

// The app launched with the test's install record and helper, signed in to the fake agent: the sidebar,
// where the update's line is, shows once an agent is added and signed in to. The window's page.
async function launched(): Promise<Page> {
  origin = await agent.start();
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

  it("installs the update by its root helper at Restart to update, then quits as the app quits and starts again from its launcher", async () => {
    publish("0.0.1");
    const page = await launched();
    await expect.poll(() => page.locator("#update-button").textContent({ timeout: 1_000 }).catch(() => null), { timeout: 30_000 }).toBe("Restart to update");
    const first = app!.process().pid;
    const closed = app!.waitForEvent("close", { timeout: 30_000 });
    // The app goes while the click is still answered.
    await page.click("#update-button").catch(() => {});
    await closed;
    app = undefined;
    // The helper was handed the files as the user downloaded them.
    const updates = join(home, "k", "surogate", "updates", "0.0.1");
    expect(readFileSync(join(home, "applied"), "utf8")).toBe(["--apply", join(updates, "manifest.json"), join(updates, "manifest.json.sig"), join(updates, "release.tar.gz"), ""].join("\n"));
    // Started again as Start at login starts a development build: its Electron on this main, the update's.
    await expect.poll(() => relaunched().filter(({ pid, argv }) => pid !== first && !argv.some((arg) => arg.startsWith("--type=")))
      .map(({ argv }) => argv), { timeout: 30_000 }).toEqual([[ELECTRON, MAIN]]);
  });

  it("starts nothing again at a quit that no update asked for, with one downloaded and waiting", async () => {
    publish("0.0.1");
    const page = await launched();
    await expect.poll(() => page.locator("#update-button").textContent({ timeout: 1_000 }).catch(() => null), { timeout: 30_000 }).toBe("Restart to update");
    const closed = app!.waitForEvent("close", { timeout: 30_000 });
    await app!.evaluate(({ app: electron }) => electron.quit());
    await closed;
    app = undefined;
    // A restart's app is there within a second of the quit: three seconds on, none is, and nothing was installed.
    await new Promise((resolve) => setTimeout(resolve, 3_000));
    expect(relaunched().filter(({ argv }) => !argv.some((arg) => arg.startsWith("--type=")))).toEqual([]);
    expect(existsSync(join(home, "applied"))).toBe(false);
  });

  it("takes Restart to update from the window's own page alone: the agent's web client has no such call, and no one answers Settings'", async () => {
    publish("0.0.1");
    const page = await launched();
    await expect.poll(() => page.locator("#update-button").textContent({ timeout: 1_000 }).catch(() => null), { timeout: 30_000 }).toBe("Restart to update");
    // The agent's own page, in the window's view of it: none of the shell's calls is there.
    const client = await webClient(app!, origin);
    expect(await client.evaluate(() => "surogateShell" in window)).toBe(false);
    // Settings is a page of the app's own, with the same preload: the call is there, and its channel is not.
    await page.evaluate(() => (window as unknown as { surogateShell: { settings(): Promise<void> } }).surogateShell.settings());
    await expect.poll(() => app!.windows().some((found) => found.url().endsWith("/settings.html")), { timeout: 10_000 }).toBe(true);
    const settings = app!.windows().find((found) => found.url().endsWith("/settings.html"))!;
    await settings.waitForLoadState();
    const answered = await settings.evaluate(() => (window as unknown as { surogateShell: { update(): Promise<void> } }).surogateShell.update()
      .then(() => "answered", (error: Error) => error.message));
    expect(answered).toContain("No handler registered for 'shell:update'");
    // Nothing was run, and the offer is as it was.
    expect(existsSync(join(home, "applied"))).toBe(false);
    await expect.poll(() => page.textContent("#update-button"), { timeout: 10_000 }).toBe("Restart to update");
  });

  it("runs no helper at a click once a downloaded file is no longer its own: it downloads the release again, and offers it for another click", async () => {
    const tarball = publish("0.0.1");
    const page = await launched();
    await expect.poll(() => page.locator("#update-button").textContent({ timeout: 1_000 }).catch(() => null), { timeout: 30_000 }).toBe("Restart to update");
    // As a program of the user's could leave it, long after the check: a link where the tarball was.
    const updates = join(home, "k", "surogate", "updates", "0.0.1");
    writeFileSync(join(home, "elsewhere"), tarball);
    rmSync(join(updates, "release.tar.gz"));
    symlinkSync(join(home, "elsewhere"), join(updates, "release.tar.gz"));
    await page.click("#update-button");
    // The release is here again, in a file of the app's own, and nothing was run on the link.
    await expect.poll(() => (existsSync(join(updates, "release.tar.gz")) ? readFileSync(join(updates, "release.tar.gz")).equals(tarball) : false), { timeout: 10_000 }).toBe(true);
    await expect.poll(() => page.textContent("#update-button"), { timeout: 10_000 }).toBe("Restart to update");
    expect(existsSync(join(home, "applied"))).toBe(false);
    // Still the running app, its window up.
    await expect.poll(() => app!.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().some((window) => window.isVisible())), { timeout: 10_000 }).toBe(true);
  });

  it("keeps running when no administrator approves, and says so; and says why when the helper fails, with Try again", async () => {
    publish("0.0.1");
    writeFileSync(join(home, "answer"), "126\n");
    const page = await launched();
    await expect.poll(() => page.locator("#update-button").textContent({ timeout: 1_000 }).catch(() => null), { timeout: 30_000 }).toBe("Restart to update");
    await page.click("#update-button");
    await expect.poll(() => page.textContent("#update-text"), { timeout: 10_000 }).toBe("An administrator needs to install this update.");
    await expect.poll(() => page.textContent("#update-button"), { timeout: 10_000 }).toBe("Try again");
    writeFileSync(join(home, "answer"), "1 the release's archive could not be unpacked\n");
    await page.click("#update-button");
    await expect.poll(() => page.textContent("#update-text"), { timeout: 10_000 }).toBe("Surogate could not install its update: the release's archive could not be unpacked");
    await expect.poll(() => page.textContent("#update-button"), { timeout: 10_000 }).toBe("Try again");
    // Still the running app, its window up.
    await expect.poll(() => app!.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().some((window) => window.isVisible())), { timeout: 10_000 }).toBe(true);
  });
});
