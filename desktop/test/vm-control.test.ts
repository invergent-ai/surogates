import { getEventListeners } from "node:events";
import { mkdtempSync, rmSync } from "node:fs";
import { connect, createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { CANCELLED, SANDBOX_STOPPED } from "../src/guest/command.js";
import { Control, type ControlRoots } from "../src/guest/control.js";
import type { HostUser, Share } from "../src/guest/protocol.js";
import type { Outcome } from "../src/link/protocol.js";
import { ControlLink } from "../src/vm/control.js";

const USER: HostUser = { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" };
const R1: Share = { kind: "virtiofs", tag: "r1" };
const never = new Promise<never>(() => {});

let dir: string;
let path: string;
let server: Server | null;
let sockets: Socket[];
let setups: unknown[];

// The guest's own Control at the far end of a socket, as QEMU's chardev puts it on Linux, with roots that run nothing.
const roots: ControlRoots = {
  uid: () => 10_000,
  setup: async (...args) => {
    setups.push(args);
  },
  teardown: async () => {},
  perform: (_root, kind, args, signal) => new Promise<Outcome>((resolve) => {
    if (kind === "run") signal.addEventListener("abort", () => resolve(CANCELLED), { once: true });
    else resolve({ ok: args });
  }),
};

async function agent(speak = true): Promise<void> {
  server = createServer((socket) => {
    sockets.push(socket);
    if (!speak) return;
    let first = true;
    const control = new Control((message) => {
      const line = `${JSON.stringify(message)}\n`;
      if (!first) return void socket.write(line);
      // Hello, cut in two, as the guest's port can deliver a line.
      first = false;
      socket.write(line.slice(0, 7));
      setTimeout(() => socket.write(line.slice(7)), 20);
    }, roots);
    createInterface({ input: socket }).on("line", (line) => control.receive(line));
    // A line that is not JSON, which the host skips.
    socket.write("not json\n");
    control.hello();
  });
  await new Promise<void>((resolve) => server?.listen(path, resolve));
}

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "vm-control-"));
  path = join(dir, "control.sock");
  server = null;
  sockets = [];
  setups = [];
});

afterEach(async () => {
  for (const socket of sockets) socket.destroy();
  await new Promise((resolve) => (server ? server.close(resolve) : resolve(undefined)));
  rmSync(dir, { recursive: true, force: true });
});

describe("the host's side of the control port", () => {
  it("answers hello with the host's user, then asks by id", async () => {
    await agent();
    const link = await ControlLink.open(connect(path), USER, performance.now() + 5_000, never);
    expect(await link.request({ type: "uid", root: "root-1" })).toEqual({ type: "done", id: 1, uid: 10_000 });
    expect(await link.request({ type: "ping" })).toEqual({ type: "pong", id: 2 });
    expect(await link.request({ type: "setup", root: "root-1", folder: "/home/ana/p", share: R1 })).toEqual({ type: "done", id: 3 });
    expect(setups).toEqual([["root-1", "/home/ana/p", R1, USER]]);
    expect(await link.op("root-1", "which", { name: "sh" }, new AbortController().signal)).toEqual({ ok: { name: "sh" } });
    link.close();
  });

  it("answers a cancel at once, and an operation the link loses as stopped by the sandbox", async () => {
    await agent();
    const link = await ControlLink.open(connect(path), USER, performance.now() + 5_000, never);
    const cancel = new AbortController();
    const cancelled = link.op("root-1", "run", { command: "sleep 30" }, cancel.signal);
    cancel.abort();
    expect(await cancelled).toEqual(CANCELLED);
    const running = link.op("root-1", "run", { command: "sleep 30" }, new AbortController().signal);
    const asked = link.request({ type: "ping" });
    for (const socket of sockets) socket.destroy();
    expect(await running).toEqual(SANDBOX_STOPPED);
    expect(await asked).toBeNull();
    await link.closed;
    expect(await link.request({ type: "ping" })).toBeNull();
    expect(await link.op("root-1", "which", {}, new AbortController().signal)).toEqual(SANDBOX_STOPPED);
  });

  it("leaves no listener on a signal its operations shared once they settle", async () => {
    await agent();
    const link = await ControlLink.open(connect(path), USER, performance.now() + 5_000, never);
    const shared = new AbortController().signal;
    for (const name of ["sh", "ls"]) expect(await link.op("root-1", "which", { name }, shared)).toEqual({ ok: { name } });
    expect(getEventListeners(shared, "abort")).toHaveLength(0);
    link.close();
  });

  it("gives up on an answer after its own deadline", async () => {
    await agent();
    const link = await ControlLink.open(connect(path), USER, performance.now() + 5_000, never);
    expect(await link.request({ type: "op", root: "root-1", kind: "run", args: {} }, 100)).toBeNull();
    link.close();
  });

  it("fails when no hello comes by the deadline, or the VM goes first", async () => {
    await agent(false);
    await expect(ControlLink.open(connect(path), USER, performance.now() + 300, never)).rejects.toThrow("The guest's agent did not say hello");
    await expect(ControlLink.open(connect(path), USER, performance.now() + 5_000, Promise.resolve())).rejects.toThrow("The VM exited");
  });
});

