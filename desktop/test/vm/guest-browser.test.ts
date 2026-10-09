// A chat's own server opened in the user's own browser (spec, Section 5): the guest under QEMU and
// KVM, a server one of its roots started, and Chrome or Edge from its .deb, headed on xvfb's display,
// through the browser host's proxy and the VM manager's door. Behind SUROGATE_VM_TESTS=1 and
// SUROGATE_BROWSER_TESTS=1, apart from the user's session:
//   npm run build && SUROGATE_VM_TESTS=1 sh test/isolated.sh npx vitest run test/vm/guest-browser.test.ts

import { mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { Launch } from "../../src/browser/client.js";
import { BrowserHost } from "../../src/browser/host.js";
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
  let dir: string;
  let options: VmOptions;
  let manager: VmManager;
  let host: BrowserHost;
  let launch: Launch;
  // This computer's own service, on the port the chats' servers take in their sandboxes, on both of its loopback's families.
  let own: Server[];
  let port: number;
  let hits: number;
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
  // How many answers the first chat's endless server has open, once it is *open*, or after three seconds.
  const open = async (wanted: number) => {
    const count = async () => Number(((await command(ROOT, "run", { command: `curl -sS --noproxy '*' --max-time 5 http://127.0.0.1:${COUNT}/`, workdir: null, timeout: 30 })) as { ok?: { output?: string } }).ok?.output);
    let now = await count();
    for (const began = Date.now(); now !== wanted && Date.now() - began < 3_000; now = await count()) await new Promise((done) => setTimeout(done, 100));
    return now;
  };
  // The page reads an answer of the endless server's, and goes on reading.
  const READING = `fetch("http://localhost:${ENDLESS}/").then(async (answer) => { for (const reader = answer.body.getReader(); !(await reader.read()).done;); }).catch(() => {}); return 1;`;

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
    for (;;) {
      const [six, four] = own = [0, 1].map(() => createServer((socket) => {
        hits += 1;
        socket.destroy();
      })) as [Server, Server];
      await new Promise<void>((done) => six.listen(0, "::1", done));
      port = (six.address() as { port: number }).port;
      // The port IPv6 gave may be taken on IPv4: another is tried.
      if (await new Promise<boolean>((done) => four.once("error", () => done(false)).listen(port, "127.0.0.1", () => done(true)))) break;
      await new Promise<void>((done) => six.close(() => done()));
    }
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
    host.forwards([port], door(), KEY);
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
    host.forwards([], door(), KEY);
    // Each at an address the browser has not kept a copy of: a page it has in its cache it shows without asking anyone.
    expect(await browse("browser.navigate", { url: `http://localhost:${port}/?taken-back` })).toEqual({
      error: { type: "browser", message: `The agent's browser opens a server a chat started only once its user has allowed that port for the chat (port ${port})` },
    });
    // The proxy alone would let it through: the door does not.
    manager.forwards(KEY, []);
    host.forwards([port], door(), KEY);
    expect(await browse("browser.navigate", { url: `http://localhost:${port}/?forgotten` })).toEqual({
      error: { type: "browser", message: `The sandbox has not been told that the agent's browser may open port ${port} yet. Open it again in a moment.` },
    });
    // Forwarded again, to a port of the chat's that nothing listens on.
    const quiet = port === 65_535 ? port - 1 : port + 1;
    manager.forwards(KEY, [[quiet, ROOT]]);
    host.forwards([quiet], door(), KEY);
    expect(await browse("browser.navigate", { url: `http://localhost:${quiet}/` })).toEqual({
      error: { type: "browser", message: `Nothing answers on port ${quiet} of the chat's servers now: its server is not running in the chat's sandbox` },
    });
    expect(hits).toBe(0);
  });

  it("lets go, at the chat's server too, of what the browser lets go: a tab closed while it reads an answer, and a browser killed with answers under way", async () => {
    manager.forwards(KEY, [[port, ROOT], [ENDLESS, ROOT]]);
    host.forwards([port, ENDLESS], door(), KEY);
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
});
