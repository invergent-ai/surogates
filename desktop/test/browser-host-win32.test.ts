// The browser host with the person's own browser on Windows: Chrome or Edge where their installers put them, headed,
// as the Windows runner has them, or the one SUROGATE_TEST_BROWSER names. Behind SUROGATE_BROWSER_TESTS_WIN=1, which
// the Windows workflow sets, and on a CI runner alone: the browser seals its secrets with the DPAPI key of the account
// it runs in, and a test run by hand would reach a person's own. Its processes are read through surogate-helper.exe
// procs, as the app reads them.

import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { createServer, type Server } from "node:http";
import { connect as connectTcp } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { BrowserContext, Page } from "playwright-core";
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { type Launch, PAUSED } from "../src/browser/client.js";
import type { StagedDownload } from "../src/browser/downloads.js";
import { BrowserHost, type BrowserHostOptions, FILE_ASKED, NOT_ASKED, WEAKENING } from "../src/browser/host.js";
import { HELPER } from "../src/native-helper.js";

const EXECUTABLE = process.env.SUROGATE_TEST_BROWSER || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const run = process.platform === "win32" && process.env.SUROGATE_BROWSER_TESTS_WIN === "1";
const ROOT = "root-1";

// The fixture, as sites past this computer: fixture.test leads to 203.0.113.10, which the proxy dials at the fixture's port here.
const SITE = "203.0.113.10";
const PAGE = `<!doctype html><title>Fixture</title>
<input id="name" style="position:absolute;left:40px;top:100px;width:200px;height:24px">
<a id="pop" href="/second" target="_blank" style="position:absolute;left:40px;top:150px">Open</a>
<input id="file" type="file" style="position:absolute;left:40px;top:200px">
<a id="dl" href="/report.txt" download style="position:absolute;left:40px;top:250px">Download</a>
<div style="height:4000px"></div>`;

let site: Server;
let port: number;
let profile: string;
let launch: Launch;
let host: BrowserHost;
let next = 0;

const hostWith = (options: Omit<BrowserHostOptions, "proxy"> = {}) => new BrowserHost({
  proxy: {
    resolve: (name) => (name === "fixture.test" ? Promise.resolve([SITE]) : Promise.reject(new Error("ENOTFOUND"))),
    local: () => ["127.0.0.1", "::1"],
    subnets: () => [],
    connect: (address, at) => connectTcp({ host: "127.0.0.1", port: address === SITE && at === 80 ? port : 9 }),
  },
  ...options,
});

const op = (session: string, kind: string, args: Record<string, unknown> = {}, root = ROOT) =>
  host.perform(launch, root, session, kind, args, new AbortController().signal) as Promise<{ ok?: any; error?: { type: string; message: string } }>;
const script = async (session: string, code: string, root = ROOT) => (await op(session, "browser.evaluate", { code }, root)).ok?.value;
const session = () => `session-${(next += 1)}`;
const pages = async () => (await (host as unknown as { running: Promise<BrowserContext> }).running).pages().length;
const tabs = () => (host as unknown as { tabs: Map<string, Page[]> }).tabs;

// The browser's processes for this profile, each with its arguments, as surogate-helper lists them.
function processes(): Array<{ pid: number; args: string[] }> {
  const listed = spawnSync(HELPER, ["procs"], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 }).stdout.trim().split("\n");
  return listed.map((line) => JSON.parse(line) as { pid: number; args: string[] }).filter(({ args }) => args.some((arg) => arg.includes(profile)));
}

