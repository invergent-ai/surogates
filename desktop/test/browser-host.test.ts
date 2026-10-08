// The browser host with the user's own browser: Chrome or Edge from its .deb, headed on xvfb's
// display, or the one SUROGATE_TEST_BROWSER names. Behind SUROGATE_BROWSER_TESTS=1, as the VM tests
// are behind theirs, and skipped where neither browser is installed. Run apart from the user's
// session (test/isolated.sh), as
//   npm run test:browser -- test/browser-host.test.ts
// With the flag set anywhere else, they fail before any browser is launched.

import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, readlinkSync, rmSync, symlinkSync, truncateSync, writeFileSync } from "node:fs";
import { execFileSync, spawn } from "node:child_process";
import { getEventListeners } from "node:events";
import { readFile } from "node:fs/promises";
import { createServer, type Server } from "node:http";
import { connect as connectTcp } from "node:net";
import { tmpdir, userInfo } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import type { BrowserContext, Page } from "playwright-core";
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { PAUSED } from "../src/browser/client.js";
import { interrupted, LEFT_TO_USER, type StagedDownload, tooLarge } from "../src/browser/downloads.js";
import {
  AFTER_HAND_BACK_MS, ASKING, BrowserHost, type BrowserHostOptions, clearStaged, FILE_ASKED, holding, type Launch, notFinished, PROXY_BYPASSED, WEAKENING,
} from "../src/browser/host.js";
import { MAX_WRITE_BYTES } from "../src/files/answers.js";
import { isolated, notIsolated, TEST_BROWSER } from "./isolated.js";

const EXECUTABLE = TEST_BROWSER;
const run = EXECUTABLE !== undefined && process.env.SUROGATE_BROWSER_TESTS === "1";
const ROOT = "root-1";

// The fixture, as sites past this computer: fixture.test leads to 203.0.113.10 and other.test to
// 203.0.113.11, each of which the proxy dials at the fixture's own port here. Every other name is unknown.
const SITE = "203.0.113.10";
const OTHER = "203.0.113.11";

const PAGE = `<!doctype html><title>Fixture</title>
<button id="go" style="position:absolute;left:40px;top:40px;width:100px;height:30px" onclick="document.title='clicked '+(++window.n)">Go</button>
<input id="name" style="position:absolute;left:40px;top:100px;width:200px;height:24px">
<a id="pop" href="/second" target="_blank" style="position:absolute;left:40px;top:150px">Open</a>
<input id="file" type="file" style="position:absolute;left:40px;top:200px">
<a id="dl" href="/report.txt" download style="position:absolute;left:40px;top:250px">Download</a>
<iframe src="/inner" style="position:absolute;left:300px;top:40px;width:200px;height:100px"></iframe>
<div style="height:4000px"></div>
<script>window.n = 0;</script>`;

let site: Server;
let canary: Server;
let ports: { site: number; canary: number };
let hits: string[];
// What fixture.test's cross-site frame asked the site for.
let framed: string[];
let profile: string;
let launch: Launch;
let host: BrowserHost;
let next = 0;

beforeEach(async () => {
  hits = [];
  framed = [];
  site = createServer((req, res) => {
    // other.test's page, which embeds fixture.test's frame: the frame registers a worker of its own, or loads and navigates itself once.
    if (req.headers.host === "other.test") {
      return void res.writeHead(200, { "content-type": "text/html" }).end(`<title>Embed</title>
<iframe src="http://fixture.test/frame${req.url?.includes("register") ? "?register" : ""}"></iframe>
<script>addEventListener("message", (event) => { document.title = String(event.data); });</script>`);
    }
    if (req.url?.startsWith("/frame")) {
      framed.push(req.url);
      return void res.writeHead(200, { "content-type": "text/html" }).end(`<script>
const at = location.pathname + location.search;
if (at.includes("register")) {
  ServiceWorkerContainer.prototype.register.call(navigator.serviceWorker, "/sw-of-frame.js").then(() => navigator.serviceWorker.ready)
    .then(() => parent.postMessage("registered " + at, "*"), (error) => parent.postMessage("failed " + error, "*"));
} else if (at.includes("again")) {
  parent.postMessage("frame again " + at, "*");
} else {
  setTimeout(() => { location.href = "/frame?again"; }, 300);
}
</script>`);
    }
    // The frame's worker, which would answer every request of the frame's (partitioned) origin.
    if (req.url === "/sw-of-frame.js") {
      return void res.writeHead(200, { "content-type": "text/javascript" })
        .end(`self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => event.respondWith(new Response("<script>parent.postMessage('served by the worker ' + location.pathname + location.search, '*')</script>", { headers: { "content-type": "text/html" } })));`);
    }
    if (req.url === "/inner") return void res.writeHead(200, { "content-type": "text/html" }).end(`<a href="/x">Inner link</a>`);
    if (req.url === "/report.txt") return void res.writeHead(200, { "content-type": "text/plain", "content-disposition": "attachment" }).end("report");
    // A download whose first part comes at once and whose rest a moment later: it is on its way meanwhile. Not
    // text, and its first part more than the browser reads to tell what a file is: the browser takes a short
    // text file for a download only once its end has come.
    if (req.url === "/slow.bin") {
      res.writeHead(200, { "content-type": "application/octet-stream", "content-disposition": "attachment" });
      res.write("s".repeat(4_096));
      return void setTimeout(() => res.end("report"), 1_500);
    }
    // A download its site answers a moment after it is asked for: until then the browser has announced none. Asked
    // for outright, at the end of two redirects, and by a form.
    if (req.url === "/late.bin" || req.url === "/post") {
      const name = req.url === "/post" ? "posted.bin" : "late.bin";
      return void setTimeout(() => res.writeHead(200, { "content-type": "application/octet-stream", "content-disposition": `attachment; filename=${name}` }).end("late"), 1_500);
    }
    // The first redirect comes a moment after it is asked for: the requests after it begin later than the first.
    if (req.url === "/hop1") return void setTimeout(() => res.writeHead(302, { location: "/hop2" }).end(), 700);
    if (req.url === "/hop2") return void res.writeHead(302, { location: "/late.bin" }).end();
    // One that breaks off part-way: less than it said it had, and its connection gone.
    if (req.url === "/broken.txt") {
      res.writeHead(200, { "content-type": "text/plain", "content-disposition": "attachment", "content-length": "1000" });
      res.write("part");
      return void setTimeout(() => res.destroy(), 200);
    }
    if (req.url === "/second") return void res.writeHead(200, { "content-type": "text/html" }).end("<title>Second</title>");
    // A page that answers a moment late: a navigation to it is in flight meanwhile.
    if (req.url === "/slow") return void setTimeout(() => res.writeHead(200, { "content-type": "text/html" }).end("<title>Slow</title>"), 1_500);
    // A page whose first part comes at once and whose rest, with its script, comes a moment later: it is on its way meanwhile.
    if (req.url === "/long") {
      res.writeHead(200, { "content-type": "text/html" });
      res.write(`<!doctype html><title>Long</title><p id="first">first</p>${"<!-- padding -->".repeat(200)}`);
      return void setTimeout(() => res.end(`<p id="last">last</p><script>window.finished = true;</script>`), 1_500);
    }
    // The fixture's page under a title of the test's own: the browser's window is named after the tab in front.
    if (req.url?.startsWith("/t/")) return void res.writeHead(200, { "content-type": "text/html" }).end(PAGE.replace("<title>Fixture</title>", `<title>${req.url.slice(3)}</title>`));
    // A page whose outline is larger than the link carries: ten thousand buttons with long names.
    if (req.url === "/huge") {
      const button = `<button>${"a long name ".repeat(20)}</button>`;
      return void res.writeHead(200, { "content-type": "text/html" }).end(`<title>Huge</title>${button.repeat(10_000)}`);
    }
    // A page that sends itself on to another site once it has loaded.
    if (req.url === "/moves") {
      return void res.writeHead(200, { "content-type": "text/html" }).end(`<script>addEventListener("load", () => { location.href = "http://other.test/"; });</script>`);
    }
    // A service worker that would answer every request of its origin's pages.
    if (req.url === "/sw.js") {
      return void res.writeHead(200, { "content-type": "text/javascript" })
        .end(`self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => event.respondWith(new Response("<title>Served by the worker</title>", { headers: { "content-type": "text/html" } })));`);
    }
    // A page whose own code holds its main thread once it has loaded.
    if (req.url === "/hang") {
      return void res.writeHead(200, { "content-type": "text/html" })
        .end(`<title>Hang</title><script>addEventListener("load", () => setTimeout(() => { while (true) {} }, 0));</script>`);
    }
    // One whose own code holds its main thread a moment after it loaded.
    if (req.url === "/late-hang") {
      return void res.writeHead(200, { "content-type": "text/html" })
        .end(`<title>LateHang</title><script>addEventListener("load", () => setTimeout(() => { while (true) {} }, 1500));</script>`);
    }
    if (req.url === "/redirect") return void res.writeHead(302, { location: `http://127.0.0.1:${ports.canary}/redirected` }).end();
    if (req.url === "/reach") {
      // Every way a page reaches out, at this computer's own service: directly, from a worker, and by a redirect.
      const own = `http://127.0.0.1:${ports.canary}`;
      return void res.writeHead(200, { "content-type": "text/html" }).end(`<title>Reach</title>
<img src="${own}/img"><img src="/redirect"><iframe src="${own}/frame"></iframe>
<script>fetch("${own}/fetch").catch(()=>{});new WebSocket("ws://127.0.0.1:${ports.canary}/ws");navigator.sendBeacon("${own}/beacon");
new Worker(URL.createObjectURL(new Blob([\`fetch("${own}/worker").catch(()=>{})\`])));</script>`);
    }
    res.writeHead(200, { "content-type": "text/html" }).end(PAGE);
  });
  canary = createServer((req, res) => {
    hits.push(req.url ?? "");
    res.end("canary");
  });
  canary.on("upgrade", (req, socket) => {
    hits.push(req.url ?? "");
    socket.destroy();
  });
  await Promise.all([site, canary].map((server) => new Promise<void>((done) => server.listen(0, "127.0.0.1", () => done()))));
  ports = { site: (site.address() as { port: number }).port, canary: (canary.address() as { port: number }).port };
  profile = mkdtempSync(join(tmpdir(), "sb-profile-"));
  launch = { executable: EXECUTABLE ?? "", profile };
  host = hostWith();
});

// A host whose proxy finds fixture.test at SITE and dials the fixture there; *options* add to it.
const hostWith = (options: Omit<BrowserHostOptions, "proxy"> = {}) => new BrowserHost({
  proxy: {
    resolve: (name) => (name === "fixture.test" ? Promise.resolve([SITE]) : name === "other.test" ? Promise.resolve([OTHER]) : Promise.reject(new Error("ENOTFOUND"))),
    local: () => ["127.0.0.1", "::1"],
    // The networks this computer is on: none here, so no test reads the machine's own.
    subnets: () => [],
    connect: (address, port) => connectTcp({ host: "127.0.0.1", port: (address === SITE || address === OTHER) && port === 80 ? ports.site : 9 }),
  },
  ...options,
});

afterEach(async () => {
  await host.close();
  await Promise.all([site, canary].map((server) => new Promise<void>((done) => server.close(() => done()))));
  rmSync(profile, { recursive: true, force: true });
});

const op = (session: string, kind: string, args: Record<string, unknown> = {}, root = ROOT) =>
  host.perform(launch, root, session, kind, args, new AbortController().signal) as Promise<{ ok?: any; error?: { type: string; message: string } }>;
const script = async (session: string, code: string, root = ROOT) => (await op(session, "browser.evaluate", { code }, root)).ok?.value;
const session = () => `session-${(next += 1)}`;
// What *work* answers within *ms*, or "late".
const within = <T>(ms: number, work: Promise<T>) => Promise.race([work, new Promise<"late">((done) => setTimeout(() => done("late"), ms))]);

// How many pages the running browser has.
const pages = async () => (await (host as unknown as { running: Promise<BrowserContext> }).running).pages().length;
// Each session's pages, as the host keeps them.
const tabs = () => (host as unknown as { tabs: Map<string, Page[]> }).tabs;

// The browser's window on this run's own Xvfb, as xwininfo lists it: its id, and the title of the tab in
// front, which its name begins with. Every tab of the agent's is a tab of that one window, and under
// Playwright each page says it is visible and has the focus, in front or not: so which tab is in front
// is read here, never at a page.
function xwindow(): { id: string; front: string } | undefined {
  return execFileSync("xwininfo", ["-root", "-tree"], { encoding: "utf8" }).split("\n").flatMap((line) => {
    const found = /^\s+(0x[0-9a-f]+) "(.*?) - [^"]*": \(/.exec(line);
    return found ? [{ id: found[1]!, front: found[2]! }] : [];
  })[0];
}
const front = () => xwindow()?.front;
// The user's own hand on that display, as X events (x-user.py): the window given the keyboard, a click, keys typed.
const X_USER = fileURLToPath(new URL("./x-user.py", import.meta.url));
const asUser = (...args: string[]) => void execFileSync("python3", [X_USER, ...args]);
// Where the middle of *page*'s element is on the screen, for a click of its user's.
const onScreen = (page: Page, id: string) => page.evaluate((of) => {
  const box = document.getElementById(of)!.getBoundingClientRect();
  return [
    window.screenX + Math.round((window.outerWidth - window.innerWidth) / 2 + box.x + box.width / 2),
    window.screenY + (window.outerHeight - window.innerHeight) + Math.round(box.y + box.height / 2),
  ] as const;
}, id);

// The browser's processes for this profile, each as its command line: Chrome rewrites its title, so split on spaces too.
function processes(): Array<{ pid: string; args: string[] }> {
  return readdirSync("/proc").filter((pid) => /^\d+$/.test(pid)).flatMap((pid) => {
    try {
      const line = readFileSync(`/proc/${pid}/cmdline`, "utf8");
      return line.includes(profile) ? [{ pid, args: line.split(/[\0 ]/).filter(Boolean) }] : [];
    } catch {
      return [];
    }
  });
}

// The sockets *pid* holds, by inode: none where its folder cannot be read, as a sandboxed renderer's.
function sockets(pid: string): string[] {
  try {
    return readdirSync(`/proc/${pid}/fd`).flatMap((fd) => {
      try {
        return /^socket:\[(\d+)\]$/.exec(readlinkSync(`/proc/${pid}/fd/${fd}`))?.slice(1) ?? [];
      } catch {
        return [];
      }
    });
  } catch {
    return [];
  }
}

// The TCP sockets listening on this computer, by inode.
const listening = (): Set<string> => new Set(["/proc/net/tcp", "/proc/net/tcp6"].flatMap((table) =>
  readFileSync(table, "utf8").split("\n").slice(1).map((row) => row.trim().split(/\s+/)).filter((fields) => fields[3] === "0A").map((fields) => fields[9] ?? "")));

