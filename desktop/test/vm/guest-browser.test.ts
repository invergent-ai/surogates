// A chat's own server opened in the user's own browser (spec, Section 5): the guest under QEMU and
// KVM, a server one of its roots started, and Chrome or Edge from its .deb, headed on xvfb's display,
// through the browser host's proxy and the VM manager's door. Behind SUROGATE_VM_TESTS=1 and
// SUROGATE_BROWSER_TESTS=1, apart from the user's session:
//   npm run build && SUROGATE_VM_TESTS=1 sh test/isolated.sh npx vitest run test/vm/guest-browser.test.ts

import { mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { connect, createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { Launch } from "../../src/browser/client.js";
import { BrowserHost } from "../../src/browser/host.js";
import { MAX_INBOUND } from "../../src/guest/protocol.js";
import { DOOR } from "../../src/vm/inbound.js";
import { VmManager, type VmOptions } from "../../src/vm/manager.js";
import { isolated, TEST_BROWSER } from "../isolated.js";
import { agentDisk, background, folderOf, IMAGE, KVM, needsKvm, OTHER, ROOT, signal, until, USER } from "./guest-support.js";

const run = TEST_BROWSER !== undefined && process.env.SUROGATE_BROWSER_TESTS === "1" && process.env.SUROGATE_VM_TESTS === "1";

describe.skipIf(!run)("a chat's own server in the user's browser, through the guest", { timeout: 120_000 }, () => {
  const KEY = "a7".repeat(32);
  // A second server of the first chat's, which answers without end, and a third that says how many of its answers are open.
  const ENDLESS = 8102;
  const COUNT = 8103;
  // Its live sockets' server says how many it has open on LIVE_COUNT, and HELD takes connections and says nothing.
  const LIVE_COUNT = 8105;
  const HELD = 8106;
  let dir: string;
  let options: VmOptions;
  let manager: VmManager;
  let host: BrowserHost;
  let launch: Launch;
  // This computer's own service, on the port the chats' servers take in their sandboxes, on both of its loopback's families.
  let own: Server[];
  let port: number;
  let hits: number;
  // The port of the first chat's live sockets, which a service of this computer's own has too.
  let live: number;
  const door = () => join(options.run, DOOR);
  const command = (root: string, kind: string, args: Record<string, unknown>) =>
    manager.perform({ id: `${kind}-${Math.random()}`, root, folder: folderOf(join(dir, root === ROOT ? "a" : "b")), kind, args }, signal());
  const browse = (kind: string, args: Record<string, unknown> = {}, root = ROOT, session = root) =>
    host.perform(launch, root, session, kind, args, signal()) as Promise<{ ok?: any; error?: { type: string; message: string } }>;
  const listens = async (root: string, at: number) => {
    let up = false;
    await until(() => up || (void manager.listening(root, at).then((yes) => (up = yes === true)), false), 30_000);
  };
  // Each chat's server, python's own, serving its folder, once it listens.
  const serve = async (root: string) => {
    expect(await command(root, "start", background(`python3 -m http.server ${port} --bind 127.0.0.1`))).toMatchObject({ ok: { session_id: expect.any(String) } });
    await listens(root, port);
  };
  // How many answers the first chat's endless server has open, or its live sockets' server, once it is *open*, or after three seconds.
  const open = async (wanted: number, at = COUNT) => {
    const count = async () => Number(((await command(ROOT, "run", { command: `curl -sS --noproxy '*' --max-time 5 http://127.0.0.1:${at}/`, workdir: null, timeout: 30 })) as { ok?: { output?: string } }).ok?.output);
    let now = await count();
    for (const began = Date.now(); now !== wanted && Date.now() - began < 3_000; now = await count()) await new Promise((done) => setTimeout(done, 100));
    return now;
  };
  // The page reads an answer of the endless server's, and goes on reading.
  const READING = `fetch("http://localhost:${ENDLESS}/").then(async (answer) => { for (const reader = answer.body.getReader(); !(await reader.read()).done;); }).catch(() => {}); return 1;`;

  // A port a service of this computer's own listens on, on both of its loopback's families: a dial that strays to either is heard.
  const ownPort = async () => {
    for (;;) {
      const [six, four] = [0, 1].map(() => createServer((socket) => {
        hits += 1;
        socket.destroy();
      })) as [Server, Server];
      await new Promise<void>((done) => six.listen(0, "::1", done));
      const at = (six.address() as { port: number }).port;
      // The port IPv6 gave may be taken on IPv4: another is tried.
      if (await new Promise<boolean>((done) => four.once("error", () => done(false)).listen(at, "127.0.0.1", () => done(true)))) {
        own.push(six, four);
        return at;
      }
      await new Promise<void>((done) => six.close(() => done()));
    }
  };
  // The page opens a socket to the first chat's live server, kept as window.live: what it heard first, or that none opened.
  const LIVE = (path: string) => `return new Promise((done) => {
  const socket = window.live = new WebSocket("ws://localhost:${live}/${path}");
  window.heard = 0;
  window.ended = new Promise((ended) => { socket.onclose = (event) => { ended("closed " + event.code + " " + event.wasClean); done("no socket"); }; });
  socket.onmessage = (event) => { window.heard += 1; done(event.data); };
});`;
  // A connection held open through the door to the first chat's silent server, as the browser's proxy knocks: once the door has answered.
  const hold = () => new Promise<Socket>((resolve, reject) => {
    const socket = connect({ path: door() });
    socket.on("error", reject);
    socket.once("data", (chunk: Buffer) => (chunk.toString() === "200\n" ? resolve(socket) : reject(new Error(chunk.toString()))));
    socket.write(`${KEY} ${HELD}\n`);
  });

  beforeAll(async () => {
    needsKvm();
    isolated();
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-browser-")));
    for (const [name, title] of [["a", "The first chat's page"], ["b", "The second chat's page"]] as const) {
      mkdirSync(join(dir, name));
      writeFileSync(join(dir, name, "index.html"), `<title>${title}</title><script>fetch("/index.html").then((answer) => { document.title += " " + answer.status; });</script>`);
    }
    writeFileSync(join(dir, "a", "endless.js"), `let open = 0;
const http = require("node:http");
http.createServer((req, res) => { open += 1; res.on("close", () => { open -= 1; }); res.setHeader("access-control-allow-origin", "*"); const part = Buffer.alloc(1 << 16, 97); const more = () => { while (!res.destroyed && res.write(part)); }; res.on("drain", more); more(); }).listen(${ENDLESS}, "127.0.0.1");
http.createServer((req, res) => res.end(String(open))).listen(${COUNT}, "127.0.0.1");`);
    hits = 0;
    own = [];
    port = await ownPort();
    live = await ownPort();
    // The first chat's live sockets, as a development server's: each is told "reload" as it opens, one asked for at
    // /ticking is sent a frame ten times a second after, and any other hears no more.
    writeFileSync(join(dir, "a", "live.js"), `let open = 0;
const http = require("node:http");
const frame = (text) => Buffer.concat([Buffer.from([0x81, text.length]), Buffer.from(text)]);
http.createServer().on("upgrade", (req, socket) => {
  const accept = require("node:crypto").createHash("sha1").update(req.headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest("base64");
  socket.write("HTTP/1.1 101 Switching Protocols\\r\\nUpgrade: websocket\\r\\nConnection: Upgrade\\r\\nSec-WebSocket-Accept: " + accept + "\\r\\n\\r\\n");
  open += 1;
  const ticking = req.url === "/ticking" ? setInterval(() => socket.write(frame("tick")), 100) : undefined;
  // As a WebSocket server does: its peer's end is the connection's, and a close is answered with its own.
  socket.on("error", () => {}).on("close", () => { open -= 1; clearInterval(ticking); }).on("end", () => socket.destroy());
  socket.on("data", (sent) => { if ((sent[0] & 15) === 8) socket.end(Buffer.from([0x88, 0])); });
  socket.write(frame("reload"));
}).listen(${live}, "127.0.0.1");
http.createServer((req, res) => res.end(String(open))).listen(${LIVE_COUNT}, "127.0.0.1");
require("node:net").createServer((socket) => socket.on("error", () => {}).resume()).listen(${HELD}, "127.0.0.1");`);
    options = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"), user: USER, kvm: KVM,
    };
    manager = new VmManager(options);
    for (const root of [ROOT, OTHER]) expect(await command(root, "run", { command: "true", workdir: null, timeout: 30 })).toMatchObject({ ok: { returncode: 0 } });
    await serve(ROOT);
    await serve(OTHER);
    expect(await command(ROOT, "start", background("node endless.js"))).toMatchObject({ ok: { session_id: expect.any(String) } });
    await listens(ROOT, COUNT);
    expect(await command(ROOT, "start", background("node live.js"))).toMatchObject({ ok: { session_id: expect.any(String) } });
    await listens(ROOT, HELD);
    launch = { executable: TEST_BROWSER ?? "", profile: mkdtempSync(join(tmpdir(), "sb-profile-")) };
    host = new BrowserHost();
  }, 120_000);

  afterAll(async () => {
    await host?.close();
    await manager?.stop();
    await Promise.all((own ?? []).map((server) => new Promise<void>((done) => server.close(() => done()))));
    rmSync(options.run, { recursive: true, force: true });
    rmSync(launch.profile, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("opens the page of the chat the port is forwarded to, its own requests too, and never this computer's own service on that port", async () => {
    manager.forwards(KEY, [[port, ROOT]]);
    host.forwards([port], door(), KEY, []);
    expect(await browse("browser.navigate", { url: `http://localhost:${port}/` })).toMatchObject({ ok: { url: `http://localhost:${port}/`, opened: true } });
    await expect.poll(async () => (await browse("browser.evaluate", { code: "return document.title;" })).ok?.value, { timeout: 10_000 }).toBe("The first chat's page 200");
    // The other chat listens on the same port in its own sandbox: forwarded to it, the browser opens its page, by either of the loopback's addresses.
    manager.forwards(KEY, [[port, OTHER]]);
    expect((await browse("browser.navigate", { url: `http://127.0.0.1:${port}/` })).ok?.title).toMatch(/^The second chat's page/);
    expect((await browse("browser.navigate", { url: `http://[::1]:${port}/` })).ok?.title).toMatch(/^The second chat's page/);
    expect(hits).toBe(0);
  });

  it("opens nothing once the port is taken back, in the browser's proxy or at the sandbox's door", async () => {
    manager.forwards(KEY, [[port, ROOT]]);
    host.forwards([], door(), KEY, []);
    // Each at an address the browser has not kept a copy of: a page it has in its cache it shows without asking anyone.
    expect(await browse("browser.navigate", { url: `http://localhost:${port}/?taken-back` })).toEqual({
      error: { type: "browser", message: `The agent's browser opens a server a chat started only once its user has allowed that port for the chat (port ${port})` },
    });
    // The proxy alone would let it through: the door does not.
    manager.forwards(KEY, []);
    host.forwards([port], door(), KEY, []);
    expect(await browse("browser.navigate", { url: `http://localhost:${port}/?forgotten` })).toEqual({
      error: { type: "browser", message: `The sandbox has not been told that the agent's browser may open port ${port} yet. Open it again in a moment.` },
    });
    // Forwarded again, to a port of the chat's that nothing listens on.
    const quiet = port === 65_535 ? port - 1 : port + 1;
    manager.forwards(KEY, [[quiet, ROOT]]);
    host.forwards([quiet], door(), KEY, []);
    expect(await browse("browser.navigate", { url: `http://localhost:${quiet}/` })).toEqual({
      error: { type: "browser", message: `Nothing answers on port ${quiet} of the chat's servers now: its server is not running in the chat's sandbox` },
    });
    expect(hits).toBe(0);
  });

  it("lets go, at the chat's server too, of what the browser lets go: a tab closed while it reads an answer, and a browser killed with answers under way", async () => {
    manager.forwards(KEY, [[port, ROOT], [ENDLESS, ROOT]]);
    host.forwards([port, ENDLESS], door(), KEY, []);
    expect(await open(0)).toBe(0);
    expect((await browse("browser.navigate", { url: `http://localhost:${port}/?reading` })).ok?.title).toMatch(/^The first chat's page/);
    expect((await browse("browser.evaluate", { code: READING })).ok?.value).toBe(1);
    expect(await open(1)).toBe(1);
    expect(await browse("browser.close")).toEqual({ ok: { closed: true } });
    const closed = await open(0);
    // Two tabs reading, and the browser's main process killed under them.
    for (const session of ["one", "two"]) {
      expect((await browse("browser.navigate", { url: `http://localhost:${port}/?${session}` }, ROOT, session)).ok?.title).toMatch(/^The first chat's page/);
      expect((await browse("browser.evaluate", { code: READING }, ROOT, session)).ok?.value).toBe(1);
    }
    expect(await open(2)).toBe(2);
    const main = readdirSync("/proc").filter((pid) => /^\d+$/.test(pid)).filter((pid) => {
      try {
        const args = readFileSync(`/proc/${pid}/cmdline`, "utf8");
        return args.includes(launch.profile) && !args.includes("--type=");
      } catch {
        return false;
      }
    });
    expect(main).toHaveLength(1);
    process.kill(Number(main[0]), "SIGKILL");
    const killed = await open(0);
    console.log(`open at the chat's server after a tab was closed while it read an answer: ${closed}; after the browser was killed with two under way: ${killed}`);
    expect([closed, killed]).toEqual([0, 0]);
    // The browser launched again opens the chat's page as before.
    await expect.poll(async () => (await browse("browser.navigate", { url: `http://localhost:${port}/?after` })).ok?.title, { timeout: 30_000 }).toMatch(/^The first chat's page/);
    expect(hits).toBe(0);
  });

  it("carries a page's live socket into the chat's sandbox, and lets go of it at the chat's server when the page closes it, its tab is closed, its port is taken back or the browser is killed", async () => {
    manager.forwards(KEY, [[port, ROOT], [live, ROOT]]);
    host.forwards([port, live], door(), KEY, []);
    const count = (wanted: number) => open(wanted, LIVE_COUNT);
    expect(await count(0)).toBe(0);
    expect((await browse("browser.navigate", { url: `http://localhost:${port}/?live` })).ok?.title).toMatch(/^The first chat's page/);
    expect((await browse("browser.evaluate", { code: LIVE("quiet") })).ok?.value).toBe("reload");
    expect(await count(1)).toBe(1);
    // The page closes it, as the two ends agree.
    expect((await browse("browser.evaluate", { code: "window.live.close(); return window.ended;" })).ok?.value).toBe("closed 1005 true");
    const closed = await count(0);
    // Its tab is closed with the socket open.
    expect((await browse("browser.evaluate", { code: LIVE("quiet") })).ok?.value).toBe("reload");
    expect(await count(1)).toBe(1);
    expect(await browse("browser.close")).toEqual({ ok: { closed: true } });
    const tabClosed = await count(0);
    // Its port taken back while it is open: ended by the proxy, with no word from the browser to the chat's server.
    expect((await browse("browser.navigate", { url: `http://localhost:${port}/?taken` })).ok?.title).toMatch(/^The first chat's page/);
    expect((await browse("browser.evaluate", { code: LIVE("ticking") })).ok?.value).toBe("reload");
    expect(await count(1)).toBe(1);
    host.forwards([port], door(), KEY, []);
    expect((await browse("browser.evaluate", { code: "return window.ended;" })).ok?.value).toBe("closed 1006 false");
    const takenBack = await count(0);
    host.forwards([port, live], door(), KEY, []);
    // Two tabs with a socket each, one of them carrying frames, and the browser's main process killed under them.
    for (const [session, path] of [["one", "quiet"], ["two", "ticking"]] as const) {
      expect((await browse("browser.navigate", { url: `http://localhost:${port}/?${session}` }, ROOT, session)).ok?.title).toMatch(/^The first chat's page/);
      expect((await browse("browser.evaluate", { code: LIVE(path) }, ROOT, session)).ok?.value).toBe("reload");
    }
    expect(await count(2)).toBe(2);
    const main = readdirSync("/proc").filter((pid) => /^\d+$/.test(pid)).filter((pid) => {
      try {
        const args = readFileSync(`/proc/${pid}/cmdline`, "utf8");
        return args.includes(launch.profile) && !args.includes("--type=");
      } catch {
        return false;
      }
    });
    expect(main).toHaveLength(1);
    process.kill(Number(main[0]), "SIGKILL");
    const killed = await count(0);
    console.log(`live sockets open at the chat's server after the page closed its own: ${closed}; after its tab was closed: ${tabClosed}; after its port was taken back: ${takenBack}; after the browser was killed with two open: ${killed}`);
    expect([closed, tabClosed, takenBack, killed]).toEqual([0, 0, 0, 0]);
    await expect.poll(async () => (await browse("browser.navigate", { url: `http://localhost:${port}/?after-live` })).ok?.title, { timeout: 30_000 }).toMatch(/^The first chat's page/);
    expect(hits).toBe(0);
  });

  it("ends the socket idle longest at a chat's bound, as a connection the page hears closed, and never one that carries frames", { timeout: 180_000 }, async () => {
    manager.forwards(KEY, [[port, ROOT], [live, ROOT], [HELD, ROOT]]);
    host.forwards([port, live], door(), KEY, []);
    const held: Socket[] = [];
    try {
      // A socket that says nothing more, opened first, and one that is sent a frame ten times a second.
      for (const [session, path] of [["quiet", "quiet"], ["ticking", "ticking"]] as const) {
        expect((await browse("browser.navigate", { url: `http://localhost:${port}/?${session}` }, ROOT, session)).ok?.title).toMatch(/^The first chat's page/);
        expect((await browse("browser.evaluate", { code: LIVE(path) }, ROOT, session)).ok?.value).toBe("reload");
      }
      expect(await open(2, LIVE_COUNT)).toBe(2);
      // The chat's other connections, up to all it takes from the browser: nothing is ended while there is room.
      for (let n = 0; n < MAX_INBOUND - 2; n += 1) held.push(await hold());
      const ended = (session: string) => browse("browser.evaluate", { code: `return Promise.race([window.ended, new Promise((done) => setTimeout(() => done("open"), 300))]);` }, ROOT, session).then((answer) => answer.ok?.value);
      expect([await ended("quiet"), await ended("ticking"), held.filter((socket) => socket.destroyed).length]).toEqual(["open", "open", 0]);
      // One more: the quiet socket has carried no byte for longest, and is the one that goes. Its page hears it
      // close as a connection lost, which a development server's page opens again.
      held.push(await hold());
      expect((await browse("browser.evaluate", { code: "return window.ended;" }, ROOT, "quiet")).ok?.value).toBe("closed 1006 false");
      expect(await open(1, LIVE_COUNT)).toBe(1);
      // Twenty more, and the quiet page's socket opened again: each ends a connection that says nothing, the one held
      // longest, and the socket that carries frames stays through them all.
      for (let n = 0; n < 20; n += 1) held.push(await hold());
      expect((await browse("browser.evaluate", { code: LIVE("quiet") }, ROOT, "quiet")).ok?.value).toBe("reload");
      await expect.poll(() => held.filter((socket) => socket.destroyed).length, { timeout: 5_000 }).toBe(21);
      expect(held.slice(0, 21).every((socket) => socket.destroyed)).toBe(true);
      const before = Number((await browse("browser.evaluate", { code: "return window.heard;" }, ROOT, "ticking")).ok?.value);
      await expect.poll(async () => Number((await browse("browser.evaluate", { code: "return window.heard;" }, ROOT, "ticking")).ok?.value), { timeout: 5_000 }).toBeGreaterThan(before);
      expect([await ended("ticking"), await open(2, LIVE_COUNT)]).toEqual(["open", 2]);
    } finally {
      for (const socket of held) socket.destroy();
    }
    expect(hits).toBe(0);
  });

  it("closes a page's live socket when its chat's root is torn down, within a second, and opens it again once the chat's server runs again", async () => {
    manager.forwards(KEY, [[port, ROOT], [live, ROOT]]);
    host.forwards([port, live], door(), KEY, []);
    expect((await browse("browser.navigate", { url: `http://localhost:${port}/?torn` })).ok?.title).toMatch(/^The first chat's page/);
    expect((await browse("browser.evaluate", { code: LIVE("ticking") })).ok?.value).toBe("reload");
    await browse("browser.evaluate", { code: "window.endedAt = window.ended.then((how) => [how, Date.now()]); return 1;" });
    const began = Date.now();
    await manager.teardown(ROOT);
    const torn = Date.now();
    const [how, at] = (await browse("browser.evaluate", { code: "return window.endedAt;" })).ok?.value as [string, number];
    console.log(`a page's live socket closed ${at - began} ms after its chat's root's teardown began, which took ${torn - began} ms`);
    expect([how, at - torn < 1_000]).toEqual(["closed 1006 false", true]);
    // Nothing of the chat's is reached meanwhile: the socket does not open, and nothing on this computer is tried in its place.
    expect((await browse("browser.evaluate", { code: LIVE("quiet") })).ok?.value).toBe("no socket");
    // Set up again by the chat's next command, with its server started again: the page's socket opens as before.
    expect(await command(ROOT, "start", background("node live.js"))).toMatchObject({ ok: { session_id: expect.any(String) } });
    await listens(ROOT, HELD);
    expect((await browse("browser.evaluate", { code: LIVE("quiet") })).ok?.value).toBe("reload");
    expect(hits).toBe(0);
  });
});
