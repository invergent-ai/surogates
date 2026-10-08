// The browser host with the user's own browser: Chrome or Edge from its .deb, headed on xvfb's
// display, or the one SUROGATE_TEST_BROWSER names. Behind SUROGATE_BROWSER_TESTS=1, as the VM tests
// are behind theirs, and skipped where neither browser is installed. Run apart from the user's
// session (test/isolated.sh), as
//   npm run test:browser -- test/browser-host.test.ts
// With the flag set anywhere else, they fail before any browser is launched.

import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, readlinkSync, rmSync, symlinkSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { connect as connectTcp } from "node:net";
import { tmpdir, userInfo } from "node:os";
import { join } from "node:path";

import type { BrowserContext } from "playwright-core";
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { BrowserHost, type BrowserHostOptions, FILE_ASKED, type Launch, PROXY_BYPASSED, WEAKENING } from "../src/browser/host.js";
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
    if (req.url === "/second") return void res.writeHead(200, { "content-type": "text/html" }).end("<title>Second</title>");
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
const script = async (session: string, code: string) => (await op(session, "browser.evaluate", { code })).ok?.value;
const session = () => `session-${(next += 1)}`;
// What *work* answers within *ms*, or "late".
const within = <T>(ms: number, work: Promise<T>) => Promise.race([work, new Promise<"late">((done) => setTimeout(() => done("late"), ms))]);

// How many pages the running browser has.
const pages = async () => (await (host as unknown as { running: Promise<BrowserContext> }).running).pages().length;

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
      expect(notIsolated({ TMPDIR: join(scratch, "tmp") })).not.toContain("TMPDIR is not a scratch folder");
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
      const a = session();
      expect((await op(a, "browser.navigate", { url: `http://127.0.0.1:${ports.canary}/` })).error?.type).toBe("browser");
      expect(await op(a, "browser.evaluate", { code: "return 1;" })).toEqual({ ok: { value: 1 } });
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

  it("opens no file dialog for a file input, keeps no download, and tells the agent of each", async () => {
    const a = session();
    await op(a, "browser.navigate", { url: "http://fixture.test/" });
    expect((await op(a, "browser.mouse", { action: "click", x: 60, y: 210, button: "left", clicks: 1 })).ok.notices).toEqual([FILE_ASKED]);
    const download = await op(a, "browser.mouse", { action: "click", x: 60, y: 255, button: "left", clicks: 1 });
    await expect.poll(async () => {
      const said = (await op(a, "browser.mouse", { action: "move", x: 1, y: 1 })).ok.notices as string[];
      return [...download.ok.notices, ...said].join(" ");
    }).toContain(`("report.txt")`);
    expect(readdirSync(profile).some((name) => name.includes("report"))).toBe(false);
  });

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
    expect(await script(b, "return document.title;")).toBe("Second");
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
    expect(await script(b, "return document.title;")).toBe("Second");
  }, 30_000);

  it("closes every tab of a deleted chat's sessions, and no other chat's", async () => {
    const [a, child, b] = [session(), session(), session()];
    await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1");
    await op(child, "browser.navigate", { url: "http://fixture.test/second" }, "chat-1");
    await op(b, "browser.navigate", { url: "http://fixture.test/second" }, "chat-2");
    expect(await pages()).toBe(3);
    await host.forget("chat-1");
    expect(await pages()).toBe(1);
    expect(await script(b, "return document.title;")).toBe("Second");
    expect((await op(a, "browser.navigate", { url: "http://fixture.test/" }, "chat-1")).ok?.opened).toBe(true);
  });
});