describe("the browser tests' gate", () => {
  it("names each thing that would put a test browser on the user's session", () => {
    expect(notIsolated({ WAYLAND_DISPLAY: "wayland-0", XDG_SESSION_TYPE: "wayland", HOME: userInfo().homedir, XDG_RUNTIME_DIR: "/run/user/1000", DISPLAY: ":4242" })).toEqual([
      "WAYLAND_DISPLAY is set",
      "XDG_SESSION_TYPE and GDK_BACKEND are not both x11",
      "DBUS_SESSION_BUS_ADDRESS is not disabled:",
      "HOME is not a scratch folder",
      "XDG_CONFIG_HOME is not a scratch folder",
      "XDG_DATA_HOME is not a scratch folder",
      "XDG_CACHE_HOME is not a scratch folder",
      "XDG_STATE_HOME is not a scratch folder",
      "XDG_RUNTIME_DIR is not a scratch folder of mode 0700",
      "TMPDIR is not a scratch folder",
      "DISPLAY is not an Xvfb's",
    ]);
    // The real /tmp is everyone's: the browser's own folders would be left there.
    for (const real of ["/tmp", "/tmp/", "/tmp/."]) expect(notIsolated({ TMPDIR: real })).toContain("TMPDIR is not a scratch folder");
    const scratch = mkdtempSync(join(tmpdir(), "sb-gate-"));
    try {
      mkdirSync(join(scratch, "tmp"));
      mkdirSync(join(scratch, "own"));
      // A temp folder of the run's own: under the scratch root its home is in.
      expect(notIsolated({ HOME: join(scratch, "own"), TMPDIR: join(scratch, "tmp") })).not.toContain("TMPDIR is not a scratch folder");
      // Anywhere else it is shared, or the user's: another temp root, the user's home, or with no scratch home to be under.
      const { homedir: real } = userInfo();
      for (const temp of ["/var/tmp", real, tmpdir()]) {
        expect(notIsolated({ HOME: join(scratch, "own"), TMPDIR: temp }), temp).toContain("TMPDIR is not a scratch folder");
      }
      expect(notIsolated({ TMPDIR: join(scratch, "tmp") })).toContain("TMPDIR is not a scratch folder");
      // A link is where it leads: into the user's home, or to the session's own runtime folder.
      const { uid, homedir } = userInfo();
      symlinkSync(homedir, join(scratch, "home"));
      symlinkSync(`/run/user/${uid}`, join(scratch, "run"));
      symlinkSync("/tmp", join(scratch, "temp"));
      const linked = notIsolated({ HOME: join(scratch, "home"), XDG_CONFIG_HOME: join(scratch, "home", ".config"), XDG_RUNTIME_DIR: join(scratch, "run"), TMPDIR: join(scratch, "temp") });
      for (const named of ["HOME is not a scratch folder", "XDG_CONFIG_HOME is not a scratch folder", "XDG_RUNTIME_DIR is not a scratch folder of mode 0700", "TMPDIR is not a scratch folder"]) {
        expect(linked).toContain(named);
      }
      // And a folder that is not there is no scratch folder.
      expect(notIsolated({ HOME: join(scratch, "gone") })).toContain("HOME is not a scratch folder");
    } finally {
      rmSync(scratch, { recursive: true, force: true });
    }
  });
});

describe("the processes on a profile", () => {
  it("finds them without waiting on one whose command line does not come, as on a dead mount, and asks that one no more", async () => {
    const folder = mkdtempSync(join(tmpdir(), "sb-holding-"));
    const on = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)", join(folder, "profile", "x")], { stdio: "ignore" });
    try {
      await new Promise((done) => on.once("spawn", done));
      // This test's own process stands for one blocked in its read: its command line never comes.
      const stuck = `/proc/${process.pid}/cmdline`;
      const asked: string[] = [];
      const read = (path: string) => (asked.push(path), path === stuck ? new Promise<string>(() => {}) : readFile(path, "utf8"));
      const started = performance.now();
      expect(await holding(join(folder, "profile"), read)).toEqual([on.pid]);
      expect(performance.now() - started).toBeLessThan(3_000);
      expect(await holding(join(folder, "profile"), read)).toEqual([on.pid]);
      expect(asked.filter((path) => path === stuck)).toHaveLength(1);
    } finally {
      on.kill("SIGKILL");
      rmSync(folder, { recursive: true, force: true });
    }
  });
});

describe("what a host that was killed left staged", () => {
  it("is cleared by the next host to start in its temporary folder, and nothing of a host whose process still runs", async () => {
    const temp = mkdtempSync(join(tmpdir(), "sb-staged-"));
    // A process that has ended, as a host that was killed: its number names what it left.
    const gone = spawn(process.execPath, ["-e", ""], { stdio: "ignore" });
    await new Promise((done) => gone.once("exit", done));
    const kept = (name: string) => {
      mkdirSync(join(temp, name));
      writeFileSync(join(temp, name, "8f6c2a51-staged"), "a download nobody saved");
      return name;
    };
    try {
      const left = kept(`surogate-downloads-${gone.pid}-AbC123`);
      // A host of this process, one of another that runs (this one's parent), and one of a process that is not
      // this user's to ask after: none is known to be gone.
      const running = [kept(`surogate-downloads-${process.pid}-dEf456`), kept(`surogate-downloads-${process.ppid}-gHi789`), kept("surogate-downloads-1-jKl012")];
      // What is no host's staging folder is left alone, whatever its name begins with.
      const others = [kept("playwright-artifacts-mNo345"), kept("surogate-downloads-pending"), kept(`surogate-downloads-x${gone.pid}-q`)];
      await clearStaged(temp);
      expect(readdirSync(temp).sort()).toEqual([...running, ...others].sort());
      expect(existsSync(join(temp, left))).toBe(false);
      for (const name of [...running, ...others]) expect(readdirSync(join(temp, name))).toEqual(["8f6c2a51-staged"]);
      // A temporary folder that is not there has nothing to clear.
      await clearStaged(join(temp, "none"));
    } finally {
      rmSync(temp, { recursive: true, force: true });
    }
  });
});

describe("the browser held, as the host keeps it", () => {
  // These run anywhere, apart from the user's session or not: so with a browser that is not there, which
  // an operation that came to launch one would fail for, launching nothing.
  const sent = (session: string, kind: string, args: Record<string, unknown>, root: string) =>
    host.perform({ executable: join(profile, "no-browser-here"), profile }, root, session, kind, args, new AbortController().signal);
  // A close with no tab closes nothing, and launches none.
  const closes = () => sent("session-of-another", "browser.close", {}, "chat-3");

  it("keeps the browser held when the chat that took it over is deleted, or another is: only a hand back by the chat that holds it ends it", async () => {
    host.pause("chat-1", true);
    expect(await closes()).toEqual(PAUSED);
    await host.forget("chat-2");
    await host.forget("chat-1");
    expect(await closes()).toEqual(PAUSED);
    // The next chat to take it over, the first one deleted, holds it: the first's hand back ends nothing.
    host.pause("chat-2", true);
    host.pause("chat-1", false);
    expect(await closes()).toEqual(PAUSED);
    host.pause("chat-2", false);
    expect(await closes()).toEqual({ ok: { closed: false } });
  });

  it("launches no browser for an operation whose turn comes while its user holds the browser", async () => {
    const state = host as unknown as { running: Promise<BrowserContext> | null };
    host.pause("chat-1", true);
    // Sent past whatever refuses it sooner, as one that was on its way when the browser was taken over.
    expect(await sent("session-of-its-own", "browser.navigate", { url: "http://fixture.test/" }, "chat-1")).toEqual(PAUSED);
    expect(await sent("session-of-another", "browser.evaluate", { code: "return 1;" }, "chat-2")).toEqual(PAUSED);
    expect(state.running).toBeNull();
  });
});

