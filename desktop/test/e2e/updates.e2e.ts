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

import { connect, FakeAgent, quitHeld, signedInAndAdded, webClient } from "./fake-agent.js";
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
  // what it was given in <home>/applied, then the exit code and the words <home>/answer holds,
  // once <home>/hold is no longer there.
  writeFileSync(join(home, "surogate-apply-update"), [
    "#!/usr/bin/env bash", "CHANNEL=stable", "RELEASE_KEYS=(", `    '${PUBLIC}'`, "  )", `printf '%s\\n' "$@" >${join(home, "applied")}`, `ls -l /proc/$$/fd >${join(home, "held")}`, `read -r code words <${join(home, "answer")}`,
    `[ -z "$words" ] || echo "Surogate Desktop: $words" >&2`, `while [ -e ${join(home, "hold")} ]; do sleep 0.1; done`, `exit "$code"`, "",
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

  it("tries again after a minute a check that failed at its start, where the base had no release yet, and finds the one published since", { timeout: 180_000 }, async () => {
    const page = await launched();
    await new Promise((resolve) => setTimeout(resolve, 3_000));
    expect(await page.textContent("#update-text")).toBe("");
    publish("0.0.1");
    await expect.poll(() => page.textContent("#update-text"), { timeout: 100_000, interval: 1_000 }).toBe("Update available: Surogate 0.0.1");
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
    // It held its three standard descriptors, the script bash reads it from, and nothing else: none
    // of what the app's main process has open, which pkexec would hold for as long as its prompt.
    const held = readFileSync(join(home, "held"), "utf8").split("\n").flatMap((line) => / (\d+) -> (.*)$/.exec(line)?.slice(1, 3).join(" ") ?? []).map((line) => line.replace(/^(\d+) (socket|pipe):.*$/, "$1 $2"));
    expect(held.sort()).toEqual(["0 /dev/null", "1 /dev/null", "2 socket", `255 ${join(home, "surogate-apply-update")}`]);
    // Started again as Start at login starts a development build: its Electron on this main, the update's.
    await expect.poll(() => relaunched().filter(({ pid, argv }) => pid !== first && !argv.some((arg) => arg.startsWith("--type=")))
      .map(({ argv }) => argv), { timeout: 30_000 }).toEqual([[ELECTRON, MAIN]]);
  });

  it("stays a quit where its user asked for one while the helper ran: the helper ends 0, and the app does not start again by itself", async () => {
    publish("0.0.1");
    writeFileSync(join(home, "hold"), "");
    const page = await launched();
    await expect.poll(() => page.locator("#update-button").textContent({ timeout: 1_000 }).catch(() => null), { timeout: 30_000 }).toBe("Restart to update");
    await page.click("#update-button");
    await expect.poll(() => page.textContent("#update-text"), { timeout: 10_000 }).toBe("Installing Surogate 0.0.1\u2026");
    // The user quits, and the quit is under way, stopping what it stops, when the helper ends.
    const closed = app!.waitForEvent("close", { timeout: 30_000 });
    const release = await quitHeld(app!, page, agent);
    rmSync(join(home, "hold"));
    await expect.poll(() => page.textContent("#update-text"), { timeout: 10_000 }).toBe("Surogate 0.0.1 is installed.");
    release();
    await closed;
    app = undefined;
    // The update is installed, and the app is quit: the next start is the user's own.
    await new Promise((resolve) => setTimeout(resolve, 3_000));
    expect(relaunched().filter(({ argv }) => !argv.some((arg) => arg.startsWith("--type=")))).toEqual([]);
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

  it("keeps a failure's reason inside the sidebar, whatever its helper said: a line that names a long path wraps, and the user's row stays in the window", async () => {
    publish("0.0.1");
    // One unbroken word of 3000 characters, as a path with no space in it is.
    writeFileSync(join(home, "answer"), `1 /${"folder/".repeat(428)}x\n`);
    const page = await launched();
    await expect.poll(() => page.locator("#update-button").textContent({ timeout: 1_000 }).catch(() => null), { timeout: 30_000 }).toBe("Restart to update");
    await page.click("#update-button");
    await expect.poll(() => page.textContent("#update-button"), { timeout: 10_000 }).toBe("Try again");
    const text = (await page.textContent("#update-text"))!;
    expect(text.startsWith("Surogate could not install its update: /folder/folder/")).toBe(true);
    expect(text.length).toBe("Surogate could not install its update: ".length + 240);
    // Drawn inside the sidebar: the line is no wider than its own box, which is inside the sidebar,
    // and the sidebar's last row is still in the window.
    const drawn = await page.evaluate(() => {
      const line = document.querySelector("#update")!;
      const box = line.getBoundingClientRect();
      const words = document.querySelector("#update-text")!.getBoundingClientRect();
      const user = document.querySelector("#user")!.getBoundingClientRect();
      return {
        wider: line.scrollWidth > line.clientWidth || words.right > box.right, inside: box.right <= document.querySelector("#sidebar")!.getBoundingClientRect().right,
        height: box.height, user: user.bottom <= window.innerHeight,
      };
    });
    expect(drawn).toMatchObject({ wider: false, inside: true, user: true });
    expect(drawn.height).toBeLessThan(260);
  });

  it("says to run the install script again where its helper is not one it can take, and offers nothing", async () => {
    publish("0.0.1");
    // A helper that lists no release key: no release is one it would install.
    writeFileSync(join(home, "surogate-apply-update"), ["#!/usr/bin/env bash", "CHANNEL=stable", "RELEASE_KEYS=(", "  )", ""].join("\n"), { mode: 0o755 });
    const page = await launched();
    await expect.poll(() => page.locator("#update-text").textContent({ timeout: 1_000 }).catch(() => null), { timeout: 30_000 }).toBe("Surogate cannot update itself. Run the install script again.");
    await expect.poll(() => page.isVisible("#update"), { timeout: 10_000 }).toBe(true);
    expect(await page.isVisible("#update-button")).toBe(false);
    expect(existsSync(join(home, "k", "surogate"))).toBe(false);
  });

  it("keeps running when no administrator approves, and says so; and says why when the helper fails, with Try again", async () => {
    publish("0.0.1");
    writeFileSync(join(home, "answer"), "126\n");
    const page = await launched();
    // The app's log: what it writes for whoever looks into a failure.
    let logged = "";
    app!.process().stderr?.on("data", (chunk: Buffer) => {
      logged += chunk.toString();
    });
    await expect.poll(() => page.locator("#update-button").textContent({ timeout: 1_000 }).catch(() => null), { timeout: 30_000 }).toBe("Restart to update");
    // The line is a live region: drawn again with the same words, it is not written again, and so not said again.
    await page.evaluate(() => {
      const counted = window as unknown as { written: number };
      counted.written = 0;
      new MutationObserver((changes) => (counted.written += changes.length)).observe(document.getElementById("update-text")!, { childList: true, characterData: true, subtree: true });
    });
    for (let drawn = 0; drawn < 10; drawn += 1) await app!.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().forEach((window) => window.webContents.send("shell:changed")));
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(await page.evaluate(() => (window as unknown as { written: number }).written)).toBe(0);
    await page.click("#update-button");
    await expect.poll(() => page.textContent("#update-text"), { timeout: 10_000 }).toBe("An administrator needs to install this update.");
    await expect.poll(() => page.textContent("#update-button"), { timeout: 10_000 }).toBe("Try again");
    // The button its user pressed still has the keyboard, through the line that had none.
    expect(await page.evaluate(() => document.activeElement?.id)).toBe("update-button");
    await expect.poll(() => logged, { timeout: 10_000 }).toContain("Surogate 0.0.1 was not installed (exit 126)");
    writeFileSync(join(home, "answer"), "1 the release's archive could not be unpacked\n");
    await page.click("#update-button");
    await expect.poll(() => page.textContent("#update-text"), { timeout: 10_000 }).toBe("Surogate could not install its update: the release's archive could not be unpacked");
    await expect.poll(() => page.textContent("#update-button"), { timeout: 10_000 }).toBe("Try again");
    // All that the helper said is in the log, as it said it.
    await expect.poll(() => logged, { timeout: 10_000 }).toContain("Surogate 0.0.1 was not installed (exit 1): Surogate Desktop: the release's archive could not be unpacked");
    // Still the running app, its window up.
    await expect.poll(() => app!.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().some((window) => window.isVisible())), { timeout: 10_000 }).toBe(true);
  });
});