describe.runIf(run)("the browser host on Windows", () => {
  beforeAll(() => {
    if (process.env.CI !== "true") throw new Error("The Windows browser tests run on a CI runner alone: the browser seals with the DPAPI key of the account it runs in");
    if (!existsSync(EXECUTABLE)) throw new Error(`No browser at ${EXECUTABLE}`);
  });

  beforeEach(async () => {
    site = createServer((req, res) => {
      if (req.url === "/report.txt") return void res.writeHead(200, { "content-type": "text/plain", "content-disposition": "attachment" }).end("report");
      if (req.url === "/second") return void res.writeHead(200, { "content-type": "text/html" }).end("<title>Second</title>");
      res.writeHead(200, { "content-type": "text/html" }).end(PAGE);
    });
    await new Promise<void>((done) => site.listen(0, "127.0.0.1", () => done()));
    port = (site.address() as { port: number }).port;
    // By its long names: a runner's temporary folder is named by a short one (RUNNER~1), which the browser's own
    // processes would not name as the host does.
    profile = mkdtempSync(join(realpathSync.native(tmpdir()), "sb-profile-"));
    launch = { executable: EXECUTABLE, profile };
    host = hostWith();
  });

  afterEach(async () => {
    await host.close();
    await new Promise<void>((done) => site.close(() => done()));
    rmSync(profile, { recursive: true, force: true });
  });

  it("launches the person's browser over a pipe, its sandbox on, without Playwright's weakening defaults, every request through its signed-in proxy", async () => {
    const a = session();
    expect(await op(a, "browser.navigate", { url: "http://fixture.test/", wait_until: "load" })).toEqual({
      ok: { url: "http://fixture.test/", title: "Fixture", opened: true, notices: [] },
    });
    const main = processes().find(({ args }) => !args.some((arg) => arg.startsWith("--type=")) && args.includes("--remote-debugging-pipe"));
    expect(main).toBeDefined();
    const args = main!.args;
    expect(args.some((arg) => arg.startsWith("--remote-debugging-port"))).toBe(false);
    expect(existsSync(join(profile, "DevToolsActivePort"))).toBe(false);
    expect(processes().some(({ args: other }) => other.includes("--no-sandbox"))).toBe(false);
    for (const weak of WEAKENING) expect(args).not.toContain(weak);
    expect(args).toContain("--proxy-bypass-list=<-loopback>");
    expect(args.some((arg) => /^--proxy-server=http:\/\/127\.0\.0\.1:\d+$/.test(arg))).toBe(true);
    // The renderers run in Windows's sandbox, which no switch turns off: none was given.
    const renderers = processes().filter(({ args: other }) => other.includes("--type=renderer"));
    expect(renderers.length).toBeGreaterThan(0);
    for (const { args: other } of renderers) expect(other).not.toContain("--no-sandbox");
    const prefs = JSON.parse(readFileSync(join(profile, "Default", "Preferences"), "utf8"));
    expect(prefs.webrtc.ip_handling_policy).toBe("disable_non_proxied_udp");
  }, 60_000);

  it("takes a popup a session's tab opens as the session's, and its close closes both", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    await op(a, "browser.mouse", { action: "click", x: 50, y: 155, button: "left", clicks: 1 });
    await expect.poll(() => script(a, "return document.title;")).toBe("Second");
    expect(await pages()).toBe(2);
    expect(await op(a, "browser.close")).toEqual({ ok: { closed: true } });
  }, 60_000);

  it("opens no file dialog for a file input, tells the agent, and gives the input the files it is sent", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    const report = { name: "report.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7").toString("base64") };
    expect(await op(a, "browser.set_input_files", { files: [report] })).toEqual({ error: { type: "browser", message: NOT_ASKED } });
    expect((await op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 })).ok.notices).toEqual([FILE_ASKED]);
    expect(await op(a, "browser.set_input_files", { files: [report] })).toEqual({ ok: { files: 1, notices: [] } });
    expect(await script(a, `const [file] = document.getElementById("file").files;
return [file.name, file.type, await file.text()];`)).toEqual(["report.pdf", "application/pdf", "%PDF-1.7"]);
  }, 60_000);

  it("stages a download its page finished in its own temporary folder, and hands it on for the chat's folder", async () => {
    const staged: StagedDownload[] = [];
    host = hostWith({ downloaded: (download) => staged.push(download) });
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    await op(a, "browser.mouse", { action: "click", x: 60, y: 255, button: "left", clicks: 1 }, "chat-1");
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(1);
    expect(staged[0]).toMatchObject({ root: "chat-1", session: a, name: "report.txt", user: false });
    expect(await readFile(staged[0]!.path, "utf8")).toBe("report");
    expect(staged[0]!.path.startsWith(profile)).toBe(false);
    expect(readdirSync(profile).some((name) => name.includes("report"))).toBe(false);
  }, 60_000);

  it("answers the agent paused while its person holds the browser, and runs it again once they hand it back; their own keys land in the page", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    host.pause("chat-1", true);
    expect(await op(a, "browser.evaluate", { code: "return document.title;" }, "chat-1")).toEqual(PAUSED);
    await tabs().get(a)![0]!.fill("#name", "typed by hand");
    host.pause("chat-1", false);
    expect(await script(a, `return document.getElementById("name").value;`, "chat-1")).toBe("typed by hand");
  }, 60_000);

  // The app's own look at the profile's processes, through the helper as the app starts it.
  it("closes its browser only once nothing holds its profile, a process that would not go killed", async () => {
    await op(session(), "browser.navigate", { url: "http://fixture.test/" });
    const lingering = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)", join(profile, "lingering")], { stdio: "ignore" });
    const gone = new Promise<string | null>((done) => lingering.once("exit", (_code, signal) => done(signal)));
    try {
      await host.close();
      // Ended by another process, as TerminateProcess ends one: Windows says no signal of it.
      expect(await Promise.race([gone, new Promise((done) => setTimeout(done, 500, "late"))])).not.toBe("late");
      expect(processes()).toEqual([]);
    } finally {
      lingering.kill("SIGKILL");
    }
  }, 60_000);
});