describe("a page's download, as the host stages it", () => {
  // These run anywhere too: the browser's part is a download as Playwright gives one, ending when the test
  // says, on a file of the test's own.
  let staged: StagedDownload[];
  let did: string[];
  const SESSION = "session-of-its-own";
  // The session's page, and a tab that is no session's: one its user opened themselves.
  const PAGE = {} as Page;
  const TAB = {} as Page;
  const state = () => host as unknown as {
    roots: Map<string, string>; tabs: Map<string, Page[]>; unseen: Map<string, string[]>; interrupt: AbortController; arriving: Set<unknown>;
    open: Map<unknown, unknown>;
    arrived(page: Page, download: unknown): Promise<void>;
    requested(request: unknown): void;
    loaded(request: unknown): void;
    failed(request: unknown): void;
  };
  // The host's clock, which a test moves.
  let clock: number;
  // A download the browser announces in *page*.
  const arrives = (download: unknown, page = PAGE) => state().arrived(page, download);
  // A navigation's request, as the browser says it begins: to *url*, and after *from* where a redirect led there.
  const asks = (url: string, from: unknown = null, navigation = true) => {
    const request = { url: () => url, isNavigationRequest: () => navigation, redirectedFrom: () => from };
    state().requested(request);
    return request;
  };
  const SITE_URL = "http://fixture.test/export";
  // A download named *name*, whose file is *file* once *ends* settles: with its path, or with why it did not finish.
  // *url*: its address; one no request is known of, unless a test's request asked for it.
  const downloadOf = (name: string, file: string, ends: Promise<string>, url = `blob:http://fixture.test/${name}`) => ({
    url: () => url,
    suggestedFilename: () => name,
    path: () => ends,
    failure: () => ends.then(() => null, (error: Error) => error.message),
    cancel: () => (did.push("cancel"), Promise.resolve()),
    delete: () => (did.push("delete"), rmSync(file, { force: true }), Promise.resolve()),
  });
  const fileOf = (bytes: number) => {
    const file = join(profile, `staged-${(next += 1)}`);
    writeFileSync(file, "");
    // Its size without its bytes: a hole.
    truncateSync(file, bytes);
    return file;
  };

  beforeEach(() => {
    staged = [];
    did = [];
    clock = 1_700_000_000_000;
    host = new BrowserHost({ downloaded: (download) => staged.push(download), now: () => clock });
    // The session's tab, as its chat's: an operation of the session's says whose it is.
    state().roots.set(SESSION, "chat-1");
    state().tabs.set(SESSION, [PAGE]);
  });

  it("hands on one of exactly what a write may carry, and none a byte over, which it removes and says", async () => {
    const most = fileOf(MAX_WRITE_BYTES);
    await arrives(downloadOf("most.bin", most, Promise.resolve(most)));
    expect(staged).toEqual([{ root: "chat-1", session: SESSION, name: "most.bin", path: most, user: false }]);
    expect([did, existsSync(most), state().unseen.get(SESSION)]).toEqual([[], true, undefined]);
    // Handed on, nothing is kept of it for a take-over to stop.
    expect([state().arriving.size, getEventListeners(state().interrupt.signal, "abort")]).toEqual([0, []]);
    const over = fileOf(MAX_WRITE_BYTES + 1);
    await arrives(downloadOf("over.bin", over, Promise.resolve(over)));
    expect([staged.length, did, existsSync(over)]).toEqual([1, ["delete"], false]);
    expect([MAX_WRITE_BYTES, state().unseen.get(SESSION)]).toEqual([50 * 1024 * 1024, [tooLarge("over.bin", MAX_WRITE_BYTES + 1)]]);
    expect(tooLarge("over.bin", MAX_WRITE_BYTES + 1)).toBe(
      'The page downloaded "over.bin" (52428801 bytes), too large to save in the chat\'s folder at once (at most 52428800 bytes), so it was not saved.',
    );
    // A host told another limit names that one.
    host = new BrowserHost({ downloaded: (download) => staged.push(download), downloadBytes: 3 });
    state().roots.set(SESSION, "chat-1");
    state().tabs.set(SESSION, [PAGE]);
    const small = fileOf(4);
    await arrives(downloadOf("small.bin", small, Promise.resolve(small)));
    expect(state().unseen.get(SESSION)).toEqual([
      'The page downloaded "small.bin" (4 bytes), too large to save in the chat\'s folder at once (at most 3 bytes), so it was not saved.',
    ]);
  });

  it("hands on none it cannot measure, nor one that did not finish, nor one of a page its session has closed, and tells the agent of the first two", async () => {
    const gone = join(profile, "gone");
    await arrives(downloadOf("gone.txt", gone, Promise.resolve(gone)));
    await arrives(downloadOf("broken.txt", gone, Promise.reject(new Error("canceled"))));
    // What else the browser says of one that did not finish is not passed on: an error's own text is not the agent's to read.
    await arrives(downloadOf("closed.txt", gone, Promise.reject(new Error(`Target page, context or browser has been closed: ${profile}`))));
    expect(state().unseen.get(SESSION)).toEqual([
      'The page downloaded "gone.txt", but its size could not be measured, so it was not saved.', notFinished("broken.txt", "canceled"),
      'The page\'s download of "closed.txt" did not finish (the browser stopped it), so it was not saved.',
    ]);
    // A page that is no session's any more, as one its session closed: what it finished after is removed, and nobody is told.
    const late = fileOf(6);
    await arrives(downloadOf("late.txt", late, Promise.resolve(late)), {} as Page);
    expect([staged, existsSync(late), [...state().unseen.keys()]]).toEqual([[], false, [SESSION]]);
    // Nor is a session told whose chat the host does not know: its file, too large or not, is removed.
    state().unseen.clear();
    state().roots.delete(SESSION);
    const [whole, over] = [fileOf(6), fileOf(MAX_WRITE_BYTES + 1)];
    await arrives(downloadOf("whole.txt", whole, Promise.resolve(whole)));
    await arrives(downloadOf("over.bin", over, Promise.resolve(over)));
    expect([staged, existsSync(whole), existsSync(over), state().unseen.size]).toEqual([[], false, false, 0]);
    // A name as long as a page likes is quoted at what a name may be.
    expect(notFinished("x".repeat(300), "canceled")).toBe(`The page's download of "${"x".repeat(200)}" did not finish (canceled), so it was not saved.`);
  });

  it("drops one of the agent's that had finished, but was not handed on yet, when its user takes the browser over: its file removed, and its agent told", async () => {
    const file = fileOf(6);
    const ends = Promise.withResolvers<string>();
    const staging = arrives(downloadOf("report.txt", file, ends.promise));
    // Taken over from another chat, as it ends: stopped by what it began under, though that changes nothing of one that has ended.
    host.pause("chat-2", true);
    ends.resolve(file);
    await staging;
    expect([staged, did, existsSync(file)]).toEqual([[], ["cancel", "delete"], false]);
    // Kept for its session's next answer, though the browser is held: it began while the agent drove.
    expect(state().unseen.get(SESSION)).toEqual([interrupted("report.txt")]);
    // Handed back before it ends, the same: nothing stopped by a take-over is taken up again.
    host.pause("chat-2", false);
    clock += AFTER_HAND_BACK_MS + 1;
    const again = fileOf(6);
    const later = Promise.withResolvers<string>();
    const second = arrives(downloadOf("again.txt", again, later.promise));
    host.pause("chat-2", true);
    host.pause("chat-2", false);
    later.resolve(again);
    await second;
    expect([staged, existsSync(again), state().unseen.get(SESSION)]).toEqual([[], false, [interrupted("report.txt"), interrupted("again.txt")]]);
    // One still on its way ends there, as the browser ends one that is cancelled: said as interrupted, not as a failure of its own.
    clock += AFTER_HAND_BACK_MS + 1;
    const slow = fileOf(3);
    const cut = Promise.withResolvers<string>();
    const third = arrives(downloadOf("slow.bin", slow, cut.promise));
    host.pause("chat-1", true);
    cut.reject(new Error("canceled"));
    await third;
    expect([staged, did.at(-1), state().unseen.get(SESSION)?.at(-1)]).toEqual([[], "cancel", interrupted("slow.bin")]);
  });

  it("stops every one of the agent's on its way at a take-over, however many, with nothing of each on what the take-over stops operations by", async () => {
    const ends = Array.from({ length: 12 }, () => Promise.withResolvers<string>());
    const files = ends.map(() => fileOf(6));
    const staging = ends.map((end, at) => arrives(downloadOf(`${at}.bin`, files[at]!, end.promise)));
    // On their way: none has added to the signal, which lasts until the browser is next taken over.
    expect([state().arriving.size, getEventListeners(state().interrupt.signal, "abort")]).toEqual([12, []]);
    host.pause("chat-2", true);
    expect(did).toEqual(ends.map(() => "cancel"));
    ends.forEach((end, at) => end.resolve(files[at]!));
    await Promise.all(staging);
    expect([staged, state().arriving.size, files.some((file) => existsSync(file))]).toEqual([[], 0, false]);
    // Each measured in its own time: told in whatever order they ended.
    expect([...(state().unseen.get(SESSION) ?? [])].sort()).toEqual(ends.map((_, at) => interrupted(`${at}.bin`)).sort());
  });

  it("takes one in a tab no chat owns for its user's while they hold the browser, for the chat it is held from; with nobody holding it, or that chat deleted, stops it at once and removes its file", async () => {
    // Nobody holds the browser: no chat is there to ask, so it is stopped, and what it left removed, as before downloads were kept.
    const first = fileOf(6);
    await arrives(downloadOf("first.txt", first, Promise.resolve(first)), TAB);
    expect([staged, did, existsSync(first)]).toEqual([[], ["cancel", "delete"], false]);
    // Held: theirs, saved in the chat they took the browser over from, as that chat's own.
    host.pause("chat-2", true);
    const second = fileOf(6);
    await arrives(downloadOf("second.txt", second, Promise.resolve(second)), TAB);
    expect(staged).toEqual([{ root: "chat-2", session: "chat-2", name: "second.txt", path: second, user: true }]);
    // One too large to save is removed there too.
    const over = fileOf(MAX_WRITE_BYTES + 1);
    await arrives(downloadOf("over.bin", over, Promise.resolve(over)), TAB);
    expect([staged.length, existsSync(over)]).toEqual([1, false]);
    // The chat it is held from deleted: held still, with no chat to save in.
    await host.forget("chat-2");
    const third = fileOf(6);
    did.length = 0;
    await arrives(downloadOf("third.txt", third, Promise.resolve(third)), TAB);
    expect([staged.length, did, existsSync(third)]).toEqual([1, ["cancel", "delete"], false]);
    // No agent is told of any of them, and none is kept as on its way.
    expect([state().unseen.size, state().arriving.size]).toEqual([0, 0]);
  });

  it("takes one that starts while its user holds the browser for theirs, from whichever chat, though it ends after they handed it back: handed on as theirs, and the agent told nothing of it, saved or not", async () => {
    host.pause("chat-2", true);
    const file = fileOf(6);
    const ends = Promise.withResolvers<string>();
    const staging = arrives(downloadOf("statement.pdf", file, ends.promise));
    // Handed back while it is on its way: it ends with nobody holding the browser, and is theirs by when it started.
    host.pause("chat-2", false);
    ends.resolve(file);
    await staging;
    expect(staged).toEqual([{ root: "chat-1", session: SESSION, name: "statement.pdf", path: file, user: true }]);
    expect(did).toEqual([]);
    // Another, and the browser taken over again before it ends, from another chat: stopped by no take-over.
    host.pause("chat-2", true);
    const next = fileOf(6);
    const later = Promise.withResolvers<string>();
    const second = arrives(downloadOf("payslip.pdf", next, later.promise));
    host.pause("chat-2", false);
    host.pause("chat-1", true);
    later.resolve(next);
    await second;
    expect([staged[1], did]).toEqual([{ root: "chat-1", session: SESSION, name: "payslip.pdf", path: next, user: true }, []]);
    // Too large, not finished, or not measured: gone without a word to the agent.
    const over = fileOf(MAX_WRITE_BYTES + 1);
    await arrives(downloadOf("over.bin", over, Promise.resolve(over)));
    await arrives(downloadOf("broken.txt", over, Promise.reject(new Error("canceled"))));
    await arrives(downloadOf("gone.txt", join(profile, "gone"), Promise.resolve(join(profile, "gone"))));
    host.pause("chat-1", false);
    expect([staged.length, existsSync(over), state().unseen.has(SESSION)]).toEqual([2, false, false]);
  });

  it("takes one with no request known for the agent's only while nobody holds the browser and more than a minute after it was last handed back: to the millisecond", async () => {
    // Whose it is handed on as: the agent's; its user's, of which the agent hears nothing; or its user's only
    // because it came just after they handed the browser back, of which the agent hears what came.
    const whose = async (name: string) => {
      const file = fileOf(6);
      await arrives(downloadOf(name, file, Promise.resolve(file)));
      const handed = staged.at(-1);
      if (handed?.name !== name) return "not staged";
      return handed.user ? (handed.afterHandBack ? "theirs, just after" : "theirs") : (handed.afterHandBack ? "malformed" : "the agent's");
    };
    expect(AFTER_HAND_BACK_MS).toBe(60_000);
    // Never held: the agent's, as a page's own.
    expect(await whose("first.bin")).toBe("the agent's");
    // Held: theirs.
    host.pause("chat-2", true);
    clock += 5_000;
    expect(await whose("held.bin")).toBe("theirs");
    host.pause("chat-2", false);
    // Handed back: what comes in the minute after may have been asked for while they held it. In doubt, theirs.
    expect(await whose("at-once.bin")).toBe("theirs, just after");
    clock += AFTER_HAND_BACK_MS - 1;
    expect(await whose("just-before.bin")).toBe("theirs, just after");
    clock += 1;
    expect(await whose("at-the-minute.bin")).toBe("theirs, just after");
    clock += 1;
    expect(await whose("just-after.bin")).toBe("the agent's");
    // The minute runs from the last hand back, by whichever chat.
    host.pause("chat-1", true);
    host.pause("chat-1", false);
    clock += AFTER_HAND_BACK_MS;
    expect(await whose("again.bin")).toBe("theirs, just after");
    clock += 1;
    expect(await whose("after-again.bin")).toBe("the agent's");
    // Announced while they hold it, it is theirs outright, whatever the clock says: taken over again within the
    // minute of the last hand back, and at every moment of that minute.
    host.pause("chat-1", true);
    host.pause("chat-1", false);
    host.pause("chat-2", true);
    for (const passed of [0, 1, AFTER_HAND_BACK_MS - 1, 1, 1, 10 * AFTER_HAND_BACK_MS]) {
      clock += passed;
      expect(await whose(`held-${clock}.bin`)).toBe("theirs");
    }
    // The host tells the agent nothing of any that it handed on: what came of each is the saver's to say.
    expect(state().unseen.size).toBe(0);
    // Handed on, the mark is there only where it holds: an own property of none of the others.
    expect(staged.map((download) => Object.hasOwn(download, "afterHandBack"))).toEqual(staged.map((download) => download.afterHandBack === true));
  });

  it("tells the agent that one taken for its user's only by the minute was not saved, in one sentence that names no file and gives no reason; and nothing of one that is theirs outright", async () => {
    expect(LEFT_TO_USER).toBe(
      "A download that began just after the user handed the agent's browser back was the user's to save, and was not saved. If it was the agent's own, the agent may start it again.",
    );
    // Each way a download is not handed on: too large, not finished, not measured.
    const fails = async () => {
      const over = fileOf(MAX_WRITE_BYTES + 1);
      await arrives(downloadOf("statement.pdf", over, Promise.resolve(over)));
      await arrives(downloadOf("statement.pdf", over, Promise.reject(new Error("canceled"))));
      await arrives(downloadOf("statement.pdf", join(profile, "gone"), Promise.resolve(join(profile, "gone"))));
      return existsSync(over);
    };
    // Held: theirs outright. Not a word, at any moment.
    host.pause("chat-2", true);
    expect([await fails(), state().unseen.size]).toEqual([false, 0]);
    host.pause("chat-2", false);
    // Just after the hand back, with no request known: it may be the agent's own, which is told that much.
    expect([await fails(), state().unseen.get(SESSION)]).toEqual([false, [LEFT_TO_USER, LEFT_TO_USER, LEFT_TO_USER]]);
    state().unseen.clear();
    // One whose request began while they held it is theirs outright in that minute too: not a word.
    host.pause("chat-2", true);
    asks(SITE_URL);
    asks(`${SITE_URL}?again`);
    host.pause("chat-2", false);
    const over = fileOf(MAX_WRITE_BYTES + 1);
    await arrives(downloadOf("export.csv", over, Promise.resolve(over), SITE_URL));
    const file = fileOf(6);
    await arrives(downloadOf("export.csv", file, Promise.resolve(file), `${SITE_URL}?again`));
    expect([staged, state().unseen.size]).toEqual([[{ root: "chat-1", session: SESSION, name: "export.csv", path: file, user: true }], 0]);
    // Nor is one in a tab no chat owns told to anyone: no agent acts there.
    const large = fileOf(MAX_WRITE_BYTES + 1);
    await arrives(downloadOf("own.bin", large, Promise.resolve(large)), TAB);
    const own = fileOf(6);
    await arrives(downloadOf("own.bin", own, Promise.resolve(own)), TAB);
    expect([staged[1], existsSync(large), state().unseen.size]).toEqual([{ root: "chat-2", session: "chat-2", name: "own.bin", path: own, user: true }, false, 0]);
    // Past the minute it is the agent's, told as any of its own.
    clock += AFTER_HAND_BACK_MS + 1;
    const mine = fileOf(MAX_WRITE_BYTES + 1);
    await arrives(downloadOf("report.bin", mine, Promise.resolve(mine)));
    expect(state().unseen.get(SESSION)).toEqual([tooLarge("report.bin", MAX_WRITE_BYTES + 1)]);
  });

  it("takes one whose request began while nobody held the browser for the agent's, in the minute after a hand back too; and one whose request began while it was held for its user's however late it comes", async () => {
    // Held, handed back, and then a navigation of the agent's: its request is known to have begun after.
    host.pause("chat-2", true);
    host.pause("chat-2", false);
    asks(SITE_URL);
    const mine = fileOf(6);
    await arrives(downloadOf("export.csv", mine, Promise.resolve(mine), SITE_URL));
    expect(staged).toEqual([{ root: "chat-1", session: SESSION, name: "export.csv", path: mine, user: false }]);
    // A request begun while held, answered long after the hand back, past the minute, at the end of a redirect:
    // the download's address is the last request's, which takes its beginning from the first. The browser
    // says a redirected request has ended before it says the next has begun.
    host.pause("chat-2", true);
    const first = asks("http://fixture.test/hop1");
    host.pause("chat-2", false);
    clock += 10 * AFTER_HAND_BACK_MS;
    state().loaded(first);
    const last = asks("http://other.test/late.bin", asks("http://fixture.test/hop2", first));
    state().failed(last);
    const theirs = fileOf(6);
    // Its address may carry a fragment its request's does not.
    await arrives(downloadOf("late.bin", theirs, Promise.resolve(theirs), "http://other.test/late.bin#top"));
    expect(staged[1]).toEqual({ root: "chat-1", session: SESSION, name: "late.bin", path: theirs, user: true });
    expect([did, state().unseen.size, state().open.size]).toEqual([[], 0, 0]);
  });

  it("drops one whose request began while nobody held the browser and that comes while it is held, or after it was held meanwhile, and tells its agent", async () => {
    // The agent's navigation, not answered yet when its user takes the browser over: announced under their hand.
    asks(SITE_URL);
    host.pause("chat-2", true);
    const file = fileOf(6);
    await arrives(downloadOf("export.csv", file, Promise.resolve(file), SITE_URL));
    expect([staged, did, existsSync(file)]).toEqual([[], ["cancel", "delete"], false]);
    // Held and handed back before it is answered: stopped by the take-over all the same, however long after.
    host.pause("chat-2", false);
    asks(`${SITE_URL}?again`);
    host.pause("chat-2", true);
    host.pause("chat-2", false);
    clock += 10 * AFTER_HAND_BACK_MS;
    const again = fileOf(6);
    await arrives(downloadOf("again.csv", again, Promise.resolve(again), `${SITE_URL}?again`));
    expect([staged, existsSync(again)]).toEqual([[], false]);
    expect(state().unseen.get(SESSION)).toEqual([interrupted("export.csv"), interrupted("again.csv")]);
  });

  it("takes one for its user's where two requests of its address are known and one began while they held the browser", async () => {
    asks(SITE_URL);
    host.pause("chat-2", true);
    asks(SITE_URL);
    host.pause("chat-2", false);
    clock += 10 * AFTER_HAND_BACK_MS;
    // In doubt which of the two it is the answer to: theirs.
    const first = fileOf(6);
    await arrives(downloadOf("export.csv", first, Promise.resolve(first), SITE_URL));
    expect(staged).toEqual([{ root: "chat-1", session: SESSION, name: "export.csv", path: first, user: true }]);
    // The other is the agent's own, begun before the take-over: dropped, as any.
    const second = fileOf(6);
    await arrives(downloadOf("export.csv", second, Promise.resolve(second), SITE_URL));
    expect([staged.length, existsSync(second), state().unseen.get(SESSION)]).toEqual([1, false, [interrupted("export.csv")]]);
  });

  it("sends one in a tab no chat owns to the chat the browser was held from when it began, though it comes after the hand back; and stops one there that began with nobody holding it", async () => {
    host.pause("chat-2", true);
    asks(SITE_URL);
    host.pause("chat-2", false);
    // Taken over from another chat by the time it comes: it began under the first.
    clock += 10 * AFTER_HAND_BACK_MS;
    host.pause("chat-1", true);
    const file = fileOf(6);
    await arrives(downloadOf("statement.pdf", file, Promise.resolve(file), SITE_URL), TAB);
    expect(staged).toEqual([{ root: "chat-2", session: "chat-2", name: "statement.pdf", path: file, user: true }]);
    host.pause("chat-1", false);
    // No request known, in the minute after the hand back: in doubt, theirs, for the chat that handed it back.
    const doubt = fileOf(6);
    await arrives(downloadOf("doubt.bin", doubt, Promise.resolve(doubt)), TAB);
    expect(staged[1]).toEqual({ root: "chat-1", session: "chat-1", name: "doubt.bin", path: doubt, user: true });
    // A request begun with nobody holding the browser, in that same minute: no chat's to ask.
    asks(`${SITE_URL}?own`);
    const own = fileOf(6);
    await arrives(downloadOf("own.bin", own, Promise.resolve(own), `${SITE_URL}?own`), TAB);
    // And one with no request known, more than a minute after.
    clock += AFTER_HAND_BACK_MS + 1;
    const late = fileOf(6);
    await arrives(downloadOf("late.bin", late, Promise.resolve(late)), TAB);
    expect([staged.length, existsSync(own), existsSync(late), state().unseen.size]).toEqual([2, false, false, 0]);
  });

  it("keeps of the browser's requests only navigations not yet ended as a page, each failed one a moment for the download it may be, and no more than a bound of them", async () => {
    // What a page loads beside its own document is no download's beginning.
    asks("http://fixture.test/image.png", null, false);
    expect(state().open.size).toBe(0);
    // One that loaded as a page is done with.
    state().loaded(asks(SITE_URL));
    expect(state().open.size).toBe(0);
    // One the browser gave up as a page, as it does a moment before it announces it as a download: kept for that.
    const failed = asks(`${SITE_URL}?failed`);
    state().failed(failed);
    clock += 10_000;
    asks(`${SITE_URL}?next`);
    expect(state().open.has(failed)).toBe(true);
    clock += 1;
    asks(`${SITE_URL}?after`);
    expect([state().open.has(failed), state().open.size]).toEqual([false, 2]);
    // However many are asked for and never answered, the oldest go.
    const many = Array.from({ length: 300 }, (_, at) => asks(`${SITE_URL}?${at}`));
    expect([state().open.size, state().open.has(many[0]), state().open.has(many.at(-1))]).toEqual([256, false, true]);
  });
});

