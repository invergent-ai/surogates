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

import type { BrowserContext, FileChooser, Frame, JSHandle, Page } from "playwright-core";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { PAUSED } from "../src/browser/client.js";
import { interrupted, LEFT_TO_USER, type StagedDownload, tooLarge } from "../src/browser/downloads.js";
import {
  A_FOLDER, AFTER_FAILURE_MS, AFTER_HAND_BACK_MS, ASKING, BrowserHost, type BrowserHostOptions, clearStaged, FILE_ASKED, filesOf, GIVEN_AS_TAKEN, holding,
  type Launch, NO_SITE, NOT_AS_ASKED, NOT_ASKED, notFinished, ONE_FILE, OWN_CHOOSER_MS, PROXY_BYPASSED, WEAKENING,
} from "../src/browser/host.js";
import { OPERATIONS } from "../src/browser/operations.js";
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

// Pages whose own script makes their file input ask, with no hand on the input: a button at 60, 40 and the input below it.
const asking = (title: string, pressed: string, script = "") => `<!doctype html><title>${title}</title>
<button id="go" style="position:absolute;left:20px;top:20px;width:120px;height:40px" onclick="${pressed}">Go</button>
<input id="file" type="file" style="position:absolute;left:20px;top:100px">
<script>window.asked = 0; const ask = () => { window.asked += 1; document.getElementById('file').click(); }; ${script}</script>`;
const ASKING_PAGES: Record<string, string> = {
  // Asks once, 1.8 s after its button is pressed.
  once: asking("ONCE", "setTimeout(ask, 1800)"),
  // Asks once, 3.5 s after its button is pressed.
  later: asking("LATER", "setTimeout(ask, 3500)"),
  // Asks every 2 s from its button's first press on, until told to stop.
  often: asking("OFTEN", "window.asking ??= setInterval(ask, 2000)"),
  // Busy for three seconds from each click on its file input on: what it asked for by that click is heard of only then.
  busy: asking("BUSY", "", "document.getElementById('file').addEventListener('click', () => setTimeout(() => { const until = performance.now() + 3000; while (performance.now() < until) {} }, 0));"),
};

let site: Server;
let canary: Server;
let ports: { site: number; canary: number };
let hits: string[];
// What fixture.test's cross-site frame asked the site for.
let framed: string[];
// How many times fixture.test's download that answers its first asker late was asked for.
let firsts: number;
let profile: string;
let launch: Launch;
let host: BrowserHost;
let next = 0;