describe("the host's side of the control port, closed from the host", () => {
  it("answers what waits once the host closes it", async () => {
    await agent();
    const link = await ControlLink.open(connect(path), USER, performance.now() + 5_000, never);
    const running = link.op("root-1", "run", { command: "sleep 30" }, new AbortController().signal);
    link.close();
    expect(await running).toEqual(SANDBOX_STOPPED);
    await link.closed;
  });

  it("says the VM exited when its channel closes before hello", async () => {
    server = createServer((socket) => {
      sockets.push(socket);
      socket.end();
    });
    await new Promise<void>((resolve) => server?.listen(path, resolve));
    await expect(ControlLink.open(connect(path), USER, performance.now() + 5_000, never)).rejects.toThrow("The VM exited");
  });
});

describe("a guest whose line passes 8 MiB", () => {
  it("is lost: before hello as a VM that exited, after it with what waits answered and the link closed", async () => {
    const unended = Buffer.alloc(9 * 1024 ** 2, "x");
    let connections = 0;
    server = createServer((socket) => {
      sockets.push(socket);
      // The host closes on what it has not read: this end is reset.
      socket.on("error", () => {});
      connections += 1;
      if (connections === 1) return void socket.write(unended);
      socket.write('{"type":"hello","id":0}\n');
      // Asked, it answers with a line that never ends.
      socket.once("data", () => socket.write(unended));
    });
    await new Promise<void>((resolve) => server?.listen(path, resolve));
    await expect(ControlLink.open(connect(path), USER, performance.now() + 5_000, never)).rejects.toThrow("The VM exited");
    const link = await ControlLink.open(connect(path), USER, performance.now() + 5_000, never);
    const asked = link.request({ type: "ping" });
    expect(await Promise.race([asked, new Promise((resolve) => setTimeout(() => resolve("unanswered"), 3_000))])).toBeNull();
    await link.closed;
  });
});

describe("a root the guest lost", () => {
  it("is told, unasked", async () => {
    // Only a root that was set up can be lost: here, after the host's first request.
    server = createServer((socket) => {
      sockets.push(socket);
      socket.write('{"type":"hello","id":0}\n');
      createInterface({ input: socket }).on("line", () => socket.write('{"type":"lost","root":"root-1"}\n'));
    });
    await new Promise<void>((resolve) => server?.listen(path, resolve));
    const link = await ControlLink.open(connect(path), USER, performance.now() + 5_000, never);
    const told = new Promise<string>((resolve) => link.onLost(resolve));
    void link.request({ type: "setup", root: "root-1", folder: "/home/ana/p", share: R1 }, 100);
    expect(await told).toBe("root-1");
    link.close();
  });
});