describe.skipIf(!run)("the browser host", () => {
  beforeAll(() => isolated());

  it("launches the user's browser over a pipe, its sandbox on, without Playwright's weakening defaults, every request through the proxy", async () => {
    const a = session();
    expect(await op(a, "browser.navigate", { url: "http://fixture.test/", wait_until: "load" })).toEqual({
      ok: { url: "http://fixture.test/", title: "Fixture", opened: true, notices: [] },
    });
    const main = processes().find(({ args }) => !args.some((arg) => arg.startsWith("--type=")) && args.includes("--remote-debugging-pipe"));
    expect(main).toBeDefined();
    const args = main!.args;
    expect(args.some((arg) => arg.startsWith("--remote-debugging-port"))).toBe(false);
    // And none is open: no process of the browser's listens, and it wrote no DevToolsActivePort.
    const open = listening();
    expect(processes().flatMap(({ pid }) => sockets(pid)).filter((inode) => open.has(inode))).toEqual([]);
    expect(existsSync(join(profile, "DevToolsActivePort"))).toBe(false);
    expect(processes().some(({ args: other }) => other.includes("--no-sandbox"))).toBe(false);
    for (const weak of WEAKENING) expect(args).not.toContain(weak);
    const disabled = args.filter((arg) => arg.startsWith("--disable-features=")).join(",");
    expect(disabled).not.toContain("HttpsUpgrades");
    expect(disabled).not.toContain("ThirdPartyStoragePartitioning");
    expect(args).toContain("--proxy-bypass-list=<-loopback>");
    expect(args.some((arg) => /^--proxy-server=http:\/\/127\.0\.0\.1:\d+$/.test(arg))).toBe(true);
    // The renderers run in the browser's seccomp sandbox.
    const renderers = processes().filter(({ args: other }) => other.includes("--type=renderer"));
    expect(renderers.length).toBeGreaterThan(0);
    for (const { pid } of renderers) expect(readFileSync(`/proc/${pid}/status`, "utf8")).toMatch(/Seccomp:\s+2/);
    // WebRTC sends no UDP around the proxy.
    const prefs = JSON.parse(readFileSync(join(profile, "Default", "Preferences"), "utf8"));
    expect(prefs.webrtc.ip_handling_policy).toBe("disable_non_proxied_udp");
    // And the browser keeps it: a connection the page makes gathers no UDP candidate.
    expect(await script(a, `const connection = new RTCPeerConnection();
connection.createDataChannel("probe");
const found = [];
const gathered = new Promise((done) => {
  connection.onicecandidate = ({ candidate }) => (candidate ? found.push(candidate.candidate) : done());
  setTimeout(done, 3000);
});
await connection.setLocalDescription(await connection.createOffer());
await gathered;
connection.close();
return found.filter((line) => / udp /i.test(line));`)).toEqual([]);
  });

  it("refuses this computer's own services to the page, whichever way it reaches, and to a navigation", async () => {
    const a = session();
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/reach" })).ok?.title).toBe("Reach");
    await new Promise((done) => setTimeout(done, 1_500));
    // The agent hears why, where the browser says only net::ERR_*.
    expect(await op(a, "browser.navigate", { url: `http://127.0.0.1:${ports.canary}/direct` })).toEqual({
      error: { type: "browser", message: `The agent's browser does not reach this computer's own services (127.0.0.1:${ports.canary})` },
    });
    expect(await op(a, "browser.navigate", { url: "https://192.168.1.5/" })).toEqual({
      error: { type: "browser", message: "The agent's browser does not reach private networks (192.168.1.5:443)" },
    });
    expect(hits).toEqual([]);
  });

  it("runs the operation sent right after a refused navigation, in the error page that comes after it", async () => {
    for (let round = 0; round < 3; round += 1) {
      const [a, b] = [session(), session()];
      expect((await op(a, "browser.navigate", { url: `http://127.0.0.1:${ports.canary}/` })).error?.type).toBe("browser");
      expect(await op(a, "browser.evaluate", { code: "return 1;" })).toEqual({ ok: { value: 1 } });
      // A shot too, once the page has drawn.
      expect((await op(b, "browser.navigate", { url: `http://127.0.0.1:${ports.canary}/` })).error?.type).toBe("browser");
      expect(typeof (await op(b, "browser.screenshot", { clip: null, labels: [] })).ok).toBe("string");
    }
  });

  it("opens only http and https addresses", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    for (const url of ["file:///etc/hostname", "chrome://version", "edge://version", "view-source:http://fixture.test/", "javascript:alert(1)", "not a url"]) {
      const refused = await op(a, "browser.navigate", { url });
      expect(refused.error?.type, url).toBe("browser");
    }
    expect(await script(a, "return location.href;")).toBe("http://fixture.test/");
  });

  it("gives each session a tab of its own, and closes only its own", async () => {
    const [a, b] = [session(), session()];
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/" })).ok?.opened).toBe(true);
    expect((await op(b, "browser.navigate", { url: "http://fixture.test/second" })).ok?.opened).toBe(true);
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/second" })).ok?.opened).toBe(false);
    expect(await op(a, "browser.close")).toEqual({ ok: { closed: true } });
    expect(await op(a, "browser.close")).toEqual({ ok: { closed: false } });
    // B's tab stays.
    expect(await script(b, "return document.title;")).toBe("Second");
    // A's next operation opens a tab again.
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/" })).ok?.opened).toBe(true);
  });

  it("says a session's tab opened at the first navigation to answer in it, whatever the session did there before", async () => {
    const [a, b] = [session(), session()];
    // A first navigation that fails, to a name nobody answers for: the tab is made, and its agent not told.
    expect((await op(a, "browser.navigate", { url: "http://nowhere.test/" })).error?.type).toBe("browser");
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/" })).ok).toMatchObject({ title: "Fixture", opened: true });
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/second" })).ok?.opened).toBe(false);
    // A session whose first operation is a script: its tab is made for it, and its first navigation says so.
    expect(await script(b, "return 1;")).toBe(1);
    expect((await op(b, "browser.navigate", { url: "http://fixture.test/" })).ok).toMatchObject({ title: "Fixture", opened: true });
    expect((await op(b, "browser.navigate", { url: "http://fixture.test/second" })).ok?.opened).toBe(false);
    // Closed and opened again: told again.
    await op(b, "browser.close");
    expect(await script(b, "return 2;")).toBe(2);
    expect((await op(b, "browser.navigate", { url: "http://fixture.test/" })).ok?.opened).toBe(true);
    // Nothing is kept of a tab closed, or gone with its browser, before any navigation answered in it.
    const untold = (host as unknown as { untold: Set<string> }).untold;
    const [c, d] = [session(), session()];
    expect(await script(c, "return 3;")).toBe(3);
    expect(await script(d, "return 4;")).toBe(4);
    expect([...untold]).toEqual([c, d]);
    await op(c, "browser.close");
    expect([...untold]).toEqual([d]);
    for (const { pid } of processes().filter(({ args }) => !args.some((arg) => arg.startsWith("--type=")))) process.kill(Number(pid), "SIGTERM");
    await expect.poll(() => processes().length, { timeout: 10_000 }).toBe(0);
    await expect.poll(() => [...untold], { timeout: 5_000 }).toEqual([]);
  }, 30_000);

  it("says the address of the page a session's next operation acts in, wherever the page sent itself, a popup's once it opened one", async () => {
    const a = session();
    expect(await host.address(a)).toBe("about:blank");
    await op(a, "browser.navigate", { url: "http://fixture.test/moves" });
    await expect.poll(() => host.address(a)).toBe("http://other.test/");
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    await op(a, "browser.mouse", { action: "click", x: 50, y: 155, button: "left", clicks: 1 });
    await expect.poll(() => host.address(a)).toBe("http://fixture.test/second");
  });

  it("finds the page's elements in every frame with their backend ids, and clicks one where it is now", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    const snapshot = (await op(a, "browser.observe", { script: "snapshot@1", params: { selector: null } })).ok;
    expect(snapshot.title).toBe("Fixture");
    const nodes = snapshot.frames.flatMap((frame: { x: number; nodes: unknown[] }) => frame.nodes.map((node) => ({ frame: frame.x, node })));
    const go = nodes.find(({ node }: { node: { name: string } }) => node.name === "Go").node;
    expect(go.role).toBe("button");
    expect(typeof go.backend_node_id).toBe("number");
    // The iframe's link, with its frame's origin.
    expect(nodes.find(({ node }: { node: { name: string } }) => node.name === "Inner link").frame).toBe(300);
    const place = (await op(a, "browser.observe", { script: "locate@1", params: { backend_node_id: go.backend_node_id, role: "button", name: "Go", nth: 0 } })).ok;
    expect(place).toEqual({ x: 90, y: 55 });
    // Found again by role and name when its id has gone.
    expect((await op(a, "browser.observe", { script: "locate@1", params: { backend_node_id: 999_999, role: "button", name: "Go", nth: 0 } })).ok).toEqual(place);
    expect((await op(a, "browser.observe", { script: "locate@1", params: { backend_node_id: null, role: "button", name: "Gone", nth: 0 } })).ok).toEqual({ missing: "gone" });
    await op(a, "browser.mouse", { action: "click", x: place.x, y: place.y, button: "left", clicks: 1 });
    expect(await script(a, "return document.title;")).toBe("clicked 1");
    expect((await op(a, "browser.observe", { script: "nothing@1", params: {} })).error?.type).toBe("browser");
  });

  it("measures a page's outline in the page, and sends none too large for the link", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/huge" });
    const outline = await op(a, "browser.observe", { script: "snapshot@1", params: { selector: null } });
    expect(outline.error).toEqual({ type: "browser", message: expect.stringMatching(/^The page's outline is too large to send: \d+ characters, at most 2097152\. /) });
    // A part of it, by a selector, is not.
    expect((await op(a, "browser.observe", { script: "snapshot@1", params: { selector: "button:first-of-type" } })).ok?.title).toBe("Huge");
  });

  it("types, presses keys, scrolls, drags and takes its shot, with the labels drawn for it and gone after", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    await op(a, "browser.keyboard", { action: "type", text: "héllo", at: { x: 140, y: 112 }, delay: 0 });
    await op(a, "browser.keyboard", { action: "press", keys: "Shift+Home", delay: 0 });
    expect(await script(a, "const i = document.getElementById('name'); return [i.value, i.selectionStart, i.selectionEnd];")).toEqual(["héllo", 0, 5]);
    const scrolled = (await op(a, "browser.mouse", { action: "wheel", x: 100, y: 100, delta_x: 0, delta_y: 600 })).ok;
    expect(scrolled.scroll_y).toBeGreaterThan(0);
    expect(scrolled.viewport_height).toBeGreaterThan(0);
    expect((await op(a, "browser.mouse", { action: "drag", path: [[10, 10], [20, 20], [30, 30]], button: "left" })).ok).toEqual({ notices: [] });
    const shot = (await op(a, "browser.screenshot", { clip: null, labels: [{ label: 1, x: 90, y: 55 }] })).ok as string;
    expect(Buffer.from(shot, "base64").subarray(0, 8)).toEqual(Buffer.from("\x89PNG\r\n\x1a\n", "latin1"));
    expect(await script(a, "return document.getElementById('surogates-overlay');")).toBeNull();
  });

  it("opens no file dialog for a file input, and tells the agent", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    expect((await op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 })).ok.notices).toEqual([FILE_ASKED]);
  });

  it("stages a download a session's page finished in its own temporary folder, and hands it on for the chat's folder; one its user started while they hold the browser as theirs, the agent told nothing", async () => {
    const staged: StagedDownload[] = [];
    host = hostWith({ downloaded: (download) => staged.push(download) });
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    // Its click says nothing of it: the save, after, is what the agent hears of.
    expect((await op(a, "browser.mouse", { action: "click", x: 60, y: 255, button: "left", clicks: 1 }, "chat-1")).ok.notices).toEqual([]);
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(1);
    expect(staged[0]).toMatchObject({ root: "chat-1", session: a, name: "report.txt", user: false });
    expect(await readFile(staged[0]!.path, "utf8")).toBe("report");
    // Not in the profile, nor anywhere the browser's own downloads go.
    expect(staged[0]!.path.startsWith(profile)).toBe(false);
    expect(readdirSync(profile).some((name) => name.includes("report"))).toBe(false);
    // Handed on, nothing is kept of it for a take-over to stop.
    expect((host as unknown as { arriving: Set<unknown> }).arriving.size).toBe(0);
    // Its user takes the browser over, and clicks the link themselves.
    host.pause("chat-1", true);
    await tabs().get(a)![0]!.click("#dl");
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(2);
    expect(staged[1]).toMatchObject({ root: "chat-1", session: a, name: "report.txt", user: true });
    host.pause("chat-1", false);
    expect((await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")).ok.notices).toEqual([]);
    // The browser is the agent's one browser: held from another chat, a download in this chat's page is its user's all the same.
    host.pause("chat-2", true);
    await tabs().get(a)![0]!.click("#dl");
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(3);
    expect(staged[2]).toMatchObject({ root: "chat-1", session: a, name: "report.txt", user: true });
    host.pause("chat-2", false);
    expect((await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")).ok.notices).toEqual([]);
  }, 30_000);

  it("stages no download over what a write may carry, and says so", async () => {
    const staged: StagedDownload[] = [];
    host = hostWith({ downloaded: (download) => staged.push(download), downloadBytes: 3 });
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    // Told with the click's answer, or with a later one.
    const told: string[] = (await op(a, "browser.mouse", { action: "click", x: 60, y: 255, button: "left", clicks: 1 })).ok.notices;
    await expect.poll(async () => {
      told.push(...(await op(a, "browser.mouse", { action: "move", x: 1, y: 1 })).ok.notices);
      return told.join(" ");
    }, { timeout: 8_000 }).toBe('The page downloaded "report.txt" (6 bytes), too large to save in the chat\'s folder at once (at most 3 bytes), so it was not saved.');
    expect(staged).toEqual([]);
  });

  it("stages no download that did not finish, and says so", async () => {
    const staged: StagedDownload[] = [];
    host = hostWith({ downloaded: (download) => staged.push(download) });
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    await script(a, "location.href = '/broken.txt'; return 1;");
    const told: string[] = [];
    await expect.poll(async () => {
      told.push(...(await op(a, "browser.mouse", { action: "move", x: 1, y: 1 })).ok.notices);
      return told;
    }, { timeout: 8_000 }).toEqual([notFinished("broken.txt", "canceled")]);
    expect(told[0]).toBe('The page\'s download of "broken.txt" did not finish (canceled), so it was not saved.');
    expect(staged).toEqual([]);
  });

  it("drops a download still on its way when its user takes the browser over, as the operation that started it is interrupted: staged at no hand back, and its agent told once the browser is its again", async () => {
    const staged: StagedDownload[] = [];
    host = hostWith({ downloaded: (download) => staged.push(download) });
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    // The agent's own operation starts it: its first part comes at once, its rest 1.5 s on.
    const [started] = await Promise.all([page.waitForEvent("download", { timeout: 10_000 }), script(a, "location.href = '/slow.bin'; return 1;", "chat-1")]);
    // Its user takes the browser over meanwhile, from another chat: the browser is one for every chat.
    host.pause("chat-2", true);
    // Stopped where it was, at once: not left to end under its user's hand.
    expect(await within(1_000, started.failure())).toBe("canceled");
    // Held, the agent hears nothing: every operation of its is answered paused.
    expect(await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")).toEqual(PAUSED);
    host.pause("chat-2", false);
    // Handed back, its session's next answer says what became of it, once.
    expect((await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")).ok.notices).toEqual([interrupted("slow.bin")]);
    expect(interrupted("slow.bin")).toBe(
      'The page\'s download of "slow.bin" was interrupted when the user took over the agent\'s browser on this computer, so it was not saved.',
    );
    expect((await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")).ok.notices).toEqual([]);
    // Past when it would have ended: never staged.
    await new Promise((done) => setTimeout(done, 2_000));
    expect(staged).toEqual([]);
    // The same download, started again by the agent now that it drives, is staged as its own.
    await script(a, "location.href = '/slow.bin'; return 1;", "chat-1");
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(1);
    expect(staged[0]).toMatchObject({ root: "chat-1", session: a, name: "slow.bin", user: false });
    expect(await readFile(staged[0]!.path, "utf8")).toBe(`${"s".repeat(4_096)}report`);
    // It is the one file in the host's temporary folder: nothing is left there of the one that was stopped.
    expect(readdirSync(dirname(staged[0]!.path))).toEqual([basename(staged[0]!.path)]);
  }, 30_000);

  it("takes a download in a tab no chat owns for its user's while they hold the browser, saved in the chat it is held from; with nobody holding it, or that chat gone, stops it at once, and leaves nothing of any in its temporary folder", async () => {
    const staged: StagedDownload[] = [];
    host = hostWith({ downloaded: (download) => staged.push(download) });
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const context = await (host as unknown as { running: Promise<BrowserContext> }).running;
    // A tab its user opens themselves, as with Ctrl+T, and goes to a site in.
    const own = await context.newPage();
    await own.goto("http://fixture.test/");
    const starts = async (code: string) => (await Promise.all([own.waitForEvent("download", { timeout: 10_000 }), own.evaluate(code)]))[0];
    // Nobody holds the browser: one on its way is stopped at once, and one that came whole is removed.
    expect(await within(2_000, (await starts("void (location.href = '/slow.bin')")).failure())).toBe("canceled");
    await starts("document.getElementById('dl').click()");
    // Held, from the agent's chat: theirs, for that chat, asked there as any of theirs.
    host.pause("chat-1", true);
    await starts("document.getElementById('dl').click()");
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(1);
    expect(staged[0]).toMatchObject({ root: "chat-1", session: "chat-1", name: "report.txt", user: true });
    expect(await readFile(staged[0]!.path, "utf8")).toBe("report");
    // It is the one file in the host's temporary folder: nothing is left there of the two that were stopped.
    await expect.poll(() => readdirSync(dirname(staged[0]!.path)), { timeout: 5_000 }).toEqual([basename(staged[0]!.path)]);
    // The chat it is held from deleted, its tabs closed with it: held still, and no chat is there to save in.
    await host.forget("chat-1");
    expect(await within(2_000, (await starts("void (location.href = '/slow.bin')")).failure())).toBe("canceled");
    await starts("document.getElementById('dl').click()");
    await new Promise((done) => setTimeout(done, 1_000));
    expect(staged).toHaveLength(1);
    expect(readdirSync(dirname(staged[0]!.path))).toEqual([basename(staged[0]!.path)]);
    // No agent was told of any of them.
    expect((host as unknown as { unseen: Map<string, string[]> }).unseen.size).toBe(0);
  }, 40_000);

  it("takes the page a new browser opens with for a tab no chat owns until a session takes it: a download its user makes there while they hold the browser is the holder's chat's", async () => {
    const staged: StagedDownload[] = [];
    host = hostWith({ downloaded: (download) => staged.push(download) });
    const a = session();
    const state = host as unknown as { running: Promise<BrowserContext> | null };
    // The chat's first operation, and its user takes the browser over while it launches: no session takes the page it opens with.
    const first = op(a, "browser.evaluate", { code: "return 1;" }, "chat-1");
    await expect.poll(() => state.running !== null, { timeout: 5_000 }).toBe(true);
    host.pause("chat-1", true);
    expect(await first).toEqual(PAUSED);
    const context = await state.running!;
    await expect.poll(() => context.pages().length, { timeout: 5_000 }).toBe(1);
    expect(tabs().has(a)).toBe(false);
    // Their own hand in that page.
    const spare = context.pages()[0]!;
    await spare.goto("http://fixture.test/");
    await spare.click("#dl");
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(1);
    expect(staged[0]).toMatchObject({ root: "chat-1", session: "chat-1", name: "report.txt", user: true });
    // Handed back, the session takes that page as its tab: a download there is its chat's from then on.
    host.pause("chat-1", false);
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1")).ok).toMatchObject({ title: "Fixture", opened: true });
    expect(tabs().get(a)![0]).toBe(spare);
    await script(a, "location.href = '/report.txt'; return 1;", "chat-1");
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(2);
    expect(staged[1]).toMatchObject({ root: "chat-1", session: a, name: "report.txt", user: false });
  }, 40_000);

  it("keeps a download its user started theirs though it ends after they handed the browser back, with nobody holding it; and through a later take-over", async () => {
    const staged: StagedDownload[] = [];
    host = hostWith({ downloaded: (download) => staged.push(download) });
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const starts = () => Promise.all([page.waitForEvent("download", { timeout: 10_000 }), page.evaluate("void (location.href = '/slow.bin')")]);
    host.pause("chat-1", true);
    // Their own hand in the page they hold: whose a download is goes by when it started.
    await starts();
    // Handed back while it is on its way, and nobody takes the browser over again: it ends while the agent drives.
    host.pause("chat-1", false);
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(1);
    // Theirs outright, by its request: handed on with no mark that the agent is to hear of it.
    expect(staged[0]).toEqual({ root: "chat-1", session: a, name: "slow.bin", path: staged[0]!.path, user: true });
    expect(await readFile(staged[0]!.path, "utf8")).toBe(`${"s".repeat(4_096)}report`);
    expect((await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")).ok.notices).toEqual([]);
    // Another, and taken over again while it is still on its way, from another chat: theirs still, and not stopped as the agent's is.
    host.pause("chat-1", true);
    await starts();
    host.pause("chat-1", false);
    host.pause("chat-2", true);
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(2);
    expect(staged[1]).toMatchObject({ root: "chat-1", session: a, name: "slow.bin", user: true });
    host.pause("chat-2", false);
    expect((await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")).ok.notices).toEqual([]);
  }, 30_000);

  it("takes a link with `download` that its user clicked while they held the browser for theirs though its site answers after the hand back: the browser says no request of it; and the agent's own in the minute after, in doubt, too, each marked as theirs only by that minute", async () => {
    const staged: StagedDownload[] = [];
    // The host's clock, which the test moves on.
    let ahead = 0;
    host = hostWith({ downloaded: (download) => staged.push(download), now: () => Date.now() + ahead });
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    // A link with `download`, as a site's "Export" button: clicked, and answered 1.5 s on.
    const click = "const link = document.createElement('a'); link.href = '/late.bin'; link.download = ''; document.body.append(link); link.click();";
    host.pause("chat-1", true);
    await page.evaluate(`${click} void 0`);
    await new Promise((done) => setTimeout(done, 200));
    host.pause("chat-1", false);
    // Announced with nobody holding the browser, a second after it was handed back.
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(1);
    // Theirs, and only by the minute: what saves it tells the agent what came of it, since it may be the agent's own.
    expect(staged[0]).toEqual({ root: "chat-1", session: a, name: "late.bin", path: staged[0]!.path, user: true, afterHandBack: true });
    // The host itself tells the agent nothing of one it handed on.
    expect((await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")).ok.notices).toEqual([]);
    // The agent's own click on such a link in that minute cannot be told from theirs: asked as theirs, marked the same.
    expect(await script(a, `${click} return 1;`, "chat-1")).toBe(1);
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(2);
    expect(staged[1]).toEqual({ root: "chat-1", session: a, name: "late.bin", path: staged[1]!.path, user: true, afterHandBack: true });
    // More than a minute after the hand back, it is the agent's.
    ahead = AFTER_HAND_BACK_MS + 1;
    expect(await script(a, `${click} return 1;`, "chat-1")).toBe(1);
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(3);
    expect(staged[2]).toEqual({ root: "chat-1", session: a, name: "late.bin", path: staged[2]!.path, user: false });
  }, 40_000);

  it("tells the agent that a download was not saved, and no more, only where it is its user's by the minute after a hand back alone: of one announced while they hold the browser, or whose request began then, nothing, whatever the clock says", async () => {
    const staged: StagedDownload[] = [];
    let ahead = 0;
    // A host that hands no download on: each is over what it may carry, so what the agent hears is the host's to say.
    host = hostWith({ downloaded: (download) => staged.push(download), downloadBytes: 3, now: () => Date.now() + ahead });
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    // What the agent's next answers carry, once the download a test waits for has ended: the page's own hears it too.
    const hears = async (starts: () => Promise<unknown>) => {
      const [download] = await Promise.all([page.waitForEvent("download", { timeout: 10_000 }), starts()]);
      await download.failure();
      // Removed by the host once it had looked at it: by then it has said what it says.
      await expect.poll(() => download.path().then((path) => existsSync(path), () => false), { timeout: 5_000 }).toBe(false);
      return (await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")) as { ok?: { notices: string[] }; error?: unknown };
    };
    const link = () => page.click("#dl");
    // Held: theirs outright. The agent's operations are paused, and once handed back its next answer carries nothing.
    host.pause("chat-1", true);
    expect(await hears(link)).toEqual(PAUSED);
    host.pause("chat-1", false);
    expect((await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")).ok.notices).toEqual([]);
    // In the minute after the hand back, with no request known: it may be the agent's own click, which hears that much.
    expect((await hears(() => script(a, "document.getElementById('dl').click(); return 1;", "chat-1"))).ok?.notices).toEqual([LEFT_TO_USER]);
    // A request begun while they held it, answered in that minute: theirs outright. Nothing.
    host.pause("chat-1", true);
    await page.evaluate("void (location.href = '/late.bin')");
    await new Promise((done) => setTimeout(done, 300));
    host.pause("chat-1", false);
    expect((await hears(() => Promise.resolve())).ok?.notices).toEqual([]);
    // Taken over again within the minute of that hand back: announced while held, it is theirs outright at every moment of the clock.
    host.pause("chat-2", true);
    for (const passed of [0, AFTER_HAND_BACK_MS, 10 * AFTER_HAND_BACK_MS]) {
      ahead += passed;
      expect(await hears(link)).toEqual(PAUSED);
    }
    host.pause("chat-2", false);
    ahead += AFTER_HAND_BACK_MS + 1;
    // Past the minute: nothing was kept of any of those, and the agent's own is told as any of its own.
    expect((await hears(() => script(a, "document.getElementById('dl').click(); return 1;", "chat-1"))).ok?.notices).toEqual([tooLarge("report.txt", 6, 3)]);
    expect(staged).toEqual([]);
  }, 60_000);

  it("takes a download for its user's by when its request began, however it was asked for and however late its site answers: a navigation, a redirect, a form, a new window and a frame, each answered after the hand back and past the minute", async () => {
    const staged: StagedDownload[] = [];
    let ahead = 0;
    host = hostWith({ downloaded: (download) => staged.push(download), now: () => Date.now() + ahead });
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const kinds: Array<[kind: string, code: string, name: string]> = [
      ["a navigation", "void (location.href = '/late.bin#part')", "late.bin"],
      ["a redirect", "void (location.href = '/hop1')", "late.bin"],
      ["a form", "document.body.insertAdjacentHTML('beforeend', '<form id=posts method=post action=/post><input name=a value=1></form>'); document.getElementById('posts').submit()", "posted.bin"],
      ["a new window", "void window.open('/late.bin')", "late.bin"],
      ["a frame", "const frame = document.createElement('iframe'); frame.src = '/hop1'; document.body.append(frame); void 0", "late.bin"],
    ];
    for (const [kind, code, name] of kinds) {
      host.pause("chat-1", true);
      // Their own hand, in the page they hold. Its request is said at once; its site answers 1.5 s on, a
      // redirect's first hop 700 ms on, so that its later requests begin with nobody holding the browser.
      await page.evaluate(code);
      await new Promise((done) => setTimeout(done, 300));
      host.pause("chat-1", false);
      // Past the minute in which one with no request known would still be theirs: this one is theirs by its request.
      ahead += AFTER_HAND_BACK_MS + 1;
      const count = staged.length;
      await expect.poll(() => staged.length, { timeout: 10_000, message: kind }).toBe(count + 1);
      // Theirs by its request, outright: not by the minute, so the agent is told nothing of what comes of it.
      expect(staged.at(-1), kind).toEqual({ root: "chat-1", session: a, name, path: staged.at(-1)!.path, user: true });
    }
    expect((await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")).ok.notices).toEqual([]);
    // Nothing is kept of a request once its download has come.
    expect((host as unknown as { open: Map<unknown, unknown> }).open.size).toBe(0);
  }, 60_000);

  it("drops a download of the agent's whose request began before its user took the browser over and whose site answers while they hold it, or after they held it meanwhile", async () => {
    const staged: StagedDownload[] = [];
    host = hostWith({ downloaded: (download) => staged.push(download) });
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    // The agent's own navigation, answered 1.5 s on: its user takes the browser over before the browser has announced anything.
    expect(await script(a, "location.href = '/late.bin'; return 1;", "chat-1")).toBe(1);
    await new Promise((done) => setTimeout(done, 300));
    host.pause("chat-2", true);
    await new Promise((done) => setTimeout(done, 2_500));
    host.pause("chat-2", false);
    expect((await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")).ok.notices).toEqual([interrupted("late.bin")]);
    // Taken over and handed back before its site answers: stopped by that take-over all the same.
    expect(await script(a, "location.href = '/late.bin'; return 1;", "chat-1")).toBe(1);
    await new Promise((done) => setTimeout(done, 300));
    host.pause("chat-2", true);
    host.pause("chat-2", false);
    const told: string[] = [];
    await expect.poll(async () => {
      told.push(...(await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")).ok.notices);
      return told;
    }, { timeout: 8_000 }).toEqual([interrupted("late.bin")]);
    expect(staged).toEqual([]);
  }, 40_000);

  it("takes a popup a session's tab opens as the session's: its operations act there, and its close closes both", async () => {
    const [a, b] = [session(), session()];
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    await op(b, "browser.navigate", { url: "http://fixture.test/second" });
    await op(a, "browser.mouse", { action: "click", x: 50, y: 155, button: "left", clicks: 1 });
    await expect.poll(() => script(a, "return document.title;")).toBe("Second");
    expect(await pages()).toBe(3);
    expect(await op(a, "browser.close")).toEqual({ ok: { closed: true } });
    expect(await pages()).toBe(1);
    expect(await script(b, "return document.title;")).toBe("Second");
  });

  it("forgets each popup of a session's once it has closed", async () => {
    const a = session();
    const kept = () => (host as unknown as { tabs: Map<string, unknown[]> }).tabs.get(a)?.length;
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    for (let round = 0; round < 3; round += 1) {
      await op(a, "browser.mouse", { action: "click", x: 50, y: 155, button: "left", clicks: 1 });
      await expect.poll(kept).toBe(2);
      await script(a, "setTimeout(() => window.close(), 0); return 1;");
      await expect.poll(kept).toBe(1);
    }
    expect(await script(a, "return document.title;")).toBe("Fixture");
  });

  it("launches again after its user closed it, in new tabs", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    for (const { pid } of processes().filter(({ args }) => !args.some((arg) => arg.startsWith("--type=")))) process.kill(Number(pid), "SIGTERM");
    await expect.poll(() => processes().length, { timeout: 10_000 }).toBe(0);
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/second" })).ok).toMatchObject({ title: "Second", opened: true });
  });

  it("answers a script's value under value, whatever its shape, a transfer's too", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    const shaped = { transfer: { size: 5, sha256: "a".repeat(64) } };
    expect(await op(a, "browser.evaluate", { code: `return ${JSON.stringify(shaped)};` })).toEqual({ ok: { value: shaped } });
    expect(await op(a, "browser.evaluate", { code: "document.title = 'x';" })).toEqual({ ok: { value: null } });
    // One too large for the link is measured in the page and refused there, before it crosses.
    const large = await op(a, "browser.evaluate", { code: "return 'x'.repeat(3 * 1024 * 1024);" });
    expect(large.ok === undefined ? large : `a value of ${JSON.stringify(large.ok).length} characters`).toEqual({
      error: { type: "browser", message: "The script's value is too large to send: 3145730 characters, at most 2097152. Return less of it." },
    });
  });

  it("closes a page that does not answer in time, and a close does not wait behind one", async () => {
    await host.close();
    host = hostWith({ boundMs: 2_000 });
    const [a, b] = [session(), session()];
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    await op(b, "browser.navigate", { url: "http://fixture.test/second" });
    // An endless script: its page is closed at the bound, and the session's next operation opens a tab again.
    const started = performance.now();
    expect(await op(a, "browser.evaluate", { code: "while (true) {}" })).toEqual({
      error: { type: "browser", message: "The page did not answer within 2 s, so it was closed" },
    });
    expect(performance.now() - started).toBeLessThan(5_000);
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/" })).ok?.opened).toBe(true);
    // A close goes past the one stuck in its line.
    const stuck = op(a, "browser.evaluate", { code: "while (true) {}" });
    await new Promise((done) => setTimeout(done, 300));
    const closing = performance.now();
    expect(await op(a, "browser.close")).toEqual({ ok: { closed: true } });
    expect(performance.now() - closing).toBeLessThan(1_500);
    expect((await stuck).error?.type).toBe("browser");
    expect(await script(b, "return document.title;")).toBe("Second");
    // The browser goes with its last tab, and the next operation launches it again.
    expect(await op(b, "browser.close")).toEqual({ ok: { closed: true } });
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/" })).ok?.opened).toBe(true);
  });

  it("opens a tab in a fresh browser for an operation that comes while the browser closes with its last tab", async () => {
    const [a, b] = [session(), session()];
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    // Another chat's first tab, asked for with the close of the only one.
    expect(await within(10_000, Promise.all([op(b, "browser.navigate", { url: "http://fixture.test/second" }, "chat-2"), op(a, "browser.close", {}, "chat-1")]))).toEqual([
      { ok: { url: "http://fixture.test/second", title: "Second", opened: true, notices: [] } },
      { ok: { closed: true } },
    ]);
    // The closing session's own next operation, sent without waiting for its close.
    expect(await within(10_000, Promise.all([op(b, "browser.close", {}, "chat-2"), op(b, "browser.navigate", { url: "http://fixture.test/" }, "chat-2")]))).toEqual([
      { ok: { closed: true } },
      { ok: { url: "http://fixture.test/", title: "Fixture", opened: true, notices: [] } },
    ]);
    expect(await pages()).toBe(1);
  }, 40_000);

  it("closes a page that holds its main thread after it loaded, so a navigation answers within the bound too", async () => {
    await host.close();
    host = hostWith({ boundMs: 2_000 });
    const a = session();
    const started = performance.now();
    expect(await op(a, "browser.navigate", { url: "http://fixture.test/hang" })).toEqual({
      error: { type: "browser", message: "The page did not answer within 2 s, so it was closed" },
    });
    expect(performance.now() - started).toBeLessThan(5_000);
    // The session's next operation runs, in a tab of its own again.
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/second" })).ok).toMatchObject({ title: "Second", opened: true });
  });

  it("closes a page stuck in its own code under a labelled shot, so the shot answers within the bound too", async () => {
    await host.close();
    host = hostWith({ boundMs: 2_000 });
    const a = session();
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/late-hang" })).ok?.title).toBe("LateHang");
    await new Promise((done) => setTimeout(done, 2_000));
    const started = performance.now();
    expect(await within(8_000, op(a, "browser.screenshot", { clip: null, labels: [{ label: 1, x: 10, y: 10 }] }))).toEqual({
      error: { type: "browser", message: "The page did not answer within 2 s, so it was closed" },
    });
    expect(performance.now() - started).toBeLessThan(5_000);
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/second" })).ok).toMatchObject({ title: "Second", opened: true });
  }, 20_000);

  it("lets no service worker answer the agent's pages, one registered through the prototype's own register too", async () => {
    await host.close();
    // fixture.test as a secure origin, as an https site is: it may have service workers.
    host = hostWith({ args: ["--unsafely-treat-insecure-origin-as-secure=http://fixture.test"] });
    const [a, b] = [session(), session()];
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    await script(a, `await ServiceWorkerContainer.prototype.register.call(navigator.serviceWorker, "/sw.js");
await navigator.serviceWorker.ready;`);
    // Neither the page that registered it nor another chat's tab on its origin is answered by it.
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/second" })).ok?.title).toBe("Second");
    expect((await op(b, "browser.navigate", { url: "http://fixture.test/second" })).ok?.title).toBe("Second");
    expect(await script(b, "return (await fetch('/second')).text();")).toBe("<title>Second</title>");
  });

  it("lets no service worker answer the first load of a popup a session's tab opens", async () => {
    await host.close();
    host = hostWith({ args: ["--unsafely-treat-insecure-origin-as-secure=http://fixture.test"] });
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    await script(a, `await ServiceWorkerContainer.prototype.register.call(navigator.serviceWorker, "/sw.js");
await navigator.serviceWorker.ready;`);
    await op(a, "browser.mouse", { action: "click", x: 50, y: 155, button: "left", clicks: 1 });
    await expect.poll(() => script(a, "return location.pathname;")).toBe("/second");
    expect(await script(a, "return document.title;")).toBe("Second");
  });

  it("lets no service worker answer a cross-site frame's own navigations, in the chat whose frame registered it or another", async () => {
    await host.close();
    host = hostWith({ args: ["--unsafely-treat-insecure-origin-as-secure=http://fixture.test,http://other.test"] });
    const [a, b] = [session(), session()];
    await op(a, "browser.navigate", { url: "http://other.test/embed?register" });
    await expect.poll(() => script(a, "return document.title;"), { timeout: 10_000 }).toBe("registered /frame?register");
    for (const chat of [b, a]) {
      framed = [];
      await op(chat, "browser.navigate", { url: "http://other.test/embed" });
      await expect.poll(() => script(chat, "return document.title;"), { timeout: 10_000 }).toMatch(/again/);
      expect(await script(chat, "return document.title;")).toBe("frame again /frame?again");
      expect(framed).toEqual(["/frame", "/frame?again"]);
    }
  }, 40_000);

  it("refuses a browser whose requests do not come through its proxy, as a policy can make it, and leaves it closed", async () => {
    await host.close();
    // As a managed policy would: the browser takes its proxy settings from elsewhere.
    host = hostWith({ args: ["--no-proxy-server"] });
    expect(await op(session(), "browser.navigate", { url: "http://fixture.test/" })).toEqual({ error: { type: "browser", message: PROXY_BYPASSED } });
    await expect.poll(() => processes().length, { timeout: 10_000 }).toBe(0);
  });

  it("opens no tab for a deleted chat's operation that waited in its line behind another", async () => {
    const [a, b] = [session(), session()];
    await op(b, "browser.navigate", { url: "http://fixture.test/second" }, "chat-2");
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const first = op(a, "browser.evaluate", { code: "await new Promise((done) => setTimeout(done, 1500)); return 1;" }, "chat-1");
    const queued = op(a, "browser.navigate", { url: "http://fixture.test/second" }, "chat-1");
    await new Promise((done) => setTimeout(done, 300));
    await host.forget("chat-1");
    expect((await first).error?.type).toBe("browser");
    expect((await queued).error?.type).toBe("browser");
    // Only the other chat's tab is left, without forgetting the chat again.
    expect(await pages()).toBe(1);
    expect(await script(b, "return document.title;", "chat-2")).toBe("Second");
  });

  it("keeps a chat's tab opened after its browser closed under it the chat's, for the chat's deletion to close", async () => {
    const [a, b] = [session(), session()];
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const first = op(a, "browser.evaluate", { code: "await new Promise((done) => setTimeout(done, 3000)); return 1;" }, "chat-1");
    const queued = op(a, "browser.navigate", { url: "http://fixture.test/second" }, "chat-1");
    await new Promise((done) => setTimeout(done, 300));
    // Its user closes the browser while the chat's operation runs; the next one opens a tab in another.
    for (const { pid } of processes().filter(({ args }) => !args.some((arg) => arg.startsWith("--type=")))) process.kill(Number(pid), "SIGTERM");
    expect((await first).error?.type).toBe("browser");
    expect((await queued).ok).toMatchObject({ title: "Second", opened: true });
    await op(b, "browser.navigate", { url: "http://fixture.test/second" }, "chat-2");
    expect(await pages()).toBe(2);
    await host.forget("chat-1");
    expect(await pages()).toBe(1);
    expect(await script(b, "return document.title;", "chat-2")).toBe("Second");
  }, 30_000);

  it("tries a picked browser with its socket in the user's runtime folder, however deep the try's temp folder, and leaves nothing of it there", async () => {
    // As the app gives a try's host its temp folder: <data>/surogate/browser-profiles/tmp, under a home of 40 characters.
    const home = join(tmpdir(), "a-home-of-forty-characters-for-a-long-na");
    const deep = join(home, ".local", "share", "surogate", "browser-profiles", "tmp");
    mkdirSync(deep, { recursive: true });
    const runtime = process.env.XDG_RUNTIME_DIR!;
    const there = readdirSync(runtime);
    const before = process.env.TMPDIR;
    process.env.TMPDIR = deep;
    try {
      expect(await host.tryBrowser(EXECUTABLE!)).toMatchObject({ ok: { version: expect.stringMatching(/^\d+\./) } });
      expect(await host.tryBrowser("/bin/true")).toMatchObject({ error: { type: "browser" } });
    } finally {
      process.env.TMPDIR = before;
    }
    // Neither try leaves its browser's folder behind, in the temp folder or the runtime one.
    expect(readdirSync(deep).filter((name) => /com\./.test(name))).toEqual([]);
    expect(readdirSync(runtime).filter((name) => !there.includes(name) && /com\./.test(name))).toEqual([]);
    rmSync(home, { recursive: true, force: true });
  }, 60_000);

  it("answers a try of a picked program that launches as a browser but does not close, within its bound", async () => {
    const folder = mkdtempSync(join(tmpdir(), "sb-picked-"));
    const picked = join(folder, "lingering-browser");
    // The browser, and then the program goes on after it, holding the browser's pipe.
    writeFileSync(picked, `#!/bin/sh\n"${EXECUTABLE}" "$@"\nsleep 600\n`, { mode: 0o755 });
    try {
      const started = performance.now();
      expect(await within(20_000, host.tryBrowser(picked))).toMatchObject({ ok: { version: expect.stringMatching(/^\d+\./) } });
      expect(performance.now() - started).toBeLessThan(15_000);
    } finally {
      // The program, and its sleep with it: the process group it leads.
      for (const pid of readdirSync("/proc").filter((entry) => /^\d+$/.test(entry))) {
        try {
          if (readFileSync(`/proc/${pid}/cmdline`, "utf8").includes(picked)) process.kill(-Number(pid), "SIGKILL");
        } catch {
          // Gone, or not its.
        }
      }
      rmSync(folder, { recursive: true, force: true });
    }
  }, 30_000);

  it("closes its browser only once nothing holds its profile, a process that would not go killed", async () => {
    await op(session(), "browser.navigate", { url: "http://fixture.test/" });
    // As a process of the browser's that outlives it, still writing the profile.
    const lingering = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)", join(profile, "lingering")], { stdio: "ignore" });
    const gone = new Promise<string | null>((done) => lingering.once("exit", (_code, signal) => done(signal)));
    try {
      await host.close();
      expect(await within(500, gone)).toBe("SIGKILL");
      expect(processes()).toEqual([]);
    } finally {
      lingering.kill("SIGKILL");
    }
  }, 30_000);

  it("answers every chat's operations paused while a chat's user holds the browser, one waiting in its line too; handed back by that chat, they run", async () => {
    const [a, b] = [session(), session()];
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    await op(b, "browser.navigate", { url: "http://fixture.test/second" }, "chat-2");
    // One that holds its page a moment, and one waiting behind it in the session's line.
    const holding = op(a, "browser.evaluate", { code: "await new Promise((done) => setTimeout(done, 1000)); return 1;" }, "chat-1");
    const waiting = op(a, "browser.evaluate", { code: "return document.title;" }, "chat-1");
    await new Promise((done) => setTimeout(done, 300));
    host.pause("chat-1", true);
    // What was acting is answered paused, with nothing it read after; what waited does nothing.
    expect(await holding).toEqual(PAUSED);
    expect(await waiting).toEqual(PAUSED);
    expect(await op(a, "browser.close", {}, "chat-1")).toEqual(PAUSED);
    // Another chat's, which never asked for the take-over: the browser its user holds is the agent's one browser.
    expect(await op(b, "browser.evaluate", { code: "return document.title;" }, "chat-2")).toEqual(PAUSED);
    expect(await op(b, "browser.close", {}, "chat-2")).toEqual(PAUSED);
    expect(await pages()).toBe(2);
    // Only the chat that took it over hands it back.
    host.pause("chat-2", false);
    expect(await op(b, "browser.evaluate", { code: "return document.title;" }, "chat-2")).toEqual(PAUSED);
    host.pause("chat-1", false);
    expect((await op(a, "browser.evaluate", { code: "return document.title;" }, "chat-1")).ok?.value).toBe("Fixture");
    expect((await op(b, "browser.evaluate", { code: "return document.title;" }, "chat-2")).ok?.value).toBe("Second");
  }, 30_000);

  it("opens nothing and brings nothing to the front for another chat while its user holds the browser, and their keys go on landing in the page they hold", async () => {
    const [a, b, c] = [session(), session(), session()];
    await op(a, "browser.navigate", { url: "http://fixture.test/t/HELD" }, "chat-1");
    await op(b, "browser.navigate", { url: "http://fixture.test/t/OTHER" }, "chat-2");
    const held = tabs().get(a)![0]!;
    host.pause("chat-1", true);
    expect(await host.show("chat-1")).toBe(true);
    await expect.poll(front, { timeout: 5_000 }).toBe("HELD");
    // Another chat's navigation, a script of its that would open a window, and the first operation of a
    // session of its that has no tab yet.
    expect(await op(b, "browser.navigate", { url: "http://fixture.test/t/MOVED" }, "chat-2")).toEqual(PAUSED);
    expect(await op(b, "browser.evaluate", { code: "window.open('http://fixture.test/t/POPUP'); return 1;" }, "chat-2")).toEqual(PAUSED);
    expect(await op(c, "browser.navigate", { url: "http://fixture.test/t/THIRD" }, "chat-2")).toEqual(PAUSED);
    expect(await pages()).toBe(2);
    expect(tabs().get(b)![0]!.url()).toBe("http://fixture.test/t/OTHER");
    expect(tabs().has(c)).toBe(false);
    // The user, at the page they hold: a click into its field, then keys.
    const at = await onScreen(held, "name");
    asUser("focus", xwindow()!.id);
    asUser("click", String(at[0]), String(at[1]));
    asUser("type", "abc");
    await expect.poll(() => held.evaluate(() => (document.getElementById("name") as HTMLInputElement).value), { timeout: 5_000 }).toBe("abc");
    await expect.poll(front, { timeout: 5_000 }).toBe("HELD");
  }, 30_000);

  it("opens a session's tab behind the page in front", async () => {
    const [a, b] = [session(), session()];
    await op(a, "browser.navigate", { url: "http://fixture.test/t/FIRST" }, "chat-1");
    await expect.poll(front, { timeout: 5_000 }).toBe("FIRST");
    // Another session's first operation opens its tab and loads its page: neither takes the front.
    expect((await op(b, "browser.navigate", { url: "http://fixture.test/t/SECOND" }, "chat-2")).ok).toMatchObject({ title: "SECOND", opened: true });
    expect(await pages()).toBe(2);
    // A tab that took the front has renamed the window by now.
    await new Promise((done) => setTimeout(done, 500));
    await expect.poll(front, { timeout: 5_000 }).toBe("FIRST");
    // It is the agent's all the same: its own keys reach it.
    await op(b, "browser.keyboard", { action: "type", text: "typed behind", at: { x: 60, y: 110 }, delay: 0 }, "chat-2");
    expect(await script(b, "return document.getElementById('name').value;", "chat-2")).toBe("typed behind");
    expect(await script(a, "return document.getElementById('name').value;", "chat-1")).toBe("");
  }, 30_000);

  it("runs nothing in the page, and takes no tab, for an operation whose browser was still launching when its user took the browser over", async () => {
    const a = session();
    const state = host as unknown as { running: Promise<BrowserContext> | null; live: BrowserContext | null };
    // The chat's first operation: its turn has come, and the browser launches for it.
    const first = op(a, "browser.evaluate", { code: "document.title = 'ran after the pause'; return 1;" }, "chat-1");
    await expect.poll(() => state.running !== null, { timeout: 5_000 }).toBe(true);
    // Not launched yet: the operation has no page to act in.
    expect(state.live).toBeNull();
    host.pause("chat-1", true);
    // Answered at once, not when the launch has ended: the browser is not up yet.
    expect(await first).toEqual(PAUSED);
    expect(state.live).toBeNull();
    // Then it comes up, with the one page a browser opens with: no session's, and none beside it. Read a
    // moment after the launch has ended: in the turn it ends, the host has not yet gone on to take a page.
    await state.running;
    await new Promise((done) => setTimeout(done, 300));
    expect(tabs().has(a)).toBe(false);
    expect(await host.show("chat-1")).toBe(false);
    expect(await pages()).toBe(1);
    host.pause("chat-1", false);
    // Its next navigation is the one that opens its tab, and says so: its pane hears of the browser then.
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1")).ok).toMatchObject({ title: "Fixture", opened: true });
    expect(await pages()).toBe(1);
  }, 30_000);

  it("leaves no tab behind for an operation whose tab was opening when its user took the browser over", async () => {
    const [other, a] = [session(), session()];
    await op(other, "browser.navigate", { url: "http://fixture.test/second" }, "chat-2");
    const context = await (host as unknown as { running: Promise<BrowserContext> }).running;
    // Its user takes the browser over at the moment the session's tab has opened, before the host has taken it for the session.
    context.once("page", () => host.pause("chat-1", true));
    expect(await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1")).toEqual(PAUSED);
    // The tab is closed again, and is no session's.
    await expect.poll(pages, { timeout: 5_000 }).toBe(1);
    expect(tabs().has(a)).toBe(false);
    expect(await host.show("chat-1")).toBe(false);
    host.pause("chat-1", false);
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1")).ok).toMatchObject({ title: "Fixture", opened: true });
    expect(await pages()).toBe(2);
  }, 30_000);

  it("opens no tab, not for a moment, for an operation whose turn comes while its user holds the browser, though its session's tab is gone", async () => {
    const [other, a] = [session(), session()];
    await op(other, "browser.navigate", { url: "http://fixture.test/second" }, "chat-2");
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const context = await (host as unknown as { running: Promise<BrowserContext> }).running;
    host.pause("chat-1", true);
    // Its user, holding the browser, closes the session's tab.
    await tabs().get(a)![0]!.close();
    const made: Page[] = [];
    context.on("page", (page) => made.push(page));
    expect(await op(a, "browser.evaluate", { code: "return 1;" }, "chat-1")).toEqual(PAUSED);
    expect(await op(session(), "browser.navigate", { url: "http://fixture.test/" }, "chat-2")).toEqual(PAUSED);
    expect(made).toEqual([]);
    expect(await pages()).toBe(1);
    expect(tabs().get(a) ?? []).toEqual([]);
  }, 30_000);

  it("runs nothing in the page for an operation still waiting behind another when its user took the browser over", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    // Its tab is there. One that holds its page a moment, and one whose turn has not come.
    const held = op(a, "browser.evaluate", { code: "await new Promise((done) => setTimeout(done, 1000)); return 1;" }, "chat-1");
    const queued = op(a, "browser.evaluate", { code: "document.title = 'ran after the pause'; return 2;" }, "chat-1");
    await new Promise((done) => setTimeout(done, 300));
    host.pause("chat-1", true);
    expect(await held).toEqual(PAUSED);
    expect(await queued).toEqual(PAUSED);
    host.pause("chat-1", false);
    expect(await script(a, "return document.title;", "chat-1")).toBe("Fixture");
  }, 30_000);

  it("does nothing in a page for an operation that was waiting to see whether the page answers when its user took the browser over", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/t/HELD" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const asking = (host as unknown as { asking: Set<Page> }).asking;
    // A page whose question its user left open, as its agent's next operation finds it: here one only slow
    // to answer, for a quarter of a second, which the host waits out.
    asking.add(page);
    void page.evaluate("(() => { const until = Date.now() + 250; while (Date.now() < until) {} })()").catch(() => {});
    const going = op(a, "browser.navigate", { url: "http://fixture.test/second" }, "chat-1");
    // Its user takes the browser over while the operation waits.
    await new Promise((done) => setTimeout(done, 100));
    host.pause("chat-1", true);
    expect(await within(1_000, going)).toEqual(PAUSED);
    // The page answered after that, as the host saw: nothing is done in it, and it is not left.
    await new Promise((done) => setTimeout(done, 1_500));
    expect(asking.has(page)).toBe(false);
    expect(page.url()).toBe("http://fixture.test/t/HELD");
    expect(await page.title()).toBe("HELD");
  }, 30_000);

  it("presses nothing for a drag whose pointer was on its way to its start when its user took the browser over", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    await page.evaluate(() => {
      const seen = { downs: 0, ups: 0, clicks: 0 };
      Object.assign(window, { seen });
      addEventListener("mousedown", () => (seen.downs += 1));
      addEventListener("mouseup", () => (seen.ups += 1));
      addEventListener("click", () => (seen.clicks += 1));
    });
    // Taken over as the pointer reaches the drag's start, before its button goes down.
    const move = page.mouse.move.bind(page.mouse);
    page.mouse.move = async (...args: Parameters<typeof move>) => {
      await move(...args);
      host.pause("chat-1", true);
    };
    // Over the fixture's button: a press and a release there would be a click of it.
    expect(await within(2_000, op(a, "browser.mouse", { action: "drag", path: [[60, 55], [70, 55], [80, 55]], button: "left" }, "chat-1"))).toEqual(PAUSED);
    await new Promise((done) => setTimeout(done, 500));
    expect(await page.evaluate(() => (window as unknown as { seen: unknown }).seen)).toEqual({ downs: 0, ups: 0, clicks: 0 });
    expect(await page.title()).toBe("Fixture");
  }, 30_000);

  it("types not one more character once its user took the browser over", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const typed = () => page.evaluate(() => (document.getElementById("name") as HTMLInputElement).value);
    await page.evaluate(() => document.getElementById("name")!.focus());
    const text = "abcdefghijklmnopqrstuvwxyz0123";
    const typing = op(a, "browser.keyboard", { action: "type", text, at: null, delay: 200 }, "chat-1");
    await expect.poll(async () => (await typed()).length, { timeout: 10_000 }).toBeGreaterThanOrEqual(3);
    host.pause("chat-1", true);
    // Answered at once, not when the last character would have come.
    expect(await within(1_000, typing)).toEqual(PAUSED);
    // The key that was down comes up; then nothing of the agent's follows, whatever its user does in the page.
    await new Promise((done) => setTimeout(done, 300));
    const atPause = await typed();
    expect(text.startsWith(atPause) && atPause.length < 10, atPause).toBe(true);
    await new Promise((done) => setTimeout(done, 1_500));
    expect(await typed()).toBe(atPause);
  }, 30_000);

  it("moves a drag no further once its user took the browser over, and lets its button go", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    await page.evaluate(() => {
      const seen = { moves: 0, ups: 0 };
      Object.assign(window, { seen });
      addEventListener("mousemove", () => (seen.moves += 1));
      addEventListener("mouseup", () => (seen.ups += 1));
    });
    const seen = () => page.evaluate(() => (window as unknown as { seen: { moves: number; ups: number } }).seen);
    const path = Array.from({ length: 1_000 }, (_, n) => [20 + (n % 500), 20 + Math.floor(n / 4)]);
    const dragging = op(a, "browser.mouse", { action: "drag", path, button: "left" }, "chat-1");
    await expect.poll(async () => (await seen()).moves, { timeout: 10_000 }).toBeGreaterThanOrEqual(2);
    host.pause("chat-1", true);
    expect(await within(1_000, dragging)).toEqual(PAUSED);
    // The move that was under way ends, and the button comes up where the pointer is: then nothing more.
    await expect.poll(async () => (await seen()).ups, { timeout: 5_000 }).toBe(1);
    const atPause = await seen();
    expect(atPause.moves).toBeLessThan(900);
    await new Promise((done) => setTimeout(done, 1_000));
    expect(await seen()).toEqual(atPause);
  }, 30_000);

  it("lets go a button its agent pressed and holds down when its user takes the browser over, from whichever chat, and presses none whose pointer was on its way", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    await page.evaluate(() => {
      const seen = { downs: 0, ups: 0 };
      Object.assign(window, { seen });
      addEventListener("mousedown", () => (seen.downs += 1));
      addEventListener("mouseup", () => (seen.ups += 1));
    });
    const seen = () => page.evaluate(() => (window as unknown as { seen: { downs: number; ups: number } }).seen);
    // Pressed by a `down` of the agent's, whose `up` is its next call, a model's turn away.
    expect(await op(a, "browser.mouse", { action: "down", x: 300, y: 300, button: "left" }, "chat-1")).toMatchObject({ ok: {} });
    expect(await seen()).toEqual({ downs: 1, ups: 0 });
    // Taken over from another chat: the browser is one, and the button comes up where it is, not left held under its user's hand.
    host.pause("chat-2", true);
    await expect.poll(seen, { timeout: 5_000 }).toEqual({ downs: 1, ups: 1 });
    host.pause("chat-2", false);
    // A `down` whose pointer was on its way when its user took the browser over: the button does not go down.
    const move = page.mouse.move.bind(page.mouse);
    page.mouse.move = async (...args: Parameters<typeof move>) => {
      await move(...args);
      host.pause("chat-1", true);
    };
    expect(await within(2_000, op(a, "browser.mouse", { action: "down", x: 320, y: 300, button: "left" }, "chat-1"))).toEqual(PAUSED);
    await new Promise((done) => setTimeout(done, 500));
    expect(await seen()).toEqual({ downs: 1, ups: 1 });
    host.pause("chat-1", false);
    page.mouse.move = move;
    // And one that went down just as they took it over comes up again.
    const press = page.mouse.down.bind(page.mouse);
    page.mouse.down = async (...args: Parameters<typeof press>) => {
      await press(...args);
      host.pause("chat-1", true);
    };
    expect(await within(2_000, op(a, "browser.mouse", { action: "down", x: 340, y: 300, button: "left" }, "chat-1"))).toEqual(PAUSED);
    await expect.poll(seen, { timeout: 5_000 }).toEqual({ downs: 2, ups: 2 });
  }, 30_000);

  it("answers an operation in flight paused at once, and gives the agent nothing it read after its user took the browser over", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    // A script that reads the page's field a moment on: by then its user has typed there.
    const reading = op(a, "browser.evaluate", { code: "await new Promise((done) => setTimeout(done, 1500)); return document.getElementById('name').value;" }, "chat-1");
    await new Promise((done) => setTimeout(done, 300));
    host.pause("chat-1", true);
    expect(await within(1_000, reading)).toEqual(PAUSED);
    await page.evaluate(() => {
      (document.getElementById("name") as HTMLInputElement).value = "typed by its user";
    });
    host.pause("chat-1", false);
    // An outline and a shot asked of a page too busy to answer before the take-over: neither is given once it can.
    for (const [kind, args] of [["browser.observe", { script: "snapshot@1", params: {} }], ["browser.screenshot", { clip: null, labels: [] }]] as const) {
      void page.evaluate("const until = Date.now() + 1500; while (Date.now() < until) {}").catch(() => {});
      const asked = op(a, kind, args, "chat-1");
      await new Promise((done) => setTimeout(done, 300));
      host.pause("chat-1", true);
      expect(await within(1_000, asked), kind).toEqual(PAUSED);
      host.pause("chat-1", false);
      // The page answers again before the next is asked of it.
      await expect.poll(() => within(500, page.evaluate("1")), { timeout: 10_000 }).toBe(1);
    }
  }, 30_000);

  it("keeps what a page did for its session's next answer when the operation that would have told of it was answered paused", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const unseen = (host as unknown as { unseen: Map<string, string[]> }).unseen;
    // An operation whose answer carries notices, long enough to be acting still when the browser is taken over.
    const typing = op(a, "browser.keyboard", { action: "type", text: "abcdefgh", at: null, delay: 200 }, "chat-1");
    // Meanwhile the page asks for a file.
    await page.click("#file");
    await expect.poll(() => unseen.get(a), { timeout: 5_000 }).toEqual([FILE_ASKED]);
    host.pause("chat-1", true);
    expect(await within(1_000, typing)).toEqual(PAUSED);
    // What is left of the operation ends, its answer given to no one.
    await new Promise((done) => setTimeout(done, 500));
    host.pause("chat-1", false);
    expect((await op(a, "browser.mouse", { action: "move", x: 5, y: 5 }, "chat-1")).ok.notices).toEqual([FILE_ASKED]);
  }, 30_000);

  it("keeps nothing of an operation, once it has answered, on what a take-over would stop it by", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    // One signal for every operation until the browser is next taken over, which may be never in a run of the app.
    const stops = () => getEventListeners((host as unknown as { interrupt: AbortController }).interrupt.signal, "abort");
    for (let n = 0; n < 20; n += 1) expect(await script(a, `return ${n};`, "chat-1")).toBe(n);
    await op(a, "browser.navigate", { url: "http://fixture.test/second" }, "chat-1");
    expect(stops()).toHaveLength(0);
  }, 30_000);

  it("tells the agent nothing of a file asked for, or a download started, while its user held the browser", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    host.pause("chat-1", true);
    // Its user's own hand in the page they hold: its file input, then its download.
    const [asked] = await Promise.all([page.waitForEvent("filechooser", { timeout: 10_000 }), page.click("#file")]);
    const [started] = await Promise.all([page.waitForEvent("download", { timeout: 10_000 }), page.click("#dl")]);
    expect([asked.isMultiple(), started.suggestedFilename()]).toEqual([false, "report.txt"]);
    host.pause("chat-1", false);
    // Handed back, its agent's next answer carries neither.
    expect((await op(a, "browser.mouse", { action: "move", x: 5, y: 5 }, "chat-1")).ok.notices).toEqual([]);
    // What the page does once the agent drives again is told as before.
    expect((await op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 }, "chat-1")).ok.notices).toEqual([FILE_ASKED]);
  }, 30_000);

  it("leaves a page's own question open for its user while they hold the browser: nobody answers it for them, and their own answer reaches the page", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/t/HELD" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const answered = () => within(500, page.evaluate("window.answered"));
    // While the agent drives, a page's question is answered at once, unseen, as before: left open it would hold the page.
    expect(await within(5_000, script(a, "return [confirm('Leave?'), prompt('Why?')];", "chat-1"))).toEqual([false, null]);
    // A question the page asks by itself a moment on: by then its user holds the browser.
    await script(a, "setTimeout(() => { window.answered = confirm('Pay?'); }, 300); return 1;", "chat-1");
    host.pause("chat-1", true);
    expect(await host.show("chat-1")).toBe(true);
    // Open still, 2 s on: the page waits on it, and nobody has answered it for them.
    await new Promise((done) => setTimeout(done, 2_300));
    expect(await answered()).toBe("late");
    // Their own accept, at their keyboard, reaches the page.
    asUser("focus", xwindow()!.id);
    asUser("press", "Return");
    await expect.poll(answered, { timeout: 5_000 }).toBe(true);
    // The agent is told nothing of it.
    host.pause("chat-1", false);
    expect((await op(a, "browser.mouse", { action: "move", x: 5, y: 5 }, "chat-1")).ok.notices).toEqual([]);
    expect(await script(a, "return window.answered;", "chat-1")).toBe(true);
  }, 30_000);

  it("refuses the agent a page whose question its user left open at the hand back, in words it can read, until they have answered it", async () => {
    expect(ASKING).toEqual({
      error: {
        type: "browser",
        message: "The page asked its user a question while they held the browser, and it is still open. It is theirs to answer, in the agent's browser on this computer: nothing is done in this page until they have.",
      },
    });
    const [a, b] = [session(), session()];
    await op(a, "browser.navigate", { url: "http://fixture.test/t/HELD" }, "chat-1");
    await op(b, "browser.navigate", { url: "http://fixture.test/second" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const answered = () => within(500, page.evaluate("window.answered"));
    await script(a, "setTimeout(() => { window.answered = confirm('Pay?'); }, 300); return 1;", "chat-1");
    host.pause("chat-1", true);
    await new Promise((done) => setTimeout(done, 1_000));
    expect(await answered()).toBe("late");
    // Handed back with it open: left as its user left it, and each operation in that page refused at once, not left to hang.
    host.pause("chat-1", false);
    for (const [kind, args] of [
      ["browser.evaluate", { code: "return 1;" }], ["browser.navigate", { url: "http://fixture.test/second" }],
      ["browser.mouse", { action: "click", x: 60, y: 110, button: "left", clicks: 1 }], ["browser.screenshot", { clip: null, labels: [] }],
    ] as const) {
      expect(await within(2_000, op(a, kind, args, "chat-1")), kind).toEqual(ASKING);
    }
    expect(await answered()).toBe("late");
    expect(page.url()).toBe("http://fixture.test/t/HELD");
    // Another page of the chat's is the agent's as ever.
    expect(await script(b, "return document.title;", "chat-1")).toBe("Second");
    // Its user answers it, here with a no: the page is the agent's again, and reads what they answered.
    asUser("focus", xwindow()!.id);
    asUser("press", "Escape");
    await expect.poll(async () => (await op(a, "browser.evaluate", { code: "return window.answered;" }, "chat-1")).ok?.value, { timeout: 10_000 }).toBe(false);
    const asking = (host as unknown as { asking: Set<Page> }).asking;
    expect(asking.size).toBe(0);
    // A question left open again: its agent can still close the tab, which goes with its question, unanswered.
    await script(a, "setTimeout(() => { window.answered = confirm('Pay again?'); }, 300); return 1;", "chat-1");
    host.pause("chat-1", true);
    await new Promise((done) => setTimeout(done, 1_000));
    host.pause("chat-1", false);
    expect(await within(2_000, op(a, "browser.evaluate", { code: "return 1;" }, "chat-1"))).toEqual(ASKING);
    expect(asking.size).toBe(1);
    expect(await within(5_000, op(a, "browser.close", {}, "chat-1"))).toEqual({ ok: { closed: true } });
    expect(asking.size).toBe(0);
    expect(await pages()).toBe(1);
  }, 30_000);

  it("leaves a question open for its user in a tab they opened themselves too while they hold the browser, and asks them before such a page is left", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/t/HELD" }, "chat-1");
    const context = await (host as unknown as { running: Promise<BrowserContext> }).running;
    host.pause("chat-1", true);
    // A tab its user opens themselves, as with Ctrl+T, and goes to a site in: no session's, and in front.
    const own = await context.newPage();
    await own.goto("http://fixture.test/t/OWN");
    await expect.poll(front, { timeout: 5_000 }).toBe("OWN");
    const answered = () => within(500, own.evaluate("window.answered"));
    await own.evaluate("void setTimeout(() => { window.answered = confirm('Pay?'); }, 300)");
    // Open still, 2 s on: the page waits on it, and nobody has answered it for them.
    await new Promise((done) => setTimeout(done, 2_300));
    expect(await answered()).toBe("late");
    // It is no session's page: no agent is refused anything for it.
    expect((host as unknown as { asking: Set<Page> }).asking.size).toBe(0);
    // Their own accept, at their keyboard, reaches the page.
    asUser("focus", xwindow()!.id);
    asUser("press", "Return");
    await expect.poll(answered, { timeout: 5_000 }).toBe(true);
    // A page that asks before it is left, once its user has acted in it: they are asked, and it is not left for them.
    await own.evaluate("addEventListener('beforeunload', (event) => { event.preventDefault(); event.returnValue = 'stay'; })");
    const [x, y] = await onScreen(own, "go");
    asUser("click", String(x), String(y));
    await expect.poll(() => within(500, own.title()), { timeout: 5_000 }).toBe("clicked 1");
    await own.evaluate("void setTimeout(() => { location.href = '/second'; }, 300)");
    await new Promise((done) => setTimeout(done, 2_300));
    expect(own.url()).toBe("http://fixture.test/t/OWN");
    // Their own answer, to leave, is what leaves it.
    asUser("focus", xwindow()!.id);
    asUser("press", "Return");
    await expect.poll(() => own.url(), { timeout: 5_000 }).toBe("http://fixture.test/second");
  }, 40_000);

  it("leaves a page it was asked to confirm leaving, while the agent drives, as before", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    // A page that asks before it is left, once its user, or its agent, has acted in it.
    await script(a, "addEventListener('beforeunload', (event) => { event.preventDefault(); event.returnValue = 'stay'; }); return 1;", "chat-1");
    await op(a, "browser.mouse", { action: "click", x: 60, y: 110, button: "left", clicks: 1 }, "chat-1");
    expect((await within(10_000, op(a, "browser.navigate", { url: "http://fixture.test/second" }, "chat-1")) as { ok?: { title: string } }).ok?.title).toBe("Second");
  }, 30_000);

  it("leaves the page its user holds where it is: a navigation in flight is stopped, and no bound closes it", async () => {
    await host.close();
    host = hostWith({ boundMs: 3_000 });
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/t/HELD" }, "chat-1");
    const page = tabs().get(a)![0]!;
    // A navigation to a page that answers 1.5 s on.
    const going = op(a, "browser.navigate", { url: "http://fixture.test/slow" }, "chat-1");
    await new Promise((done) => setTimeout(done, 300));
    host.pause("chat-1", true);
    expect(await within(1_000, going)).toEqual(PAUSED);
    host.pause("chat-1", false);
    // A script that never answers: left to its bound, its page would be closed, and the browser with its last page.
    const stuck = op(a, "browser.evaluate", { code: "await new Promise(() => {}); return 1;" }, "chat-1");
    await new Promise((done) => setTimeout(done, 300));
    host.pause("chat-1", true);
    expect(await within(1_000, stuck)).toEqual(PAUSED);
    // Past the slow page's answer, and past the bound: the page is the one its user holds, open.
    await new Promise((done) => setTimeout(done, 3_500));
    expect(page.isClosed()).toBe(false);
    expect(page.url()).toBe("http://fixture.test/t/HELD");
    expect(await page.title()).toBe("HELD");
    expect(await pages()).toBe(1);
  }, 30_000);

  it("lets a page that had begun to arrive finish arriving when its user takes the browser over: only a navigation not yet answered is stopped", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/t/HELD" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const going = op(a, "browser.navigate", { url: "http://fixture.test/long" }, "chat-1");
    // The new page has taken the tab: its first part is there, its rest 1.5 s away.
    await expect.poll(() => page.url(), { timeout: 10_000 }).toBe("http://fixture.test/long");
    host.pause("chat-1", true);
    expect(await within(1_000, going)).toEqual(PAUSED);
    // The page its user holds now is the new one, and it comes whole: its end, and its script.
    await expect.poll(() => within(500, page.evaluate("[document.getElementById('last') !== null, window.finished === true]")), { timeout: 10_000 }).toEqual([true, true]);
  }, 30_000);

  it("stops a navigation not yet answered though the page it would replace changes its own address meanwhile", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/t/HELD" }, "chat-1");
    const page = tabs().get(a)![0]!;
    // A page that rewrites its own address as it goes, as many do: no new page has come for that.
    await script(a, "setInterval(() => history.replaceState(null, '', '#' + Date.now()), 20); return 1;", "chat-1");
    // A navigation to a page that answers 1.5 s on.
    const going = op(a, "browser.navigate", { url: "http://fixture.test/slow" }, "chat-1");
    await new Promise((done) => setTimeout(done, 300));
    host.pause("chat-1", true);
    expect(await within(1_000, going)).toEqual(PAUSED);
    // Past the slow page's answer: the page its user holds is the one they held.
    await new Promise((done) => setTimeout(done, 2_500));
    expect(new URL(page.url()).pathname).toBe("/t/HELD");
    expect(await page.title()).toBe("HELD");
  }, 30_000);

  it("brings a chat's own newest page to the front before a sub-agent's, and shows none for a chat with no page", async () => {
    const [child, another] = [session(), session()];
    expect(await host.show("chat-1")).toBe(false);
    // A sub-agent's page and another chat's, each a tab of the one window, the other chat's put in front.
    await op(child, "browser.navigate", { url: "http://fixture.test/t/CHILD" }, "chat-1");
    await op(another, "browser.navigate", { url: "http://fixture.test/t/ANOTHER" }, "chat-2");
    const inFront = async (of: string, page = 0) => {
      await tabs().get(of)![page]!.bringToFront();
      await expect.poll(front, { timeout: 5_000 }).toBe(await tabs().get(of)![page]!.title());
    };
    await inFront(another);
    // The chat has only its sub-agent's page: that one is shown.
    expect(await host.show("chat-1")).toBe(true);
    await expect.poll(front, { timeout: 5_000 }).toBe("CHILD");
    // Its own tab, once it has one, before its sub-agent's.
    await op("chat-1", "browser.navigate", { url: "http://fixture.test/t/OWN" }, "chat-1");
    expect(await host.show("chat-1")).toBe(true);
    await expect.poll(front, { timeout: 5_000 }).toBe("OWN");
    // A popup its tab opens is its newest page: shown, not the tab it came from.
    await op("chat-1", "browser.evaluate", { code: "window.open('http://fixture.test/t/POPUP'); return 1;" }, "chat-1");
    await expect.poll(async () => Promise.all((tabs().get("chat-1") ?? []).map((page) => page.title())), { timeout: 10_000 }).toEqual(["OWN", "POPUP"]);
    await inFront(another);
    expect(await host.show("chat-1")).toBe(true);
    await expect.poll(front, { timeout: 5_000 }).toBe("POPUP");
    expect(await host.show("chat-3")).toBe(false);
    await expect.poll(front, { timeout: 5_000 }).toBe("POPUP");
  }, 30_000);

  it("refuses an operation that names a session's tab under another chat than its own", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const ANOTHER_CHATS = { error: { type: "browser", message: "This session's tab in the agent's browser on this computer is another chat's" } };
    // A session is one chat's: named under another, it acts in no page, and closes none.
    expect(await op(a, "browser.evaluate", { code: "document.title = 'acted under another chat'; return 1;" }, "chat-2")).toEqual(ANOTHER_CHATS);
    expect(await op(a, "browser.close", {}, "chat-2")).toEqual(ANOTHER_CHATS);
    expect(await pages()).toBe(1);
    // It is its own chat's still: held from that chat's side, shown for it, and its own operations run.
    expect(await host.show("chat-1")).toBe(true);
    expect(await host.show("chat-2")).toBe(false);
    expect((await op(a, "browser.evaluate", { code: "return document.title;" }, "chat-1")).ok?.value).toBe("Fixture");
    // Once it has no page left, here closed by its user, there is nothing of its chat's to act in: the name is free.
    await op(session(), "browser.navigate", { url: "http://fixture.test/" }, "chat-3");
    await tabs().get(a)![0]!.close();
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/second" }, "chat-2")).ok).toMatchObject({ title: "Second", opened: true });
    expect(await host.show("chat-2")).toBe(true);
  }, 30_000);

  it("closes every tab of a deleted chat's sessions, and no other chat's", async () => {
    const [a, child, b] = [session(), session(), session()];
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    await op(child, "browser.navigate", { url: "http://fixture.test/second" }, "chat-1");
    await op(b, "browser.navigate", { url: "http://fixture.test/second" }, "chat-2");
    expect(await pages()).toBe(3);
    await host.forget("chat-1");
    expect(await pages()).toBe(1);
    expect(await script(b, "return document.title;", "chat-2")).toBe("Second");
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1")).ok?.opened).toBe(true);
  });
});
