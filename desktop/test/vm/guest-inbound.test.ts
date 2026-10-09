// The way into a chat's sandbox from outside it (spec, Section 5): the guest under QEMU and KVM,
// booted by the VM manager, asked through its inbound port whether a root's own server listens.
// Behind SUROGATE_VM_TESTS=1 (npm run build first).

import { mkdirSync, mkdtempSync, readdirSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { VmManager, type VmOptions } from "../../src/vm/manager.js";
import { agentDisk, background, folderOf, IMAGE, KVM, median, needsKvm, OTHER, ROOT, signal, USER } from "./guest-support.js";

beforeAll(needsKvm);

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("whether a chat's own server listens, asked of the guest", { timeout: 120_000 }, () => {
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
    let hits = 0;
    // This computer's own service, on both of its loopback's families, on one port: a dial that strays to either is heard.
    let own: Server[] = [];
    let port = 0;
    for (;;) {
      const [six, four] = own = [0, 1].map(() => createServer((socket) => {
        hits += 1;
        socket.on("error", () => {});
        socket.end("this computer's own");
      })) as [Server, Server];
      await new Promise<void>((done) => six.listen(0, "::1", done));
      port = (six.address() as { port: number }).port;
      // The port IPv6 gave may be taken on IPv4: another is tried.
      if (await new Promise<boolean>((done) => four.once("error", () => done(false)).listen(port, "127.0.0.1", () => done(true)))) break;
      await new Promise<void>((done) => six.close(() => done()));
    }
    try {
      expect([await manager.listening(ROOT, port), await manager.listening(OTHER, port)]).toEqual([false, false]);
      // Nothing to poll for what must not come: a moment for a dial that strayed to arrive.
      await new Promise((done) => setTimeout(done, 300));
      expect(hits).toBe(0);
    } finally {
      await Promise.all(own.map((server) => new Promise<void>((done) => server.close(() => done()))));
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
    type Seen = Record<"quiet" | "endless", { open: number; total: number; bytes: number }>;
    const seen = async () => JSON.parse(await said(ROOT, "curl -sS --noproxy '*' --max-time 5 http://127.0.0.1:8008/")) as Seen;
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