beforeEach(async () => {
  hits = [];
  framed = [];
  firsts = 0;
  site = createServer((req, res) => {
    // other.test's page with a file input of its own, framing fixture.test's page with another.
    if (req.headers.host === "other.test" && req.url === "/fileframe") {
      return void res.writeHead(200, { "content-type": "text/html" }).end(`<title>Framing</title>
<iframe src="http://fixture.test/fileinput" style="position:absolute;left:0;top:0;width:400px;height:200px;border:0"></iframe>
<input id="top" type="file" style="position:absolute;left:20px;top:220px;width:200px;height:40px">`);
    }
    if (req.url === "/fileinput" || req.url === "/fileinput?second") {
      return void res.writeHead(200, { "content-type": "text/html" })
        .end(`<input id="file" type="file" style="position:absolute;left:20px;top:20px;width:200px;height:40px">`);
    }
    // fixture.test's page framing two pages of its own, each with a file input.
    if (req.url === "/twoframes") {
      return void res.writeHead(200, { "content-type": "text/html" })
        .end(`<title>Two</title><iframe id="f" src="/fileinput"></iframe><iframe id="g" src="/fileinput?second"></iframe>`);
    }
    // Pages that ask for a file by themselves, each by a script of its own.
    if (req.url?.startsWith("/asks/")) {
      const scripted = ASKING_PAGES[req.url.slice("/asks/".length)];
      if (scripted !== undefined) return void res.writeHead(200, { "content-type": "text/html" }).end(scripted);
    }
    // And framing three file inputs in frames with no address of their own: one the page spells out, one it writes into an
    // empty frame, and one from a data address, which runs as no site at all.
    if (req.url === "/unaddressed") {
      return void res.writeHead(200, { "content-type": "text/html" }).end(`<title>Unaddressed</title>
<iframe id="spelled" srcdoc='<input id="file" type="file">'></iframe><iframe id="written"></iframe>
<iframe id="data" src="data:text/html,<input id=file type=file>"></iframe>
<script>const written = document.getElementById("written").contentDocument; written.write('<input id="file" type="file">'); written.close();</script>`);
    }
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
    // A download whose site answers the first to ask for it only 5 s on, and whoever asks after that at once.
    if (req.url === "/first-late.bin") {
      const answer = () => res.writeHead(200, { "content-type": "application/octet-stream", "content-disposition": "attachment" }).end("late");
      return void ((firsts += 1) === 1 ? setTimeout(answer, 5_000) : answer());
    }
    // One whose site has nothing for the first to ask, and the file for whoever asks after.
    if (req.url === "/first-empty.bin") {
      if ((firsts += 1) === 1) return void res.writeHead(204).end();
      return void res.writeHead(200, { "content-type": "application/octet-stream", "content-disposition": "attachment" }).end("late");
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
// A file of the chat's folder, as the main side sends it to the page: its name, its type and what it holds.
const REPORT = { name: "report.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7").toString("base64") };
// The file input the host keeps for *session*'s next upload, if any.
const kept = (session: string) => (host as unknown as { choosers: Map<string, FileChooser> }).choosers.get(session);
// A page of *session*'s asks for a file at *click*, a click in it as its agent's would be: settled once the host has heard it.
async function asksFor(session: string, click: () => Promise<unknown>): Promise<void> {
  const before = kept(session);
  await click();
  await expect.poll(() => kept(session) !== undefined && kept(session) !== before, { timeout: 10_000 }).toBe(true);
}
// The names of the files each file input of a page or a frame holds.
const filed = () => [...document.querySelectorAll("input")].map((input) => [...(input.files ?? [])].map((file) => file.name));
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
// The browser's own file choosers open on that display, each a window that says it is one. Under test/isolated.sh
// the browser reaches no portal, so its chooser is GTK's.
function ownChoosers(): string[] {
  const ids = execFileSync("xwininfo", ["-root", "-tree"], { encoding: "utf8" }).split("\n").flatMap((line) => /^\s+(0x[0-9a-f]+) /.exec(line)?.[1] ?? []);
  return ids.filter((id) => {
    try {
      return execFileSync("xprop", ["-id", id, "WM_WINDOW_ROLE"], { encoding: "utf8" }).includes("GtkFileChooserDialog");
    } catch {
      // Gone since it was listed.
      return false;
    }
  });
}
// The fixture's addresses the host keeps a navigation's request of, as not known to have ended as a page. Not the
// address a new browser is asked first, to see that its requests come through its proxy: that one fails, and is kept its second.
const requested = () => [...(host as unknown as { open: Map<{ url(): string }, unknown> }).open.keys()].map((request) => request.url())
  .filter((url) => url.startsWith("http://fixture.test/"));
// How many listeners *page* has for a file a page asks for: the host's, or none while its user holds the browser.
const hears = (page: Page) => (page as unknown as { listenerCount(event: string): number }).listenerCount("filechooser");
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

describe("an upload's files, as the host takes them from the main side", () => {
  const data = Buffer.from("%PDF-1.7").toString("base64");
  const file = (changed: Record<string, unknown> = {}) => ({ name: "report.pdf", mimeType: "application/pdf", buffer: data, ...changed });

  it("takes one to ten, each a plain name, a type and what it holds in base64, and gives the page those three and nothing else of each", () => {
    expect(filesOf([file()])).toEqual([{ name: "report.pdf", mimeType: "application/pdf", buffer: data }]);
    expect(filesOf(Array.from({ length: 10 }, () => file()))).toHaveLength(10);
    // Whatever else came with one, as a path would, is not the page's.
    expect(filesOf([file({ path: "/home/u/notes/report.pdf", lastModified: 1 })])).toEqual([{ name: "report.pdf", mimeType: "application/pdf", buffer: data }]);
    // A name as a folder holds it, with what a shell or a page might read into it left as it is; an empty file; no type.
    expect(filesOf([file({ name: "a b (2) #1 & c.tar.gz", buffer: "", mimeType: "" })])).toEqual([{ name: "a b (2) #1 & c.tar.gz", mimeType: "", buffer: "" }]);
  });

  it("takes none where there are no files, more than ten, or one that is not a plain name, a type and base64", () => {
    for (const files of [undefined, null, "report.pdf", {}, [], Array.from({ length: 11 }, () => file())]) expect(filesOf(files), JSON.stringify(files)?.slice(0, 40)).toBeNull();
    for (const changed of [
      { name: "" }, { name: "." }, { name: ".." }, { name: "../etc/passwd" }, { name: "/etc/passwd" }, { name: "a\\b" }, { name: "a\0b" }, { name: 7 },
      { name: "n".repeat(256) }, { mimeType: 7 }, { mimeType: undefined }, { mimeType: "t".repeat(256) },
      { buffer: 7 }, { buffer: undefined }, { buffer: Buffer.from("x") }, { buffer: "not base64!" }, { buffer: "QQ" }, { buffer: "QQ=\n" }, { buffer: "=QQ=" },
    ]) {
      expect(filesOf([file(changed)]), JSON.stringify(changed)).toBeNull();
      // One such among good ones: none of the upload is taken.
      expect(filesOf([file(), file(changed)]), JSON.stringify(changed)).toBeNull();
    }
    for (const one of [null, "report.pdf", [], 7]) expect(filesOf([one])).toBeNull();
  });

  it("takes up to what a write may carry, in all, and none of an upload a byte over", () => {
    const half = Buffer.alloc(MAX_WRITE_BYTES / 2).toString("base64");
    expect(filesOf([file({ buffer: half }), file({ buffer: half })])).toHaveLength(2);
    expect(filesOf([file({ buffer: half }), file({ buffer: half }), file({ buffer: "QQ==" })])).toBeNull();
    expect(filesOf([file({ buffer: Buffer.alloc(MAX_WRITE_BYTES + 1).toString("base64") })])).toBeNull();
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
    hearing: Map<Page, unknown>; choosers: Map<string, FileChooser>; named: Map<string, { input: { chooser: FileChooser } | null }>;
    live: BrowserContext | null;
    adopt(session: string, page: Page): void;
    arrived(page: Page, download: unknown): Promise<void>;
    requested(request: unknown): void;
    loaded(request: unknown): void;
    failed(request: unknown): void;
    closed(context: BrowserContext): void;
  };
  // The host's clock, which a test moves.
  let clock: number;
  // A download the browser announces in *page*.
  const arrives = (download: unknown, page = PAGE) => state().arrived(page, download);
  // A navigation's request, as the browser says it begins: to *url*, after *from* where a redirect led there, in *page*.
  const asks = (url: string, from: unknown = null, navigation = true, page: Page = PAGE) => {
    const request = { url: () => url, isNavigationRequest: () => navigation, redirectedFrom: () => from, frame: () => ({ page: () => page }) };
    state().requested(request);
    return request;
  };
  // A page of the session's in which the agent's own navigation acts, not answered yet: as the host runs it, by
  // what a take-over stops it by. *answer*: its site answers, so the page it leads to has begun to arrive.
  const navigating = (url: string) => {
    const hears = new Map<string, (event: unknown) => void>();
    const frame = {};
    const page = {
      on: (event: string, listener: (event: unknown) => void) => void hears.set(event, listener), off: () => {},
      goto: () => new Promise(() => {}), mainFrame: () => frame, url: () => url, title: () => Promise.resolve(""),
    } as unknown as Page;
    state().tabs.get(SESSION)!.push(page);
    void OPERATIONS["browser.navigate"]!(page, { url }, state().interrupt.signal).catch(() => {});
    const answer = () => hears.get("response")?.({ request: () => ({ isNavigationRequest: () => true, frame: () => frame }), status: () => 200 });
    return { page, request: asks(url, null, true, page), answer };
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

  it("begins the minute only at a hand back that hands the browser back: not at one by a chat that does not hold it, nor at a second one in a row", async () => {
    const whose = async (name: string) => {
      const file = fileOf(6);
      await arrives(downloadOf(name, file, Promise.resolve(file)));
      const handed = staged.at(-1);
      return handed?.name !== name ? "not staged" : handed.user ? (handed.afterHandBack ? "theirs, just after" : "theirs") : "the agent's";
    };
    // Nobody holds the browser: a hand back hands nothing back, and no minute begins.
    host.pause("chat-2", false);
    expect(await whose("first.bin")).toBe("the agent's");
    // Held from one chat: another's hand back ends nothing, and the browser is held still.
    host.pause("chat-1", true);
    host.pause("chat-2", false);
    expect(await whose("held.bin")).toBe("theirs");
    // Handed back by the chat that holds it: the minute begins, and runs out.
    host.pause("chat-1", false);
    expect(await whose("after.bin")).toBe("theirs, just after");
    clock += AFTER_HAND_BACK_MS + 1;
    expect(await whose("later.bin")).toBe("the agent's");
    // A second hand back, by that chat or another, with nobody holding it: the minute is not begun again.
    host.pause("chat-1", false);
    host.pause("chat-2", false);
    expect(await whose("again.bin")).toBe("the agent's");
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
    // And another, begun under the first, where the second chat has held the browser and handed it back since: the
    // chat it began under, not the one that handed it back last.
    host.pause("chat-1", false);
    host.pause("chat-2", true);
    asks(`${SITE_URL}?second`);
    host.pause("chat-2", false);
    host.pause("chat-1", true);
    host.pause("chat-1", false);
    const second = fileOf(6);
    await arrives(downloadOf("payslip.pdf", second, Promise.resolve(second), `${SITE_URL}?second`), TAB);
    expect(staged.pop()).toEqual({ root: "chat-2", session: "chat-2", name: "payslip.pdf", path: second, user: true });
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

  it("reads the minute after a hand back, and a failed request's second, on a clock that cannot be set: the computer's clock put on or back changes neither", async () => {
    // The clock a host told none reads, and the computer's own, which its user or its network can set.
    let steady = 5_000;
    let wall = 1_700_000_000_000;
    const clocks = [vi.spyOn(performance, "now").mockImplementation(() => steady), vi.spyOn(Date, "now").mockImplementation(() => wall)];
    try {
      host = new BrowserHost({ downloaded: (download) => staged.push(download) });
      state().roots.set(SESSION, "chat-1");
      state().tabs.set(SESSION, [PAGE]);
      const comes = async (name: string, url?: string) => {
        const file = fileOf(6);
        await arrives(downloadOf(name, file, Promise.resolve(file), url));
        const handed = staged.at(-1);
        return handed?.name !== name ? "dropped" : handed.user ? "theirs" : "the agent's";
      };
      host.pause("chat-2", true);
      host.pause("chat-2", false);
      // Five seconds after the hand back, the computer's clock an hour on: the minute is not over.
      steady += 5_000;
      wall += 3_600_000;
      expect(await comes("on.bin")).toBe("theirs");
      // The minute over, and the computer's clock put two hours back: it does not begin again.
      steady += AFTER_HAND_BACK_MS;
      wall -= 2 * 3_600_000;
      expect(await comes("back.bin")).toBe("the agent's");
      // A request the browser gave up as a page a second ago, the computer's clock an hour on meanwhile: its download's
      // beginning still. The agent's own, begun before this take-over: dropped.
      state().failed(asks(SITE_URL));
      steady += AFTER_FAILURE_MS;
      wall += 3_600_000;
      host.pause("chat-2", true);
      expect(await comes("export.csv", SITE_URL)).toBe("dropped");
    } finally {
      for (const clock of clocks) clock.mockRestore();
    }
  });

  it("keeps of the browser's requests only navigations not yet ended as a page, a failed one no longer than the second it counts for, none of a browser that closed, and no more than a bound of them", async () => {
    // What a page loads beside its own document is no download's beginning.
    asks("http://fixture.test/image.png", null, false);
    expect(state().open.size).toBe(0);
    // One that loaded as a page is done with.
    state().loaded(asks(SITE_URL));
    expect(state().open.size).toBe(0);
    // One the browser gave up as a page, as it does a moment before it announces it as a download: kept for that
    // second, and gone at the next thing the browser says once it has passed.
    const failed = asks(`${SITE_URL}?failed`);
    state().failed(failed);
    clock += AFTER_FAILURE_MS;
    asks(`${SITE_URL}?next`);
    expect(state().open.has(failed)).toBe(true);
    clock += 1;
    asks(`${SITE_URL}?after`);
    expect([state().open.has(failed), state().open.size]).toEqual([false, 2]);
    // However many are asked for and never answered, the oldest go.
    const many = Array.from({ length: 300 }, (_, at) => asks(`${SITE_URL}?${at}`));
    expect([state().open.size, state().open.has(many[0]), state().open.has(many.at(-1))]).toEqual([256, false, true]);
    // A browser that closed answers none of them: its next one starts with none.
    state().closed({} as BrowserContext);
    expect(state().open.size).toBe(0);
  });

  it("counts a request the browser gave up as a page for a download only where the download is announced within a second of that, to the millisecond; one still on its way counts however old", async () => {
    expect(AFTER_FAILURE_MS).toBe(1_000);
    const comes = async (name: string, url: string) => {
      const file = fileOf(6);
      await arrives(downloadOf(name, file, Promise.resolve(file), url));
      return staged.at(-1)?.name === name ? `staged, user: ${staged.at(-1)!.user}` : existsSync(file) ? "kept" : "dropped";
    };
    // The agent's own navigation that becomes a download: given up as a page, announced a moment after. Its own, still.
    state().failed(asks(SITE_URL));
    clock += 15;
    expect(await comes("export.csv", SITE_URL)).toBe("staged, user: false");
    // A navigation of the agent's that failed, as to a site that refused it, and then its user takes the browser over.
    // No operation of the agent's waited on it: the take-over stopped none of it.
    state().failed(asks(`${SITE_URL}?a`));
    state().failed(asks(`${SITE_URL}?b`));
    host.pause("chat-2", true);
    // Announced exactly a second after: its answer, the agent's, begun before the take-over. Dropped, and its agent told.
    clock += AFTER_FAILURE_MS;
    expect(await comes("a.bin", `${SITE_URL}?a`)).toBe("dropped");
    expect(state().unseen.get(SESSION)).toEqual([interrupted("a.bin")]);
    // A millisecond later the other is no download's beginning: a download of that address is one with no request
    // known, as its user's own by the site's link with `download` is. Theirs, and the agent told nothing of it.
    clock += 1;
    expect(await comes("b.bin", `${SITE_URL}?b`)).toBe("staged, user: true");
    expect([did, state().unseen.get(SESSION), state().open.size]).toEqual([["cancel", "delete"], [interrupted("a.bin")], 0]);
    host.pause("chat-2", false);
    // One the browser has not given up counts however long it has been on its way.
    clock += AFTER_HAND_BACK_MS + 1;
    asks(`${SITE_URL}?slow`);
    clock += 600 * AFTER_FAILURE_MS;
    host.pause("chat-2", true);
    expect(await comes("slow.bin", `${SITE_URL}?slow`)).toBe("dropped");
  });

  it("forgets at a take-over the navigation of the agent's that the take-over itself stopped: a download of that address its user then makes is theirs, at once, and the agent is told nothing of it", async () => {
    // The agent's navigation to a download its site has not answered yet, as its own operation runs it.
    const stopped = navigating(SITE_URL);
    // Another, which its site has answered: the page it leads to has begun to arrive, and a take-over leaves it to.
    const arriving = navigating(`${SITE_URL}?arriving`);
    arriving.answer();
    // And one no operation of the agent's waits on, as a click's: the take-over stops none of it.
    const clicked = asks(`${SITE_URL}?clicked`);
    host.pause("chat-2", true);
    expect([stopped.request, arriving.request, clicked].map((request) => state().open.has(request))).toEqual([false, true, true]);
    // The browser says the stopped one failed, a moment after: it stays forgotten.
    state().failed(stopped.request);
    expect(state().open.has(stopped.request)).toBe(false);
    // Their own click on the site's own link with `download` to that address, in that same second: no request of it is said.
    const theirs = fileOf(6);
    await arrives(downloadOf("late.bin", theirs, Promise.resolve(theirs), SITE_URL), stopped.page);
    expect(staged).toEqual([{ root: "chat-1", session: SESSION, name: "late.bin", path: theirs, user: true }]);
    expect([did, state().unseen.size]).toEqual([[], 0]);
    // The two the take-over did not stop are the agent's still: answered under its user's hand, dropped, and told.
    for (const [name, url] of [["arriving.bin", `${SITE_URL}?arriving`], ["clicked.bin", `${SITE_URL}?clicked`]] as const) {
      const file = fileOf(6);
      await arrives(downloadOf(name, file, Promise.resolve(file), url));
      expect([staged.length, existsSync(file)], name).toEqual([1, false]);
    }
    expect(state().unseen.get(SESSION)).toEqual([interrupted("arriving.bin"), interrupted("clicked.bin")]);
    // A take-over stops a page's navigation once: the next one forgets nothing of that page.
    host.pause("chat-2", false);
    const later = asks(`${SITE_URL}?later`, null, true, stopped.page);
    host.pause("chat-2", true);
    expect(state().open.has(later)).toBe(true);
  });

  describe("beside the files its pages ask for", () => {
    const FORM_URL = "http://fixture.test/form";
    const SUB_AGENT = "session-of-a-sub-agent";
    // A page of *session*'s as the host takes one, at the form's address, with the browser's part of it as a test
    // plays it. *heard*: how many hear it ask for a file. *input*: a file input of it clicked, as the browser says
    // it to whatever hears; *made*: what an upload's files are once they are ready in the page, whose one step puts
    // them into the input. *navigates*: the agent's own navigation in it, not answered yet, and its request.
    const taken = (session = SESSION) => {
      const hears = new Map<string, Set<(event: unknown) => void>>();
      const on = (event: string, heard: (event: unknown) => void) => void hears.set(event, (hears.get(event) ?? new Set()).add(heard));
      const frame = {};
      const page = {
        on, once: on, off: (event: string, heard: (event: unknown) => void) => void hears.get(event)?.delete(heard),
        goto: () => new Promise(() => {}), mainFrame: () => frame, frames: () => [], url: () => FORM_URL, title: () => Promise.resolve(""), isClosed: () => false,
      } as unknown as Page;
      if (!state().tabs.has(session)) {
        state().roots.set(session, "chat-1");
        state().tabs.set(session, []);
      }
      state().adopt(session, page);
      return {
        page,
        heard: () => hears.get("filechooser")?.size ?? 0,
        input: (made: unknown = {}) => {
          const element = { evaluate: () => Promise.resolve({ here: true, href: FORM_URL, origin: new URL(FORM_URL).origin }), evaluateHandle: () => Promise.resolve(made) };
          const chooser = { page: () => page, element: () => element } as unknown as FileChooser;
          for (const heard of hears.get("filechooser") ?? []) heard(chooser);
          return chooser;
        },
        navigates: (url: string) => {
          void OPERATIONS["browser.navigate"]!(page, { url }, state().interrupt.signal).catch(() => {});
          return asks(url, null, true, page);
        },
      };
    };
    // An upload of the session's as the main side sends it, launching nothing: *id*, the one its user was asked about.
    const uploads = (id?: string) => host.perform(
      { executable: join(profile, "no-browser-here"), profile }, "chat-1", SESSION, "browser.set_input_files", { files: [REPORT] }, new AbortController().signal, id,
    );
    // The session has no page but those a test's host takes.
    const fresh = () => {
      state().roots.set(SESSION, "chat-1");
      state().tabs.set(SESSION, []);
    };

    beforeEach(fresh);

    it("does at the one take-over all that it does for either, with nothing the browser says between: no input stays kept for an upload, the request of the navigation it stopped is forgotten and the download on its way stopped; its pages are heard five seconds more, for no one, and again from the hand back, when the minute begins", async () => {
      vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
      try {
        const [tab, other] = [taken(), taken(SUB_AGENT)];
        // The agent drives. A file input of its page asked, and an upload's prompt named it; a sub-agent's navigation
        // to a download is not answered yet; and a download of its own is on its way.
        const asked = tab.input();
        expect(await host.address(SESSION, true, "upload-1")).toBe(FORM_URL);
        const request = other.navigates(SITE_URL);
        const own = fileOf(6);
        let ends!: (path: string) => void;
        const onItsWay = arrives(downloadOf("own.bin", own, new Promise((done) => {
          ends = done;
        })), tab.page);
        expect([state().choosers.get(SESSION), state().named.get(SESSION)?.input?.chooser, state().open.has(request), state().arriving.size])
          .toEqual([asked, asked, true, 1]);
        expect([tab.heard(), other.heard()]).toEqual([1, 1]);
        host.pause("chat-2", true);
        // All of it is so by the time the take-over returns.
        expect([state().choosers.size, state().named.size, state().open.has(request), did]).toEqual([0, 0, false, ["cancel"]]);
        expect([tab.heard(), other.heard()]).toEqual([1, 1]);
        // What the agent was doing reaches its page after that, and the page asks for a file: heard, so the browser
        // opens no chooser of its own, and kept for no one.
        tab.input();
        expect([state().choosers.size, state().unseen.get(SESSION)]).toEqual([0, [FILE_ASKED]]);
        // Their own download of the address the stopped navigation asked for, of which the browser says no request: theirs.
        const theirs = fileOf(6);
        await arrives(downloadOf("export.csv", theirs, Promise.resolve(theirs), SITE_URL), other.page);
        expect(staged).toEqual([{ root: "chat-1", session: SUB_AGENT, name: "export.csv", path: theirs, user: true }]);
        // The upload that prompt was for comes now: it gives nothing.
        expect(await uploads("upload-1")).toEqual(PAUSED);
        // Five seconds after the take-over, and after the file the page asked for since, and not before, a file input is their own to click.
        vi.advanceTimersByTime(OWN_CHOOSER_MS - 1);
        expect([tab.heard(), other.heard()]).toEqual([1, 1]);
        vi.advanceTimersByTime(1);
        expect([tab.heard(), other.heard()]).toEqual([0, 0]);
        // Handed back: heard again at once, with nothing kept from before for either upload.
        host.pause("chat-2", false);
        expect([tab.heard(), other.heard(), state().choosers.size, state().named.size]).toEqual([1, 1, 0, 0]);
        expect(await uploads("upload-1")).toEqual({ error: { type: "browser", message: NOT_AS_ASKED } });
        expect(await uploads()).toEqual({ error: { type: "browser", message: NOT_ASKED } });
        // And a download of which no request is known is theirs in doubt, from that same moment.
        const doubt = fileOf(6);
        await arrives(downloadOf("doubt.bin", doubt, Promise.resolve(doubt)), tab.page);
        expect(staged[1]).toEqual({ root: "chat-1", session: SESSION, name: "doubt.bin", path: doubt, user: true, afterHandBack: true });
        // The agent's own, stopped where it was: dropped once it has ended, and its agent told.
        ends(own);
        await onItsWay;
        expect([staged.length, existsSync(own), state().unseen.get(SESSION)]).toEqual([2, false, [FILE_ASKED, interrupted("own.bin")]]);
      } finally {
        vi.useRealTimers();
      }
    });

    it("names no input for an upload's prompt where the browser was taken over while its page was still saying where the input is, though it was handed back before the page said: the upload that prompt is about is given to nothing", async () => {
      const tab = taken();
      const asked = tab.input({ evaluate: () => Promise.resolve("given"), dispose: () => Promise.resolve() });
      // The page is slow to say where its input is, as a busy one is.
      const slow: { says?: (place: unknown) => void } = {};
      Object.assign(asked.element(), { evaluate: () => new Promise((resolve) => {
        slow.says = resolve;
      }) });
      const naming = host.address(SESSION, true, "upload-1");
      await vi.waitFor(() => expect(slow.says).toBeDefined());
      // Taken over and handed back meanwhile: the input asked before its user held the browser.
      host.pause("chat-2", true);
      host.pause("chat-2", false);
      slow.says!({ here: true, href: FORM_URL, origin: new URL(FORM_URL).origin });
      await naming;
      expect(state().named.get(SESSION)?.input ?? null).toBeNull();
      // Its user allows the prompt: the files go to nothing, asked about or not.
      expect(await uploads("upload-1")).toEqual({ error: { type: "browser", message: NOT_AS_ASKED } });
      expect(await uploads()).toEqual({ error: { type: "browser", message: NOT_ASKED } });
    });

    it("gives nothing of an upload that waited its turn while the browser was taken over and handed back: its files were read before its user held the browser, and go to no input that asks after", async () => {
      const tab = taken();
      // The session's line is held by a prompt's naming, whose page is slow to say where its input is.
      const first = tab.input();
      const slow: { says?: (place: unknown) => void } = {};
      Object.assign(first.element(), { evaluate: () => new Promise((resolve) => {
        slow.says = resolve;
      }) });
      const naming = host.address(SESSION, true);
      await vi.waitFor(() => expect(slow.says).toBeDefined());
      // An upload nobody was asked about waits behind it, its files read already.
      const waiting = uploads();
      host.pause("chat-2", true);
      host.pause("chat-2", false);
      await new Promise((done) => setTimeout(done, 20));
      // The page asks anew once the agent drives again: an input that upload was never for.
      const given: unknown[] = [];
      tab.input({ evaluate: () => (given.push("given"), Promise.resolve("given")), dispose: () => Promise.resolve() });
      expect(state().choosers.size).toBe(1);
      slow.says!({ here: true, href: FORM_URL, origin: new URL(FORM_URL).origin });
      await naming;
      expect(await waiting).toEqual(PAUSED);
      expect(given).toEqual([]);
      // One sent after the hand back is given to it.
      expect(await uploads()).toMatchObject({ ok: { files: 1 } });
      expect(given).toEqual(["given"]);
    });

    it("forgets an upload it was asked about once it is told the upload is not coming: nothing stays named for it, and its operation is known no more", async () => {
      const tab = taken();
      const prompted = () => (host as unknown as { prompted: Set<string> }).prompted;
      tab.input({ evaluate: () => Promise.resolve("given"), dispose: () => Promise.resolve() });
      expect(await host.address(SESSION, true, "upload-1")).toBe(FORM_URL);
      expect([state().named.get(SESSION)?.input?.chooser !== undefined, [...prompted()]]).toEqual([true, ["upload-1"]]);
      // Denied, or run out: told so.
      host.notComing("upload-1");
      expect([state().named.size, prompted().size]).toEqual([0, 0]);
      // Another upload's name is not this one's to drop.
      expect(await host.address(SESSION, true, "upload-2")).toBe(FORM_URL);
      host.notComing("upload-1");
      expect([state().named.size, [...prompted()]]).toEqual([1, ["upload-2"]]);
      // And one that comes is forgotten as it comes, as before.
      expect(await uploads("upload-2")).toMatchObject({ ok: { files: 1 } });
      expect([state().named.size, prompted().size]).toEqual([0, 0]);
    });

    it("reads each bound on the clock it can be read on: the minute after a hand back on the one that cannot be set, the five seconds after a take-over on a timer, which the computer's clock moves no more, and the quarter second of an upload's step on the computer's own, the one its page reads too", async () => {
      // The clock a host told none reads, and the computer's own, which its user or its network can set.
      let steady = 5_000;
      let wall = 1_700_000_000_000;
      const clocks = [vi.spyOn(performance, "now").mockImplementation(() => steady), vi.spyOn(Date, "now").mockImplementation(() => wall)];
      vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
      try {
        host = new BrowserHost({ downloaded: (download) => staged.push(download) });
        fresh();
        const tab = taken();
        const comes = async (name: string) => {
          const file = fileOf(6);
          await arrives(downloadOf(name, file, Promise.resolve(file)), tab.page);
          return staged.at(-1)?.name !== name ? "dropped" : staged.at(-1)!.user ? "theirs" : "the agent's";
        };
        // An upload's step is sent with the moment it is late from, which its page compares with its own reading of
        // the computer's clock: the only clock both read. The first is taken late; the computer's clock is put back
        // an hour meanwhile, and the next is late from an hour earlier. The steady one moves neither.
        const lateFrom: number[] = [];
        tab.input({
          evaluate: (_put: unknown, { by }: { by: number }) => {
            lateFrom.push(by);
            wall -= 3_600_000;
            steady += 7_000;
            return Promise.resolve(lateFrom.length === 1 ? "late" : "given");
          },
          dispose: () => Promise.resolve(),
        });
        expect(await uploads()).toEqual({ ok: { files: 1, notices: [FILE_ASKED] } });
        expect(lateFrom).toEqual([1_700_000_000_000 + 250, 1_700_000_000_000 - 3_600_000 + 250]);
        // The five seconds after a take-over are a timer's: neither clock put on an hour ends them, and the timer does.
        host.pause("chat-2", true);
        wall += 3_600_000;
        steady += 3_600_000;
        vi.advanceTimersByTime(OWN_CHOOSER_MS - 1);
        expect(tab.heard()).toBe(1);
        vi.advanceTimersByTime(1);
        expect(tab.heard()).toBe(0);
        // The minute after the hand back is the steady clock's: the computer's put on an hour does not end it, nor do
        // the timers, and the steady one does.
        host.pause("chat-2", false);
        wall += 3_600_000;
        vi.advanceTimersByTime(10 * AFTER_HAND_BACK_MS);
        steady += AFTER_HAND_BACK_MS;
        expect(await comes("within.bin")).toBe("theirs");
        steady += 1;
        wall -= 2 * 3_600_000;
        expect(await comes("after.bin")).toBe("the agent's");
      } finally {
        vi.useRealTimers();
        for (const clock of clocks) clock.mockRestore();
      }
    });

    it("keeps nothing of a browser that closed, of the files its pages asked for or of what they requested: an upload is given to nothing after, and a download of an address it was asked is no answer to it", async () => {
      const context = {} as BrowserContext;
      state().live = context;
      const tab = taken();
      const asked = tab.input();
      expect(await host.address(SESSION, true, "upload-1")).toBe(FORM_URL);
      const request = tab.navigates(SITE_URL);
      expect([state().choosers.get(SESSION), state().named.get(SESSION)?.input?.chooser, state().hearing.has(tab.page), state().open.has(request)])
        .toEqual([asked, asked, true, true]);
      state().closed(context);
      expect([state().choosers.size, state().named.size, state().hearing.size, state().tabs.size, state().open.size, state().live]).toEqual([0, 0, 0, 0, 0, null]);
      // The upload its user was asked about, and one nobody was asked about: neither has an input.
      expect(await uploads("upload-1")).toEqual({ error: { type: "browser", message: NOT_AS_ASKED } });
      expect(await uploads()).toEqual({ error: { type: "browser", message: NOT_ASKED } });
      // Its user takes the next browser over, and downloads that address by the site's own link in a page of the
      // session's: theirs, where the closed browser's request would have made it the agent's, stopped and dropped.
      fresh();
      const next = taken();
      host.pause("chat-2", true);
      const theirs = fileOf(6);
      await arrives(downloadOf("export.csv", theirs, Promise.resolve(theirs), SITE_URL), next.page);
      expect([staged, did]).toEqual([[{ root: "chat-1", session: SESSION, name: "export.csv", path: theirs, user: true }], []]);
    });
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

  it("opens no file dialog for a file input, tells the agent, and gives the input the files it is sent, once", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    // Nothing asked for yet: nothing to give.
    const report = { name: "report.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7").toString("base64") };
    expect(await op(a, "browser.set_input_files", { files: [report] })).toEqual({
      error: { type: "browser", message: NOT_ASKED },
    });
    expect((await op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 })).ok.notices).toEqual([FILE_ASKED]);
    expect(await op(a, "browser.set_input_files", { files: [report] })).toEqual({ ok: { files: 1, notices: [] } });
    expect(await script(a, `const [file] = document.getElementById("file").files;
return [file.name, file.type, await file.text()];`)).toEqual(["report.pdf", "application/pdf", "%PDF-1.7"]);
    // Answered once: the next upload waits for the page to ask again.
    expect((await op(a, "browser.set_input_files", { files: [report] })).error?.message).toBe(NOT_ASKED);
    // A file the page is sent is a name and what it holds, never a path.
    await op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 });
    expect((await op(a, "browser.set_input_files", { files: [{ ...report, name: "../etc/passwd" }] })).error?.message).toBe(
      "A file for the page is a name, its type and what it holds",
    );
  });

  it("names the frame of the file input that asked, for an upload's prompt, and gives the files to that input though another asks after", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://other.test/fileframe" });
    const report = { name: "report.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7").toString("base64") };
    const page = (host as unknown as { tabs: Map<string, Page[]> }).tabs.get(a)![0]!;
    const framed = page.frames().find((frame) => frame.url() === "http://fixture.test/fileinput")!;
    // What each input holds: the framed site's, and the framing page's own.
    const names = (id: string) => [...(document.getElementById(id) as HTMLInputElement).files!].map((file) => file.name);
    const held = async () => [await framed.evaluate(names, "file"), await page.evaluate(names, "top")];
    // The framed site is drawn by a process of its own: the mouse reaches it a moment after its page loaded.
    await framed.evaluate(() => addEventListener("mousemove", () => Object.assign(window, { reached: true })));
    await expect.poll(async () => {
      await op(a, "browser.mouse", { action: "move", x: 60, y: 40 });
      return framed.evaluate(() => (window as unknown as { reached?: boolean }).reached === true);
    }, { timeout: 10_000 }).toBe(true);
    // The input of another site, framed in the page: the click lands in its frame.
    expect((await op(a, "browser.mouse", { action: "click", x: 60, y: 40, button: "left", clicks: 1 })).ok.notices).toEqual([FILE_ASKED]);
    // The page the tab shows is one site; the site that would get the files is the frame's.
    expect(await host.address(a)).toBe("http://other.test/fileframe");
    expect(await host.address(a, true)).toBe("http://fixture.test/fileinput");
    // Another input asks while the prompt that named the frame is open, and the agent looks at the page: the files still go where the prompt said.
    await Promise.all([page.waitForEvent("filechooser"), page.click("#top")]);
    expect((await op(a, "browser.mouse", { action: "move", x: 1, y: 1 })).ok.notices).toEqual([FILE_ASKED]);
    expect(await op(a, "browser.set_input_files", { files: [report] })).toEqual({ ok: { files: 1, notices: [] } });
    expect(await held()).toEqual([["report.pdf"], []]);
    // The one that asked after is the session's next, named by its own frame: the page's.
    expect(await host.address(a, true)).toBe("http://other.test/fileframe");
    // Its agent acts before that upload comes, as after a denied one: the name is forgotten, and an upload nobody was asked about goes to the input that asked last.
    expect((await op(a, "browser.mouse", { action: "click", x: 60, y: 40, button: "left", clicks: 1 })).ok.notices).toEqual([FILE_ASKED]);
    expect(await op(a, "browser.set_input_files", { files: [{ ...report, name: "scan.pdf" }] })).toMatchObject({ ok: { files: 1 } });
    expect(await held()).toEqual([["scan.pdf"], []]);
  }, 30_000);

  it("gives an upload its user was asked about nothing where the input its prompt named is gone from its page, moved to another frame, or at another address by then", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/twoframes" });
    const page = tabs().get(a)![0]!;
    const [f, g] = ["/fileinput", "/fileinput?second"].map((path) => page.frames().find((frame) => frame.url() === `http://fixture.test${path}`)!) as [Frame, Frame];
    const upload = () => op(a, "browser.set_input_files", { files: [REPORT] });
    // The frame's input asks, and an upload's prompt names it by its frame.
    const named = async () => {
      await f.goto("http://fixture.test/fileinput");
      await asksFor(a, () => f.click("#file"));
      expect(await host.address(a, true)).toBe("http://fixture.test/fileinput");
    };
    // Taken out of its page, and kept by the page's script: a file given to it would still be the script's to read.
    await named();
    await f.evaluate(() => {
      const input = document.getElementById("file")!;
      Object.assign(window, { taken: input });
      input.remove();
    });
    expect((await upload()).error?.message).toBe(NOT_AS_ASKED);
    expect(await f.evaluate(() => [...(window as unknown as { taken: HTMLInputElement }).taken.files!].map((file) => file.name))).toEqual([]);
    // Made another kind of input since: there is no file input to give a file.
    await named();
    await f.evaluate(() => {
      (document.getElementById("file") as HTMLInputElement).type = "text";
    });
    expect((await upload()).error?.message).toBe(NOT_AS_ASKED);
    // Moved into another frame of the page, which the prompt did not name.
    await named();
    await page.evaluate(() => {
      const [from, to] = ["f", "g"].map((id) => (document.getElementById(id) as HTMLIFrameElement).contentDocument!) as [Document, Document];
      to.body.append(to.adoptNode(from.getElementById("file")!));
    });
    expect((await upload()).error?.message).toBe(NOT_AS_ASKED);
    expect(await g.evaluate(filed)).toEqual([[], []]);
    // Its frame at another address than the prompt said, the page it shows staying as it was.
    await named();
    await f.evaluate(() => history.pushState({}, "", "/elsewhere"));
    expect((await upload()).error?.message).toBe(NOT_AS_ASKED);
    expect(await f.evaluate(filed)).toEqual([[]]);
    // Its frame gone to another page: said in the browser's own words, and the page that came is given nothing.
    await named();
    await f.goto("http://fixture.test/fileinput?second");
    expect((await upload()).error?.type).toBe("browser");
    expect(await f.evaluate(filed)).toEqual([[]]);
    // Gone from its page before the prompt is made: there is no input to name, so the prompt is about the tab's page,
    // and the upload it is about is given to nothing.
    await f.goto("http://fixture.test/fileinput");
    await asksFor(a, () => f.click("#file"));
    await f.evaluate(() => {
      const input = document.getElementById("file")!;
      Object.assign(window, { taken: input });
      input.remove();
    });
    expect(await host.address(a, true)).toBe("http://fixture.test/twoframes");
    expect((await upload()).error?.message).toBe(NOT_ASKED);
    expect(await f.evaluate(() => [...(window as unknown as { taken: HTMLInputElement }).taken.files!].map((file) => file.name))).toEqual([]);
    // Its page closed.
    await named();
    const [popup] = await Promise.all([page.waitForEvent("popup", { timeout: 10_000 }), page.evaluate("void window.open('/fileinput')")]);
    await asksFor(a, () => popup.click("#file"));
    expect(await host.address(a, true)).toBe("http://fixture.test/fileinput");
    await popup.close();
    expect((await upload()).error?.message).toBe(NOT_ASKED);
    // Where the prompt said, as the prompt said: given.
    await named();
    expect(await upload()).toMatchObject({ ok: { files: 1 } });
    expect(await f.evaluate(filed)).toEqual([["report.pdf"]]);
  }, 60_000);

  it("gives an upload its user was asked about to no other input than its own prompt named: not once its agent acted before its files came, nor where another upload was asked about meanwhile", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/twoframes" });
    const page = tabs().get(a)![0]!;
    const [f, g] = ["/fileinput", "/fileinput?second"].map((path) => page.frames().find((frame) => frame.url() === `http://fixture.test${path}`)!) as [Frame, Frame];
    // An upload, by its operation's id, as the host's process runs it.
    const upload = (id: string) =>
      host.perform(launch, ROOT, a, "browser.set_input_files", { files: [{ ...REPORT, name: `${id}.pdf` }] }, new AbortController().signal, id) as ReturnType<typeof op>;
    const holds = async () => [await f.evaluate(filed), await g.evaluate(filed)];
    // Its user is asked about one, by the first frame's input, and allows it. While its files are read, its agent's
    // next act, allowed after it, runs first: and the page makes another input ask.
    await asksFor(a, () => f.click("#file"));
    expect(await host.address(a, true, "asked")).toBe("http://fixture.test/fileinput");
    await op(a, "browser.mouse", { action: "click", x: 5, y: 5, button: "left", clicks: 1 });
    await asksFor(a, () => g.click("#file"));
    // Its files come: the input its prompt named is the session's no more, and they go to no other.
    expect((await upload("asked")).error?.message).toBe(NOT_AS_ASKED);
    expect(await holds()).toEqual([[[]], [[]]]);
    // One nobody was asked about, as in a chat that works freely, goes to the input that asked last.
    expect(await upload("unasked")).toMatchObject({ ok: { files: 1 } });
    expect(await holds()).toEqual([[[]], [["unasked.pdf"]]]);
    // Two asked about one after the other, the second's prompt made while the first's files were read: the
    // first is given to nothing, not to what the second's prompt named; the second to its own.
    await asksFor(a, () => f.click("#file"));
    expect(await host.address(a, true, "first")).toBe("http://fixture.test/fileinput");
    await asksFor(a, () => g.click("#file"));
    expect(await host.address(a, true, "second")).toBe("http://fixture.test/fileinput?second");
    expect((await upload("first")).error?.message).toBe(NOT_AS_ASKED);
    expect(await holds()).toEqual([[[]], [["unasked.pdf"]]]);
    expect(await upload("second")).toMatchObject({ ok: { files: 1 } });
    expect(await holds()).toEqual([[[]], [["second.pdf"]]]);
    // One nobody was asked about takes no input named for another: it goes to what asked last, and the one asked about to its own still.
    await asksFor(a, () => f.click("#file"));
    expect(await host.address(a, true, "third")).toBe("http://fixture.test/fileinput");
    await asksFor(a, () => g.click("#file"));
    expect(await upload("fourth")).toMatchObject({ ok: { files: 1 } });
    expect(await holds()).toEqual([[[]], [["fourth.pdf"]]]);
    expect(await upload("third")).toMatchObject({ ok: { files: 1 } });
    expect(await holds()).toEqual([[["third.pdf"]], [["fourth.pdf"]]]);
    // Asked about once, an upload sent again under its id is one nobody was asked about.
    expect((await upload("third")).error?.message).toBe(NOT_ASKED);
    // The input that asked after the one a prompt named is still the session's next once that one has its files.
    await asksFor(a, () => f.click("#file"));
    expect(await host.address(a, true, "fifth")).toBe("http://fixture.test/fileinput");
    await asksFor(a, () => g.click("#file"));
    expect(await upload("fifth")).toMatchObject({ ok: { files: 1 } });
    expect(await upload("sixth")).toMatchObject({ ok: { files: 1 } });
    expect(await holds()).toEqual([[["fifth.pdf"]], [["sixth.pdf"]]]);
  }, 60_000);

  it("names no input of a page too busy to say where it is, for an upload's prompt, and keeps the session's line no longer than a moment for it", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    const page = tabs().get(a)![0]!;
    await asksFor(a, () => page.click("#file"));
    // The page is busy longer than a prompt waits for its address.
    await page.evaluate("void setTimeout(() => { const until = Date.now() + 4000; while (Date.now() < until) {} }, 0)");
    await new Promise((done) => setTimeout(done, 100));
    const started = performance.now();
    expect(await host.address(a, true)).toBe("http://fixture.test/");
    expect(performance.now() - started).toBeLessThan(3_500);
    // Nothing was named: the upload that prompt is about is given to nothing, once the page answers again.
    await expect.poll(() => within(500, page.evaluate("1")), { timeout: 10_000 }).toBe(1);
    expect((await op(a, "browser.set_input_files", { files: [REPORT] })).error?.message).toBe(NOT_ASKED);
    expect(await page.evaluate(filed)).toEqual([[], []]);
  }, 30_000);

  it("gives nothing to an input that asks only after an upload's prompt was made about a page that had asked for none", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    const page = tabs().get(a)![0]!;
    const upload = () => op(a, "browser.set_input_files", { files: [REPORT] });
    // Nothing has asked: the prompt names the tab's page, and the upload it is about will be refused.
    expect(await host.address(a, true)).toBe("http://fixture.test/");
    // The page makes its input ask while that prompt is open.
    await asksFor(a, () => page.click("#file"));
    expect((await upload()).error?.message).toBe(NOT_ASKED);
    expect(await page.evaluate(filed)).toEqual([[], []]);
    // The next upload, about which nobody was asked, is given to the input that asked last, as before: and its
    // answer says what the page did that its agent has not heard of yet, as any answer that carries notices.
    expect(await upload()).toEqual({ ok: { files: 1, notices: [FILE_ASKED] } });
    expect(await page.evaluate(filed)).toEqual([[], ["report.pdf"]]);
    expect((await op(a, "browser.mouse", { action: "move", x: 5, y: 5 })).ok.notices).toEqual([]);
  }, 30_000);

  it("gives a file input as many files as it takes: one where it takes one, several where it takes several, and none where it asks for a folder", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    const page = tabs().get(a)![0]!;
    const click = () => op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 });
    const upload = (...names: string[]) => op(a, "browser.set_input_files", { files: names.map((name) => ({ ...REPORT, name })) });
    const holds = () => page.evaluate(() => [...(document.getElementById("file") as HTMLInputElement).files!].map((file) => file.name));
    // Two for an input that takes one: neither is given, and the input that asked is still there for one.
    await click();
    expect(await upload("a.pdf", "b.pdf")).toEqual({ error: { type: "browser", message: ONE_FILE } });
    expect(await holds()).toEqual([]);
    expect(await upload("a.pdf")).toMatchObject({ ok: { files: 1 } });
    expect(await holds()).toEqual(["a.pdf"]);
    // An input that takes several is given each, in the order they were named, and its page hears them as a person's choice.
    await page.evaluate(() => {
      const input = document.getElementById("file") as HTMLInputElement;
      input.multiple = true;
      const heard: string[] = [];
      Object.assign(window, { heard });
      for (const kind of ["input", "change"]) input.addEventListener(kind, () => heard.push(`${kind} ${input.files!.length}`));
    });
    await click();
    expect(await upload("c.pdf", "b.pdf")).toMatchObject({ ok: { files: 2 } });
    expect(await holds()).toEqual(["c.pdf", "b.pdf"]);
    expect(await page.evaluate(() => (window as unknown as { heard: string[] }).heard)).toEqual(["input 2", "change 2"]);
    // One that asks for a folder is given no files: what it holds stays.
    await page.evaluate(() => {
      (document.getElementById("file") as HTMLInputElement).webkitdirectory = true;
    });
    await click();
    expect(await upload("d.pdf")).toEqual({ error: { type: "browser", message: A_FOLDER } });
    expect(await holds()).toEqual(["c.pdf", "b.pdf"]);
  }, 30_000);

  it("gives a chat's upload to no file input of another chat's tab, nor to one in a tab no chat owns, whichever asked last", async () => {
    const [a, b] = [session(), session()];
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    await op(b, "browser.navigate", { url: "http://fixture.test/" }, "chat-2");
    const [mine, theirs] = [tabs().get(a)![0]!, tabs().get(b)![0]!];
    const upload = (session: string, root: string) => op(session, "browser.set_input_files", { files: [REPORT] }, root);
    // Another chat's page asks: the last input to ask in the whole browser, and none of this chat's.
    await asksFor(b, () => theirs.click("#file"));
    // Asked about or not, this chat's upload has no input: its prompt names its own tab's page, and nothing is given.
    expect((await upload(a, "chat-1")).error?.message).toBe(NOT_ASKED);
    expect(await host.address(a, true)).toBe("http://fixture.test/");
    expect((await upload(a, "chat-1")).error?.message).toBe(NOT_ASKED);
    expect(await theirs.evaluate(filed)).toEqual([[], []]);
    // Nor under the other chat's session, named by this chat: only the server could send that.
    expect((await upload(b, "chat-1")).error?.message).toBe("This session's tab in the agent's browser on this computer is another chat's");
    expect(await theirs.evaluate(filed)).toEqual([[], []]);
    // A tab its user opened themselves is no session's: a file input there opens the browser's own chooser, and no chat hears it.
    const own = await (await (host as unknown as { running: Promise<BrowserContext> }).running).newPage();
    await own.goto("http://fixture.test/");
    await own.click("#file");
    await expect.poll(() => ownChoosers().length, { timeout: 10_000 }).toBe(1);
    for (const of of [a, b]) expect(await host.address(of, true)).toBe("http://fixture.test/");
    expect((await upload(a, "chat-1")).error?.message).toBe(NOT_ASKED);
    expect(await own.evaluate(filed)).toEqual([[], []]);
    // The other chat's own upload is given to its own page's input, and to no other page's.
    expect(await upload(b, "chat-2")).toMatchObject({ ok: { files: 1 } });
    expect([await mine.evaluate(filed), await theirs.evaluate(filed), await own.evaluate(filed)]).toEqual([[[], []], [[], ["report.pdf"]], [[], []]]);
  }, 30_000);

  it("names an input in a frame with no address of its own by the site that frame runs as, and gives no file to one that runs as none", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/unaddressed" });
    const page = tabs().get(a)![0]!;
    const frameOf = async (id: string) => (await (await page.$(`#${id}`))!.contentFrame())!;
    const upload = () => op(a, "browser.set_input_files", { files: [REPORT] });
    // One the page spells out runs as the page's site: named by it, where the frame's own address names nothing.
    const spelled = await frameOf("spelled");
    expect(spelled.url()).toBe("about:srcdoc");
    await asksFor(a, () => spelled.click("#file"));
    expect(await host.address(a, true)).toBe("http://fixture.test");
    expect(await upload()).toMatchObject({ ok: { files: 1 } });
    expect(await spelled.evaluate(filed)).toEqual([["report.pdf"]]);
    // One the page wrote into an empty frame is at the page's own address.
    const written = await frameOf("written");
    expect(written.url()).toBe("about:blank");
    await asksFor(a, () => written.click("#file"));
    expect(await host.address(a, true)).toBe("http://fixture.test/unaddressed");
    expect(await upload()).toMatchObject({ ok: { files: 1 } });
    // One from a data address runs as no site: there is none to ask its user about. The prompt is told so, in place
    // of an address, and an upload that comes all the same is given nothing.
    const data = await frameOf("data");
    await asksFor(a, () => data.click("#file"));
    expect(await host.address(a, true)).toEqual({ refused: NO_SITE });
    expect((await upload()).error?.message).toBe(NO_SITE);
    expect(await data.evaluate(filed)).toEqual([[]]);
  }, 30_000);

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

  it("takes its user's own download of an address for theirs though the agent's navigation to that address was stopped by their take-over, a moment before or seconds before; and the agent's own navigation that becomes a download for the agent's", async () => {
    const staged: StagedDownload[] = [];
    host = hostWith({ downloaded: (download) => staged.push(download) });
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const open = (host as unknown as { open: Map<unknown, unknown> }).open;
    // The site's own link with `download` to an address, as its user clicks it: the browser says no request of it.
    const link = (to: string) => page.evaluate(`const link = document.createElement('a'); link.href = '${to}'; link.download = ''; document.body.append(link); link.click(); void 0`);
    for (const [address, name, wait] of [["/first-late.bin", "first-late.bin", 0], ["/late.bin", "late.bin", 2_500]] as const) {
      // The agent's own navigation to a download its site has not answered when its user takes the browser over.
      const going = op(a, "browser.navigate", { url: `http://fixture.test${address}` }, "chat-1");
      await new Promise((done) => setTimeout(done, 300));
      host.pause("chat-1", true);
      expect(await within(1_000, going), address).toEqual(PAUSED);
      // Stopped by the take-over: nothing of it is kept for a download to be taken for its answer.
      expect(open.size, address).toBe(0);
      // Their own click on the site's link to that same address: at once, within the second of the stop, or seconds on.
      await new Promise((done) => setTimeout(done, wait));
      const count = staged.length;
      await link(address);
      await expect.poll(() => staged.length, { timeout: 10_000, message: address }).toBe(count + 1);
      expect(staged.at(-1), address).toEqual({ root: "chat-1", session: a, name, path: staged.at(-1)!.path, user: true });
      host.pause("chat-1", false);
      // The agent is told nothing of it.
      expect((await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")).ok.notices, address).toEqual([]);
    }
    // A navigation of the agent's that failed by itself, no take-over stopping it: its site had nothing for it. More
    // than a second on it is no download's beginning either, though the browser has said nothing since.
    firsts = 0;
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/first-empty.bin" }, "chat-1")).error?.type).toBe("browser");
    await new Promise((done) => setTimeout(done, 1_200));
    host.pause("chat-1", true);
    await link("/first-empty.bin");
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(3);
    expect(staged[2]).toEqual({ root: "chat-1", session: a, name: "first-empty.bin", path: staged[2]!.path, user: true });
    host.pause("chat-1", false);
    expect((await op(a, "browser.mouse", { action: "move", x: 1, y: 1 }, "chat-1")).ok.notices).toEqual([]);
    // The agent's own navigation to a download, with nobody holding the browser: no page comes of it, and the file is its own.
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/report.txt" }, "chat-1")).error?.type).toBe("browser");
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(4);
    expect(staged[3]).toEqual({ root: "chat-1", session: a, name: "report.txt", path: staged[3]!.path, user: false });
    expect(open.size).toBe(0);
  }, 60_000);

  it("drops at the one take-over both the input a page asked a file for and the request of the navigation it stops: their user's own download of that address is theirs, no chooser of the browser's own opens, and the upload that comes after the hand back is given to nothing", async () => {
    const staged: StagedDownload[] = [];
    host = hostWith({ downloaded: (download) => staged.push(download) });
    const [a, b] = [session(), session()];
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    // A file input of the chat's page asked: kept for its agent's next upload.
    expect((await op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 }, "chat-1")).ok.notices).toEqual([FILE_ASKED]);
    // A sub-agent's navigation to a download its site has not answered when their user takes the browser over.
    await op(b, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const other = tabs().get(b)![0]!;
    const going = op(b, "browser.navigate", { url: "http://fixture.test/late.bin" }, "chat-1");
    await new Promise((done) => setTimeout(done, 300));
    expect([kept(a) !== undefined, requested(), hears(page), hears(other)]).toEqual([true, ["http://fixture.test/late.bin"], 1, 1]);
    host.pause("chat-1", true);
    // Both are so by the time the take-over returns; their pages are heard a moment more, for no one.
    expect([kept(a), requested(), hears(page), hears(other)]).toEqual([undefined, [], 1, 1]);
    expect(await within(1_000, going)).toEqual(PAUSED);
    // Their own click on the site's link with `download` to that address, in the sub-agent's page, in that moment.
    await other.evaluate("const link = document.createElement('a'); link.href = '/late.bin'; link.download = ''; document.body.append(link); link.click(); void 0");
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(1);
    expect(staged[0]).toEqual({ root: "chat-1", session: b, name: "late.bin", path: staged[0]!.path, user: true });
    await expect.poll(() => [hears(page), hears(other)], { timeout: OWN_CHOOSER_MS + 5_000 }).toEqual([0, 0]);
    expect(ownChoosers()).toEqual([]);
    host.pause("chat-1", false);
    expect([hears(page), hears(other)]).toEqual([1, 1]);
    // Neither session's agent is told of any of it, and the upload has no input.
    expect((await op(a, "browser.mouse", { action: "move", x: 5, y: 5 }, "chat-1")).ok.notices).toEqual([]);
    expect((await op(b, "browser.mouse", { action: "move", x: 5, y: 5 }, "chat-1")).ok.notices).toEqual([]);
    expect((await op(a, "browser.set_input_files", { files: [REPORT] }, "chat-1")).error?.message).toBe(NOT_ASKED);
  }, 30_000);

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

  it("keeps nothing of a browser its user closed, of the file a page asked for or of a navigation it had not answered: an upload after is given to nothing, and their own download of that address in the next browser is theirs", async () => {
    const staged: StagedDownload[] = [];
    host = hostWith({ downloaded: (download) => staged.push(download) });
    const [a, b] = [session(), session()];
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    expect((await op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 }, "chat-1")).ok.notices).toEqual([FILE_ASKED]);
    await op(b, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const going = op(b, "browser.navigate", { url: "http://fixture.test/late.bin" }, "chat-1");
    await new Promise((done) => setTimeout(done, 300));
    const open = (host as unknown as { open: Map<unknown, unknown> }).open;
    expect([kept(a) !== undefined, requested()]).toEqual([true, ["http://fixture.test/late.bin"]]);
    for (const { pid } of processes().filter(({ args }) => !args.some((arg) => arg.startsWith("--type=")))) process.kill(Number(pid), "SIGTERM");
    await expect.poll(() => processes().length, { timeout: 10_000 }).toBe(0);
    expect(((await within(10_000, going)) as { error?: { type: string } }).error?.type).toBe("browser");
    await expect.poll(() => [kept(a), open.size, tabs().size], { timeout: 10_000 }).toEqual([undefined, 0, 0]);
    expect((await op(a, "browser.set_input_files", { files: [REPORT] }, "chat-1")).error?.message).toBe(NOT_ASKED);
    // The next browser, taken over: their own click on the site's link with `download` to that address.
    expect((await op(b, "browser.navigate", { url: "http://fixture.test/" }, "chat-1")).ok).toMatchObject({ opened: true });
    host.pause("chat-1", true);
    await tabs().get(b)![0]!.evaluate("const link = document.createElement('a'); link.href = '/late.bin'; link.download = ''; document.body.append(link); link.click(); void 0");
    await expect.poll(() => staged.length, { timeout: 10_000 }).toBe(1);
    expect(staged[0]).toEqual({ root: "chat-1", session: b, name: "late.bin", path: staged[0]!.path, user: true });
    host.pause("chat-1", false);
    expect((await op(b, "browser.mouse", { action: "move", x: 5, y: 5 }, "chat-1")).ok.notices).toEqual([]);
  }, 60_000);

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

  it("gives no input a file while its user holds the browser, from whichever chat, nor after the hand back one that asked before they took it", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const holds = () => page.evaluate(() => [...(document.getElementById("file") as HTMLInputElement).files!].map((file) => file.name));
    const click = () => op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 }, "chat-1");
    const upload = () => op(a, "browser.set_input_files", { files: [REPORT] }, "chat-1");
    for (const holder of ["chat-1", "chat-2"]) {
      // Its page asks, and an upload's prompt names that input: then its user takes the browser over.
      expect((await click()).ok.notices).toEqual([FILE_ASKED]);
      expect(await host.address(a, true)).toBe("http://fixture.test/");
      host.pause(holder, true);
      expect(await upload(), holder).toEqual(PAUSED);
      expect(await holds(), holder).toEqual([]);
      // Handed back: what the page asked for before is not taken up again, named for a prompt or not.
      host.pause(holder, false);
      expect((await upload()).error?.message, holder).toBe(NOT_ASKED);
      expect((await upload()).error?.message, holder).toBe(NOT_ASKED);
      expect(await holds(), holder).toEqual([]);
    }
    // The page asks again at the agent's own click, and is given the files.
    expect((await click()).ok.notices).toEqual([FILE_ASKED]);
    expect(await upload()).toEqual({ ok: { files: 1, notices: [] } });
    expect(await holds()).toEqual(["report.pdf"]);
    // A prompt made while they hold the browser names nothing, and keeps nothing: an upload nobody is asked about
    // after the hand back goes to what the page asks for then.
    host.pause("chat-1", true);
    expect(await host.address(a, true)).toBe("http://fixture.test/");
    host.pause("chat-1", false);
    await asksFor(a, () => page.click("#file"));
    expect(await upload()).toMatchObject({ ok: { files: 1 } });
    // Handed back before a file input was let be for them: it is not let be after, under the agent's hand.
    host.pause("chat-1", true);
    host.pause("chat-1", false);
    await new Promise((done) => setTimeout(done, OWN_CHOOSER_MS + 500));
    expect(hears(page)).toBe(1);
    expect((await click()).ok.notices).toEqual([FILE_ASKED]);
    await new Promise((done) => setTimeout(done, 1_000));
    expect(ownChoosers()).toEqual([]);
  }, 30_000);

  it("gives the page nothing of an upload in flight when its user takes the browser over: answered paused at once, and not taken up again", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const holds = () => within(500, page.evaluate(() => [...(document.getElementById("file") as HTMLInputElement).files!].map((file) => file.name)));
    expect((await op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 }, "chat-1")).ok.notices).toEqual([FILE_ASKED]);
    // The page is busy a moment, by now: the upload sent meanwhile is still on its way to it when its user takes the browser over.
    await page.evaluate("void setTimeout(() => { const until = Date.now() + 1500; while (Date.now() < until) {} }, 0)");
    await new Promise((done) => setTimeout(done, 100));
    const uploading = op(a, "browser.set_input_files", { files: [REPORT] }, "chat-1");
    await new Promise((done) => setTimeout(done, 300));
    host.pause("chat-1", true);
    expect(await within(1_000, uploading)).toEqual(PAUSED);
    // The page answers again, and what was on its way has had its time: it was given nothing.
    await expect.poll(holds, { timeout: 10_000 }).toEqual([]);
    await new Promise((done) => setTimeout(done, 1_500));
    expect(await holds()).toEqual([]);
    // Nor at the hand back.
    host.pause("chat-1", false);
    expect((await op(a, "browser.mouse", { action: "move", x: 5, y: 5 }, "chat-1")).ok.notices).toEqual([]);
    expect(await holds()).toEqual([]);
    expect((await op(a, "browser.set_input_files", { files: [REPORT] }, "chat-1")).error?.message).toBe(NOT_ASKED);
  }, 30_000);

  it("opens the browser's own chooser for a file input its user clicks while they hold the browser, tells the agent nothing of it and keeps no input of theirs; and none for the agent once it is handed back", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/t/HELD" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const holds = () => page.evaluate(() => [...(document.getElementById("file") as HTMLInputElement).files!].map((file) => file.name));
    const click = () => op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 }, "chat-1");
    const upload = () => op(a, "browser.set_input_files", { files: [REPORT] }, "chat-1");
    // While the agent drives, a file input opens no chooser of the browser's: the page's ask is heard, for an upload.
    expect((await click()).ok.notices).toEqual([FILE_ASKED]);
    expect(ownChoosers()).toEqual([]);
    host.pause("chat-1", true);
    expect(await host.show("chat-1")).toBe(true);
    await expect.poll(front, { timeout: 5_000 }).toBe("HELD");
    // Once what the agent was doing at the take-over has had its moment, a file input is its user's as in any browser.
    await expect.poll(() => hears(page), { timeout: OWN_CHOOSER_MS + 5_000 }).toBe(0);
    const [x, y] = await onScreen(page, "file");
    asUser("focus", xwindow()!.id);
    asUser("click", String(x), String(y));
    await expect.poll(() => ownChoosers().length, { timeout: 10_000 }).toBe(1);
    // Handed back: the agent is told nothing of what they did there, and no input of theirs is kept for an upload.
    host.pause("chat-1", false);
    expect(hears(page)).toBe(1);
    expect((await op(a, "browser.mouse", { action: "move", x: 5, y: 5 }, "chat-1")).ok.notices).toEqual([]);
    expect((await upload()).error?.message).toBe(NOT_ASKED);
    expect(await holds()).toEqual([]);
    // The agent's own click opens none of the browser's own, as before: the page's ask is heard again, and answered by an upload.
    expect((await click()).ok.notices).toEqual([FILE_ASKED]);
    await new Promise((done) => setTimeout(done, 1_500));
    expect(ownChoosers()).toHaveLength(1);
    expect(await upload()).toEqual({ ok: { files: 1, notices: [] } });
    expect(await holds()).toEqual(["report.pdf"]);
  }, 60_000);

  it("leaves a file input to its user in a window a session's page opens while they hold the browser too, and hears it for the agent from the hand back", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    host.pause("chat-1", true);
    const [popup] = await Promise.all([page.waitForEvent("popup", { timeout: 10_000 }), page.evaluate("void window.open('/fileinput')")]);
    await expect.poll(() => tabs().get(a)!.includes(popup), { timeout: 5_000 }).toBe(true);
    await popup.waitForLoadState("load");
    expect(hears(popup)).toBe(0);
    await expect.poll(() => hears(page), { timeout: OWN_CHOOSER_MS + 5_000 }).toBe(0);
    host.pause("chat-1", false);
    expect([hears(page), hears(popup)]).toEqual([1, 1]);
    // The window is the session's newest page: its agent's click there is heard, and its upload answers it.
    expect((await op(a, "browser.mouse", { action: "click", x: 60, y: 40, button: "left", clicks: 1 }, "chat-1")).ok.notices).toEqual([FILE_ASKED]);
    expect(await op(a, "browser.set_input_files", { files: [REPORT] }, "chat-1")).toEqual({ ok: { files: 1, notices: [] } });
    expect(await popup.evaluate(() => [...(document.getElementById("file") as HTMLInputElement).files!].map((file) => file.name))).toEqual(["report.pdf"]);
    expect(ownChoosers()).toEqual([]);
  }, 30_000);

  it("opens no chooser of the browser's own for a page that asks for a file by itself once its user has taken the browser over, on what the agent's last click gave it: asked a moment after the take-over, or time after time", async () => {
    const [a, b] = [session(), session()];
    // The agent's click arms one page to ask 1.8 s on, and its user takes the browser over right after it.
    await op(a, "browser.navigate", { url: "http://fixture.test/asks/once" }, "chat-1");
    const once = tabs().get(a)![0]!;
    await op(a, "browser.mouse", { action: "click", x: 60, y: 40, button: "left", clicks: 1 }, "chat-1");
    host.pause("chat-1", true);
    await expect.poll(() => within(500, once.evaluate(() => (window as unknown as { asked: number }).asked)), { timeout: 10_000 }).toBe(1);
    await new Promise((done) => setTimeout(done, 1_500));
    expect(ownChoosers()).toEqual([]);
    host.pause("chat-1", false);
    // Another page asks every 2 s from the agent's one click on: each ask it is heard at gives it leave for the next.
    // Taken over from another chat, some seconds on, it opens none however long it keeps asking.
    await op(b, "browser.navigate", { url: "http://fixture.test/asks/often" }, "chat-1");
    const often = tabs().get(b)![0]!;
    const asked = () => within(500, often.evaluate(() => (window as unknown as { asked: number }).asked));
    await op(b, "browser.mouse", { action: "click", x: 60, y: 40, button: "left", clicks: 1 }, "chat-1");
    await expect.poll(asked, { timeout: 10_000 }).toBeGreaterThanOrEqual(2);
    host.pause("chat-2", true);
    const taken = (await asked()) as number;
    await expect.poll(asked, { timeout: 20_000, interval: 500 }).toBeGreaterThanOrEqual(taken + 5);
    expect(ownChoosers()).toEqual([]);
    // Heard still, more than five seconds after the take-over: it has not been quiet.
    expect([hears(often), hears(once)]).toEqual([1, 0]);
    // Nothing of it is kept for an upload, and its agent is told nothing of what it asked for meanwhile.
    expect(kept(b)).toBeUndefined();
    // It stops asking. Five quiet seconds on, its file input is its user's, as in any browser.
    await often.evaluate("clearInterval(window.asking)");
    expect(await host.show("chat-1")).toBe(true);
    await often.bringToFront();
    await expect.poll(front, { timeout: 5_000 }).toBe("OFTEN");
    await expect.poll(() => hears(often), { timeout: OWN_CHOOSER_MS + 5_000 }).toBe(0);
    const [x, y] = await onScreen(often, "file");
    asUser("focus", xwindow()!.id);
    asUser("click", String(x), String(y));
    await expect.poll(() => ownChoosers().length, { timeout: 10_000 }).toBe(1);
  }, 90_000);

  it("keeps no input for an upload that asked before a hand back and is heard of only after it, its page having been busy: not one the agent's click opened before the take-over, nor one its user's own hand opened while they held the browser", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/asks/busy" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const unseen = (host as unknown as { unseen: Map<string, string[]> }).unseen;
    const upload = () => op(a, "browser.set_input_files", { files: [REPORT] }, "chat-1");
    // The page answers again, and what it had to say of before has been heard.
    const answered = async () => {
      await expect.poll(() => within(500, page.evaluate("1")), { timeout: 15_000 }).toBe(1);
      await new Promise((done) => setTimeout(done, 500));
    };
    // The agent clicks the file input, and the page is busy from that click on: taken over 0.4 s after it, and handed back at 1 s.
    const clicking = op(a, "browser.mouse", { action: "click", x: 60, y: 110, button: "left", clicks: 1 }, "chat-1");
    await new Promise((done) => setTimeout(done, 400));
    host.pause("chat-1", true);
    await within(1_000, clicking);
    await new Promise((done) => setTimeout(done, 600));
    host.pause("chat-1", false);
    await answered();
    expect([kept(a), unseen.get(a) ?? []]).toEqual([undefined, []]);
    expect((await upload()).error?.message).toBe(NOT_ASKED);
    // Their own click on it while they hold the browser, in the seconds its pages are still heard; handed back before the page said so.
    host.pause("chat-1", true);
    await page.click("#file");
    await new Promise((done) => setTimeout(done, 300));
    host.pause("chat-1", false);
    await answered();
    expect([kept(a), unseen.get(a) ?? []]).toEqual([undefined, []]);
    expect((await upload()).error?.message).toBe(NOT_ASKED);
    expect(await page.evaluate(filed)).toEqual([[]]);
    // What the page asks for at the agent's click once the browser is its again is kept as ever.
    await asksFor(a, () => op(a, "browser.mouse", { action: "click", x: 60, y: 110, button: "left", clicks: 1 }, "chat-1"));
    await answered();
    expect(await upload()).toMatchObject({ ok: { files: 1 } });
    expect(await page.evaluate(filed)).toEqual([["report.pdf"]]);
  }, 90_000);

  it("keeps for an upload what a page asks for at a click the agent sends once the browser is its again, though the page is slow to answer after the hand back; and not what a click sent before the take-over makes it ask for after", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const unseen = (host as unknown as { unseen: Map<string, string[]> }).unseen;
    const busy = async (ms: number) => {
      await page.evaluate(`void setTimeout(() => { const until = Date.now() + ${ms}; while (Date.now() < until) {} }, 0)`);
      await new Promise((done) => setTimeout(done, 100));
    };
    const click = () => op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 }, "chat-1");
    // The agent's click on the file input waits on a busy page when its user takes the browser over, and still when
    // they hand it back: it reaches the page after that, and the page asks then.
    await busy(3_000);
    const clicking = click();
    await new Promise((done) => setTimeout(done, 300));
    host.pause("chat-1", true);
    expect(await within(1_000, clicking)).toEqual(PAUSED);
    await new Promise((done) => setTimeout(done, 600));
    host.pause("chat-1", false);
    await expect.poll(() => within(500, page.evaluate("1")), { timeout: 15_000 }).toBe(1);
    await new Promise((done) => setTimeout(done, 1_000));
    // The operation was answered paused: what it did after is kept for no upload, and its agent told nothing of it.
    expect([kept(a), unseen.get(a) ?? []]).toEqual([undefined, []]);
    // Taken over and handed back while the page is busy again: the agent's next click is sent only once the page has
    // answered for what came before, so what it asks for then is the agent's own, and kept.
    await busy(2_500);
    host.pause("chat-1", true);
    host.pause("chat-1", false);
    await asksFor(a, click);
    expect(await op(a, "browser.set_input_files", { files: [REPORT] }, "chat-1")).toMatchObject({ ok: { files: 1 } });
    expect(await page.evaluate(filed)).toEqual([[], ["report.pdf"]]);
  }, 60_000);

  it("lets a page be only five seconds after what the agent was doing there has reached it: a click still on its way to a busy page at the take-over arms no chooser of the browser's own", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/asks/later" }, "chat-1");
    const page = tabs().get(a)![0]!;
    // The page is busy six seconds, by now: the agent's click on its button waits on it.
    await page.evaluate("void setTimeout(() => { const until = Date.now() + 6000; while (Date.now() < until) {} }, 0)");
    await new Promise((done) => setTimeout(done, 100));
    const clicking = op(a, "browser.mouse", { action: "click", x: 60, y: 40, button: "left", clicks: 1 }, "chat-1");
    // Another page's script, of the agent's too, runs a second and a half more, and asks for nothing.
    const b = session();
    await op(b, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const scripted = tabs().get(b)![0]!;
    const running = op(b, "browser.evaluate", { code: "await new Promise((done) => setTimeout(done, 1500)); return 1;" }, "chat-1");
    await new Promise((done) => setTimeout(done, 300));
    host.pause("chat-1", true);
    const taken = performance.now();
    expect([await within(1_000, clicking), await within(1_000, running)]).toEqual([PAUSED, PAUSED]);
    // That page is let be five seconds after its script ended, and not sooner.
    await expect.poll(() => hears(scripted), { timeout: OWN_CHOOSER_MS + 6_000 }).toBe(0);
    expect(performance.now() - taken).toBeGreaterThan(OWN_CHOOSER_MS + 1_000);
    // The click reaches the page once it is free, more than five seconds after the take-over, and the page asks
    // 3.5 s after that: less than five after the click, which is what gave it leave to.
    await expect.poll(() => within(500, page.evaluate(() => (window as unknown as { asked: number }).asked)), { timeout: 20_000 }).toBe(1);
    expect(performance.now() - taken).toBeGreaterThan(OWN_CHOOSER_MS + 3_000);
    await new Promise((done) => setTimeout(done, 1_500));
    expect(ownChoosers()).toEqual([]);
  }, 60_000);

  it("opens no chooser of the browser's own for what the agent was doing when its user took the browser over: the button of a drag on a file input comes up heard, and kept for no upload", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const unseen = (host as unknown as { unseen: Map<string, string[]> }).unseen;
    // A press and a release on a file input ask for a file, as a click does, though the pointer moved between them: heard a moment after.
    await op(a, "browser.mouse", { action: "drag", path: [[45, 210], [50, 210], [55, 210]], button: "left" }, "chat-1");
    await expect.poll(() => unseen.get(a), { timeout: 5_000 }).toEqual([FILE_ASKED]);
    expect((await op(a, "browser.mouse", { action: "move", x: 60, y: 210 }, "chat-1")).ok.notices).toEqual([FILE_ASKED]);
    // A long drag there: its user takes the browser over while it moves, and its button comes up after that, under their hand.
    await page.evaluate(() => {
      const seen = { moves: 0, ups: 0 };
      Object.assign(window, { seen });
      addEventListener("mousemove", () => (seen.moves += 1));
      addEventListener("mouseup", () => (seen.ups += 1));
    });
    const seen = () => page.evaluate(() => (window as unknown as { seen: { moves: number; ups: number } }).seen);
    const path = Array.from({ length: 1_000 }, (_, n) => [45 + (n % 30), 210]);
    const dragging = op(a, "browser.mouse", { action: "drag", path, button: "left" }, "chat-1");
    await expect.poll(async () => (await seen()).moves, { timeout: 10_000 }).toBeGreaterThanOrEqual(2);
    host.pause("chat-1", true);
    expect(await within(1_000, dragging)).toEqual(PAUSED);
    await expect.poll(async () => (await seen()).ups, { timeout: 5_000 }).toBe(1);
    await new Promise((done) => setTimeout(done, OWN_CHOOSER_MS + 2_000));
    expect(ownChoosers()).toEqual([]);
    // What it asked for then is nobody's to answer: its agent is told nothing of it, and has no input to give a file.
    host.pause("chat-1", false);
    expect((await op(a, "browser.mouse", { action: "move", x: 5, y: 5 }, "chat-1")).ok.notices).toEqual([]);
    expect((await op(a, "browser.set_input_files", { files: [REPORT] }, "chat-1")).error?.message).toBe(NOT_ASKED);
  }, 30_000);

  it("looks last at whether its user holds the browser once an upload's files are ready in the page, gives them by no step that reaches a busy page late, and says so where they were given as it was taken over", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    const holds = () => within(500, page.evaluate(() => [...(document.getElementById("file") as HTMLInputElement).files!].map((file) => file.name)));
    const upload = (name: string) => op(a, "browser.set_input_files", { files: [{ ...REPORT, name }] }, "chat-1");
    const busy = async () => {
      await page.evaluate("void setTimeout(() => { const until = Date.now() + 1500; while (Date.now() < until) {} }, 0)");
      await new Promise((done) => setTimeout(done, 100));
    };
    // The page asks for a file; and *then* runs once an upload's files are ready in the page, before the host's last look.
    const ready = async (then: (made: JSHandle) => unknown) => {
      await asksFor(a, () => op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 }, "chat-1"));
      const input = kept(a)!.element();
      const make = input.evaluateHandle.bind(input) as (...args: unknown[]) => Promise<JSHandle>;
      Object.assign(input, { evaluateHandle: async (...args: unknown[]) => {
        const made = await make(...args);
        await then(made);
        return made;
      } });
    };
    // Taken over once they are ready, before the look: the page is given none of them.
    await ready(() => host.pause("chat-1", true));
    expect(await within(1_000, upload("first.pdf"))).toEqual(PAUSED);
    await new Promise((done) => setTimeout(done, 500));
    expect(await holds()).toEqual([]);
    host.pause("chat-1", false);
    // Taken over while the step that gives them waits on a page too busy to take it: it gives nothing once it runs, and is not sent again.
    await ready(async () => {
      await busy();
      setTimeout(() => host.pause("chat-1", true), 300);
    });
    expect(await within(2_000, upload("second.pdf"))).toEqual(PAUSED);
    await expect.poll(holds, { timeout: 10_000 }).toEqual([]);
    await new Promise((done) => setTimeout(done, 1_000));
    expect(await holds()).toEqual([]);
    host.pause("chat-1", false);
    // A page as busy with nobody taking the browser over is given them once it can take the step in time.
    await ready(busy);
    expect(await within(10_000, upload("third.pdf"))).toMatchObject({ ok: { files: 1 } });
    expect(await holds()).toEqual(["third.pdf"]);
    // Taken over just as the page took them: answered paused all the same, and its agent told that the page has them.
    await ready((made) => {
      const give = made.evaluate.bind(made) as (...args: unknown[]) => Promise<unknown>;
      Object.assign(made, { evaluate: async (...args: unknown[]) => {
        const came = await give(...args);
        host.pause("chat-1", true);
        return came;
      } });
    });
    expect(await within(2_000, upload("fourth.pdf"))).toEqual(PAUSED);
    expect(await holds()).toEqual(["fourth.pdf"]);
    host.pause("chat-1", false);
    expect((await op(a, "browser.mouse", { action: "move", x: 5, y: 5 }, "chat-1")).ok.notices).toContain(GIVEN_AS_TAKEN);
  }, 60_000);

  it("hears a file input for the agent from the moment the browser is handed back: its click right after opens no chooser of the browser's own, time after time", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    const page = tabs().get(a)![0]!;
    for (let round = 0; round < 5; round += 1) {
      host.pause("chat-1", true);
      await expect.poll(() => hears(page), { timeout: OWN_CHOOSER_MS + 5_000 }).toBe(0);
      host.pause("chat-1", false);
      // At once: no operation of the agent's is let through before its pages are heard again.
      await asksFor(a, () => op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 }, "chat-1"));
    }
    await new Promise((done) => setTimeout(done, 1_500));
    expect(ownChoosers()).toEqual([]);
  }, 60_000);

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
