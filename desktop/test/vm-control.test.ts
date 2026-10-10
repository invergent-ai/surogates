import { getEventListeners } from "node:events";
import { mkdtempSync, rmSync } from "node:fs";
import { connect, createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { CANCELLED, SANDBOX_STOPPED } from "../src/guest/command.js";
import { Control, type ControlPlaces, type ControlRoots } from "../src/guest/control.js";
import type { HostUser, Share } from "../src/guest/protocol.js";
import type { Outcome } from "../src/link/protocol.js";
import { ControlLink } from "../src/vm/control.js";

const USER: HostUser = { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" };
const R1: Share = { kind: "virtiofs", tag: "r1" };
const KEY = "0123456789abcdef";
const THREAD = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const ID = "a".repeat(40);
const NOT_AN_ANSWER = {
  error: { type: "history", code: "not_an_answer", message: "This computer's sandbox answered what is not a history's answer, so it was not used" },
};
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
// And places whose history answers what its request's arguments say, or waits for its cancel.
const places: ControlPlaces = {
  mount: async () => {},
  unmount: async () => {},
  history: (_key, { args }, signal) => new Promise<Outcome>((resolve) => {
    if (args.wait) signal.addEventListener("abort", () => resolve(CANCELLED), { once: true });
    else resolve(args.say as Outcome);
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
    }, roots, undefined, places);
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
    expect(await link.request({ type: "setup", root: "root-1", folder: "/home/ana/p", share: R1, ended: [] })).toEqual({ type: "done", id: 3 });
    expect(setups).toEqual([["root-1", "/home/ana/p", R1, USER, []]]);
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

  it("asks a folder's history, and gives what the agent answered only as it was checked", async () => {
    await agent();
    const link = await ControlLink.open(connect(path), USER, performance.now() + 5_000, never);
    const history = (action: string, say: unknown) => link.history(KEY, THREAD, "u1", action, { say }, new AbortController().signal);
    expect(await history("snapshot", { ok: { hash: ID, planted: true } })).toEqual({ ok: { hash: ID } });
    expect(await history("snapshot", { ok: { hash: "--upload-pack=/x" } })).toEqual(NOT_AN_ANSWER);
    // Checked as the answer of the action that was asked, whatever other action it would be an answer of.
    expect(await history("open", { ok: { hash: ID } })).toEqual(NOT_AN_ANSWER);
    expect(await history("open", { error: { type: "history", code: "no_whole_copy", message: "its words", more: 1 } }))
      .toEqual({ error: { type: "history", code: "no_whole_copy", message: "its words" } });
    for (const said of [null, 7, "ok", { error: null }, CANCELLED]) expect(await history("open", said)).toEqual(NOT_AN_ANSWER);
    // An open names what was set aside of the thread it was asked for, and of no other.
    const aside = (thread: string) => `00000001-20261010T030000Z-${thread}.copy`;
    expect(await history("open", { ok: { copy: "made", set_aside_folders: [aside(THREAD)] } })).toEqual({ ok: { copy: "made", set_aside_folders: [aside(THREAD)] } });
    expect(await history("open", { ok: { copy: "made", set_aside_folders: [aside(THREAD.replace("0b", "1c"))] } })).toEqual(NOT_AN_ANSWER);
    // A cancel is this computer's to answer, at once; and a link that closed, as a sandbox that stopped.
    const cancel = new AbortController();
    const cancelled = link.history(KEY, THREAD, "u1", "open", { wait: true }, cancel.signal);
    cancel.abort();
    expect(await cancelled).toEqual(CANCELLED);
    const running = link.history(KEY, THREAD, "u1", "open", { wait: true }, new AbortController().signal);
    for (const socket of sockets) socket.destroy();
    expect(await running).toEqual(SANDBOX_STOPPED);
    expect(await link.history(KEY, THREAD, "u1", "open", {}, new AbortController().signal)).toEqual(SANDBOX_STOPPED);
  });

  it("refuses an answer to a history that is no result at all", async () => {
    // An agent that does not know the request, or answers it as it would a place.
    server = createServer((socket) => {
      sockets.push(socket);
      socket.write('{"type":"hello","id":0}\n');
      const replies = ['{"type":"failed","id":1,"message":"The agent does not know the request history"}', '{"type":"done","id":2}', '{"type":"result","id":3}'];
      createInterface({ input: socket }).on("line", (line) => {
        if ((JSON.parse(line) as { type: string }).type === "history") socket.write(`${replies.shift()}\n`);
      });
    });
    await new Promise<void>((resolve) => server?.listen(path, resolve));
    const link = await ControlLink.open(connect(path), USER, performance.now() + 5_000, never);
    for (let asked = 0; asked < 3; asked += 1) {
      expect(await link.history(KEY, THREAD, "u1", "restore", {}, new AbortController().signal)).toEqual(NOT_AN_ANSWER);
    }
    link.close();
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

  it("is lost before a history's answer of that size is read as one: the request is answered as a sandbox that stopped", async () => {
    server = createServer((socket) => {
      sockets.push(socket);
      socket.on("error", () => {});
      socket.write('{"type":"hello","id":0}\n');
      // A result whose list never ends: each entry is a path an answer could hold.
      socket.once("data", () => {
        socket.write('{"type":"result","id":1,"outcome":{"ok":{"paths":[');
        socket.write(Buffer.from('"a.txt",'.repeat(9 * 1024 ** 2 / 8)));
      });
    });
    await new Promise<void>((resolve) => server?.listen(path, resolve));
    const link = await ControlLink.open(connect(path), USER, performance.now() + 5_000, never);
    const asked = link.history(KEY, THREAD, "u1", "changed", {}, new AbortController().signal);
    expect(await Promise.race([asked, new Promise((resolve) => setTimeout(() => resolve("unanswered"), 3_000))])).toEqual(SANDBOX_STOPPED);
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
    void link.request({ type: "setup", root: "root-1", folder: "/home/ana/p", share: R1, ended: [] }, 100);
    expect(await told).toBe("root-1");
    link.close();
  });
});
