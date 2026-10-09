// The way into a chat's sandbox from outside it (spec, Section 5): the guest under QEMU and KVM,
// booted by the VM manager, asked through its inbound port whether a root's own server listens, and
// its roots' servers reached through the manager's door, as the agent's browser's proxy reaches them.
// Behind SUROGATE_VM_TESTS=1 (npm run build first).

import { mkdirSync, mkdtempSync, readdirSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { connect, createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Duplex } from "node:stream";

import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { MAX_INBOUND } from "../../src/guest/protocol.js";
import { DOOR } from "../../src/vm/inbound.js";
import { type Guest, VmManager, type VmOptions } from "../../src/vm/manager.js";
import { agentDisk, background, folderOf, IMAGE, KVM, median, needsKvm, OTHER, ROOT, signal, USER } from "./guest-support.js";

beforeAll(needsKvm);

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("a chat's own servers, asked of the guest and reached through the manager's door", { timeout: 120_000 }, () => {
  const KEY = "5e".repeat(32);
  let dir: string;
  let options: VmOptions;
  let manager: VmManager;
  const op = (root: string, kind: string, args: Record<string, unknown>) =>
    manager.perform({ id: `${kind}-${Math.random()}`, root, folder: folderOf(join(dir, root === ROOT ? "a" : "b")), kind, args }, signal());
  const run = (root: string, command: string) => op(root, "run", { command, workdir: null, timeout: 30 });
  const said = async (root: string, command: string) => String(((await run(root, command)) as { ok?: { output?: string } }).ok?.output ?? "");
  // Waits, with a bound, until *root* listens on *port*.
  const listens = async (root: string, port: number) => {
    for (const end = Date.now() + 20_000; !(await manager.listening(root, port)); await new Promise((done) => setTimeout(done, 100))) {
      if (Date.now() > end) throw new Error(`nothing listened on ${port} in time`);
    }
  };
  // A server of *root*'s that answers every request with *says*, on *port* of *host*, once it listens.
  const serve = async (root: string, port: number, says: string, host = "127.0.0.1") => {
    const script = `require("node:http").createServer((req, res) => res.end(${JSON.stringify(says)} + " " + req.url)).listen(${port}, ${JSON.stringify(host)})`;
    expect(await op(root, "start", background(`node -e '${script}'`))).toMatchObject({ ok: { session_id: expect.any(String) } });
    await listens(root, port);
  };

  // This computer's own service, on both of its loopback's families, on one port: a dial that strays to either is heard.
  const ownService = async () => {
    const heard = { hits: 0, port: 0, close: async () => {} };
    for (;;) {
      const [six, four] = [0, 1].map(() => createServer((socket) => {
        heard.hits += 1;
        socket.on("error", () => {});
        socket.end("this computer's own");
      })) as [Server, Server];
      await new Promise<void>((done) => six.listen(0, "::1", done));
      heard.port = (six.address() as { port: number }).port;
      heard.close = async () => void (await Promise.all([six, four].map((server) => new Promise<void>((done) => server.close(() => done())))));
      // The port IPv6 gave may be taken on IPv4: another is tried.
      if (await new Promise<boolean>((done) => four.once("error", () => done(false)).listen(heard.port, "127.0.0.1", () => done(true)))) return heard;
      await new Promise<void>((done) => six.close(() => done()));
    }
  };
  // A knock at the manager's door, as the browser's proxy knocks: the door's answer line, and for one it
  // carries, the body of the server's answer to a request for "/".
  const get = (knock: number | string) => new Promise<string>((resolve) => {
    const socket = connect({ path: join(options.run, DOOR) });
    let said = "";
    let asked = false;
    socket.on("error", (error: NodeJS.ErrnoException) => resolve(String(error.code)));
    socket.on("data", (chunk: Buffer) => {
      said += chunk.toString();
      if (asked || !said.startsWith("200\n")) return;
      asked = true;
      socket.write("GET / HTTP/1.0\r\n\r\n");
    });
    socket.on("close", () => resolve(said.startsWith("200\n") ? `200 ${said.slice(said.indexOf("\r\n\r\n") + 4)}` : said));
    socket.write(`${KEY} ${knock}\n`);
  });
  // A connection held open through the door, which says nothing: resolved once the door has answered.
  const hold = (port: number) => new Promise<{ socket: Socket; said: () => string }>((resolve) => {
    const socket = connect({ path: join(options.run, DOOR) });
    let said = "";
    socket.on("error", () => {});
    socket.on("data", (chunk: Buffer) => {
      said += chunk.toString();
      resolve({ socket, said: () => said });
    });
    socket.on("close", () => resolve({ socket, said: () => said }));
    socket.write(`${KEY} ${port}\n`);
  });

  beforeAll(async () => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-inbound-")));
    for (const name of ["a", "b"]) mkdirSync(join(dir, name));
    options = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"), user: USER, kvm: KVM,
    };
    manager = new VmManager(options);
    expect(await run(ROOT, "true")).toMatchObject({ ok: { returncode: 0 } });
    expect(await run(OTHER, "true")).toMatchObject({ ok: { returncode: 0 } });
    // An emulated guest takes minutes to boot.
  }, KVM === undefined ? 60_000 : 600_000);

  afterAll(async () => {
    await manager?.stop();
    rmSync(options.run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("says that a chat listens on the port of a server it started on its own loopback, and on no other", async () => {
    await serve(ROOT, 8000, "first chat");
    expect([await manager.listening(ROOT, 8000), await manager.listening(ROOT, 8009)]).toEqual([true, false]);
  });

  it("carries no connection to the root's own proxies for its commands, though they listen in the chat", async () => {
    const guest = await (manager as unknown as { guest: Promise<Guest> }).guest;
    for (const port of [3128, 1080]) {
      expect(await said(ROOT, `(exec 3<>/dev/tcp/127.0.0.1/${port}) 2>/dev/null && echo listens`)).toBe("listens\n");
      const reached = await guest.reach(ROOT, port);
      if (typeof reached !== "string") reached.destroy();
      expect([port, typeof reached === "string" ? reached : "carried", await manager.listening(ROOT, port)]).toEqual([port, "unreachable", false]);
    }
  });

  it("asks the chat named and no other, though another chat listens on the same port", async () => {
    await serve(OTHER, 8005, "second chat");
    expect([await manager.listening(OTHER, 8005), await manager.listening(ROOT, 8005)]).toEqual([true, false]);
    expect([await manager.listening(ROOT, 8000), await manager.listening(OTHER, 8000)]).toEqual([true, false]);
  });

  it("hears a server that listens only on the root's IPv6 loopback, as one that listens on localhost may", async () => {
    await serve(ROOT, 8001, "on ::1", "::1");
    expect(await manager.listening(ROOT, 8001)).toBe(true);
    // What a server gets when it listens on "localhost" in the guest, said in the run's output: why the runner dials both.
    const bound = await said(ROOT, `node -e 'const s = require("node:net").createServer().listen(0, "localhost", () => { console.log(s.address().address); s.close(); })'`);
    console.log(`a server that listens on "localhost" in the guest is bound to ${bound.trim()}`);
  });

  it("never asks this computer's own loopback: a service of its own on the port, on either family, hears nothing", async () => {
    const own = await ownService();
    try {
      expect([await manager.listening(ROOT, own.port), await manager.listening(OTHER, own.port)]).toEqual([false, false]);
      // Nothing to poll for what must not come: a moment for a dial that strayed to arrive.
      await new Promise((done) => setTimeout(done, 300));
      expect(own.hits).toBe(0);
    } finally {
      await own.close();
    }
  });

  it("asks by one connection that says nothing, and lets it go, though the server's answer is still on its way", async () => {
    // Two servers of the chat's: one that only counts its connections, one that answers each without end, as a
    // download does; and a third that says what the two hold open and have heard.
    const script = `const net = require("node:net");
const seen = { quiet: { open: 0, total: 0, bytes: 0 }, endless: { open: 0, total: 0, bytes: 0 } };
const count = (to, socket) => { to.open += 1; to.total += 1; socket.on("error", () => {}); socket.on("data", (chunk) => { to.bytes += chunk.length; }); socket.on("close", () => { to.open -= 1; }); };
net.createServer((socket) => count(seen.quiet, socket)).listen(8006, "127.0.0.1");
net.createServer((socket) => { count(seen.endless, socket); const part = Buffer.alloc(1 << 16, 97); const more = () => { while (!socket.destroyed && socket.write(part)); }; socket.on("drain", more); more(); }).listen(8007, "127.0.0.1");
require("node:http").createServer((req, res) => res.end(JSON.stringify(seen))).listen(8008, "127.0.0.1");`;
    writeFileSync(join(dir, "a", "counting.js"), script);
    expect(await op(ROOT, "start", background("node counting.js"))).toMatchObject({ ok: { session_id: expect.any(String) } });
    for (const port of [8006, 8007, 8008]) await listens(ROOT, port);
    const before = await seen();
    for (let n = 0; n < 6; n += 1) expect([await manager.listening(ROOT, 8006), await manager.listening(ROOT, 8007)]).toEqual([true, true]);
    // Bounded: three seconds for the servers' counts to come back to none.
    let after = await seen();
    for (const began = Date.now(); after.quiet.open + after.endless.open !== 0 && Date.now() - began < 3_000; after = await seen()) {
      await new Promise((done) => setTimeout(done, 100));
    }
    console.log(`open at the chat's servers after six questions each: ${after.quiet.open} (quiet), ${after.endless.open} (answering without end)`);
    expect(after).toEqual({
      quiet: { open: 0, total: before.quiet.total + 6, bytes: 0 }, endless: { open: 0, total: before.endless.total + 6, bytes: 0 },
    });
  });

  it("keeps the two ways apart: a command of the chat still reaches nothing of another chat's, and the question takes no time to speak of; measured", async () => {
    // From inside the first chat, port 8000 is its own loopback's, so its own server answers; the second chat has none of the first's.
    expect(await run(ROOT, "curl -sS --noproxy '*' --max-time 5 http://127.0.0.1:8000/x")).toMatchObject({ ok: { output: "first chat /x" } });
    expect(await run(OTHER, "curl -sS --noproxy '*' --max-time 5 http://127.0.0.1:8001/ 2>&1; echo rc=$?")).toMatchObject({
      ok: { output: expect.stringMatching(/rc=7\n$/) },
    });
    const times: number[] = [];
    for (let n = 0; n < 30; n += 1) {
      const begun = performance.now();
      expect(await manager.listening(ROOT, 8000)).toBe(true);
      times.push(performance.now() - begun);
    }
    const begun = performance.now();
    expect(new Set(await Promise.all(Array.from({ length: 12 }, () => manager.listening(ROOT, 8000))))).toEqual(new Set([true]));
    console.log([
      `whether a chat listens, median ${median(times).toFixed(1)} ms of 30 (${Math.min(...times).toFixed(1)}–${Math.max(...times).toFixed(1)})`,
      `12 at once in ${(performance.now() - begun).toFixed(0)} ms`,
    ].join("; "));
  });

  it("carries a browser's connection to the chat its port is forwarded to and no other, to the family its knock names first, and never to this computer's own loopback", async () => {
    const own = await ownService();
    // One port of the first chat's with a server on each family, which say which they are.
    await serve(ROOT, 8002, "on 127.0.0.1");
    expect(await op(ROOT, "start", background(`node -e 'require("node:http").createServer((req, res) => res.end("on ::1 " + req.url)).listen(8002, "::1")'`))).toMatchObject({ ok: { session_id: expect.any(String) } });
    await vi.waitFor(async () => expect(await said(ROOT, "curl -sS --noproxy '*' --max-time 5 'http://[::1]:8002/'")).toBe("on ::1 /"), { timeout: 20_000, interval: 200 });
    try {
      manager.forwards(KEY, [[8000, ROOT], [8001, ROOT], [8002, ROOT], [8005, OTHER], [own.port, ROOT]]);
      expect([await get(8000), await get(8005)]).toEqual(["200 first chat /", "200 second chat /"]);
      // A server on ::1 alone is reached whichever family is tried first; one on each is the one the knock names.
      expect([await get(8001), await get("8001 6"), await get(8002), await get("8002 6")]).toEqual(["200 on ::1 /", "200 on ::1 /", "200 on 127.0.0.1 /", "200 on ::1 /"]);
      // A port not forwarded is refused at the door, whoever listens there; this computer's own service on a
      // forwarded port is never the one that answers.
      expect([await get(8006), await get(own.port), await get(`${own.port} 6`)]).toEqual(["403 refused\n", "502 ECONNREFUSED\n", "502 ECONNREFUSED\n"]);
      // Each port leads to the chat the app named last, and to that chat alone.
      manager.forwards(KEY, [[8000, OTHER], [8005, ROOT]]);
      expect([await get(8000), await get(8005), await get(8001)]).toEqual(["502 ECONNREFUSED\n", "502 ECONNREFUSED\n", "403 refused\n"]);
      await new Promise((done) => setTimeout(done, 300));
      expect(own.hits).toBe(0);
    } finally {
      manager.forwards(KEY, []);
      await own.close();
    }
  });

  type Seen = Record<"quiet" | "endless", { open: number; total: number; bytes: number }>;
  const seen = async () => JSON.parse(await said(ROOT, "curl -sS --noproxy '*' --max-time 5 http://127.0.0.1:8008/")) as Seen;
  // The chat's servers' count of what they hold open, once it is *open*, or after three seconds.
  const settled = async (which: "quiet" | "endless", open: number) => {
    let now = await seen();
    for (const began = Date.now(); now[which].open !== open && Date.now() - began < 3_000; now = await seen()) await new Promise((done) => setTimeout(done, 100));
    return now[which];
  };

  it("lets go of what a browser abandons partway: the chat's server and its root keep no connection of it", async () => {
    manager.forwards(KEY, [[8007, ROOT]]);
    const left: number[] = [];
    for (let n = 0; n < 6; n += 1) {
      const { socket, said: answer } = await hold(8007);
      expect(answer().startsWith("200\n")).toBe(true);
      // Read at full speed for a moment for three of them, and never for the other three; then gone, as a tab closed mid-download.
      if (n % 2 === 1) socket.pause();
      await new Promise((done) => setTimeout(done, 400));
      socket.destroy();
      left.push((await settled("endless", 0)).open);
    }
    console.log(`open at the chat's server after each of six downloads was let go partway: ${left.join(", ")}`);
    expect(left).toEqual([0, 0, 0, 0, 0, 0]);
    manager.forwards(KEY, []);
  });

  it("ends the connection idle longest to admit a new one at a root's bound, makes no connection to the chat's server past it, and leaves the chat's commands and another chat their own", { timeout: 180_000 }, async () => {
    manager.forwards(KEY, [[8006, ROOT], [8000, ROOT], [8005, OTHER]]);
    const before = (await seen()).quiet;
    expect(before.open).toBe(0);
    const held: Array<{ socket: Socket; said: () => string }> = [];
    const more: Array<{ socket: Socket; said: () => string }> = [];
    try {
      for (let n = 0; n < MAX_INBOUND; n += 1) held.push(await hold(8006));
      expect([held.every(({ said: answer }) => answer() === "200\n"), (await settled("quiet", MAX_INBOUND)).open]).toEqual([true, MAX_INBOUND]);
      // The chat's own commands keep their network: the judge still answers them, as before any of this.
      const judged = "curl -sS -o /dev/null -w '%{http_code}' --max-time 10 http://not-a-package-host.example/ 2>/dev/null; true";
      expect(await said(ROOT, judged)).toMatch(/^403/);
      // And another chat's port still opens.
      expect(await get(8005)).toBe("200 second chat /");
      // Whether the chat listens elsewhere cannot be asked now: the sandbox says so, and not that nothing listens.
      expect([await manager.listening(ROOT, 8000), await manager.listening(OTHER, 8005)]).toEqual(["busy", true]);
      // Forty more, one after another and then at once: each is carried, and each of the forty held longest is ended for one.
      for (let n = 0; n < 20; n += 1) more.push(await hold(8006));
      more.push(...await Promise.all(Array.from({ length: 20 }, () => hold(8006))));
      expect(more.map(({ said: answer }) => answer())).toEqual(more.map(() => "200\n"));
      await vi.waitFor(() => expect(held.filter(({ socket }) => socket.destroyed)).toHaveLength(40), { timeout: 5_000 });
      expect(held.slice(40).some(({ socket }) => socket.destroyed)).toBe(false);
      // The chat's server took one connection for each the door carried, and holds no more than the bound.
      const after = await settled("quiet", MAX_INBOUND);
      console.log(`at a root's bound of ${MAX_INBOUND}: 40 more carried, ${after.open} open at the chat's server, ${after.total - before.total} made there in all`);
      expect([after.open, after.total - before.total, after.bytes]).toEqual([MAX_INBOUND, MAX_INBOUND + 40, 0]);
    } finally {
      for (const { socket } of [...held, ...more]) socket.destroy();
    }
    expect((await settled("quiet", 0)).open).toBe(0);
    manager.forwards(KEY, []);
  });

  it("closes every connection held into a chat when its root is torn down, within a second: read, unread, or saying nothing", async () => {
    // The guest that runs, for connections held as the browser's would be: the manager itself only asks and lets go.
    const guest = await (manager as unknown as { guest: Promise<Guest> }).guest;
    const held: Duplex[] = [];
    for (const [port, read] of [[8006, false], [8007, true], [8007, false]] as const) {
      for (let n = 0; n < 4; n += 1) {
        const reached = await guest.reach(ROOT, port);
        if (typeof reached === "string") throw new Error(reached);
        reached.on("error", () => {});
        if (read) reached.resume();
        held.push(reached);
      }
    }
    await new Promise((done) => setTimeout(done, 300));
    expect(held.filter((reached) => reached.closed)).toHaveLength(0);
    await manager.teardown(ROOT);
    const torn = performance.now();
    await expect.poll(() => held.filter((reached) => reached.closed).length, { timeout: 5_000, interval: 20 }).toBe(held.length);
    const took = performance.now() - torn;
    console.log(`${held.length} connections held into a chat, all closed ${took.toFixed(0)} ms after its root was torn down`);
    expect(took).toBeLessThan(1_000);
    // Set up again by the chat's next command, the root takes connections as before, and has none of its old servers.
    expect(await run(ROOT, "true")).toMatchObject({ ok: { returncode: 0 } });
    expect(await manager.listening(ROOT, 8006)).toBe(false);
  });

  it("answers that nothing listens in a chat whose root is torn down, and boots no guest for the question", async () => {
    await manager.teardown(ROOT);
    expect([await manager.listening(ROOT, 8000), await manager.listening(OTHER, 8005)]).toEqual([false, true]);
    // A manager whose guest does not run starts none to answer.
    const idle = { ...options, run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), sessions: join(dir, "idle.img"), console: join(dir, "idle.log") };
    const other = new VmManager(idle);
    try {
      expect(await other.listening(ROOT, 8000)).toBe(false);
      expect(readdirSync(idle.run)).toEqual([]);
    } finally {
      await other.stop();
      rmSync(idle.run, { recursive: true, force: true });
    }
  });
});
