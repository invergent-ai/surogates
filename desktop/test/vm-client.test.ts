import { spawnSync } from "node:child_process";
import { chmodSync, mkdirSync, mkdtempSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { CANCELLED, SANDBOX_STOPPED } from "../src/guest/command.js";
import type { ProtectedKey } from "../src/guest/protocol.js";
import { forkManager, type FromManager, type ManagerProcess, VmClient, vmOptions } from "../src/vm/client.js";
import { unavailable, type VmOperation, type VmOptions } from "../src/vm/manager.js";

let dir: string;
let path: string | undefined;
let spawned: ManagerProcess[];
let clients: VmClient[];

// A sleep of a length only the stand-in QEMU uses, and how many of them run.
const SLEEP = "31.357";
const running = () => Number(spawnSync("pgrep", ["-fc", `^sleep ${SLEEP}$`], { encoding: "utf8" }).stdout.trim() || 0);

function client(): VmClient {
  const options: VmOptions = {
    kernel: "/i/vmlinuz", rootfs: "/i/rootfs.img", agentDisk: "/a/agent.img", sessions: join(dir, "sessions.img"), run: join(dir, "run"),
    console: join(dir, "console.log"), user: { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" },
  };
  const made = new VmClient({
    vm: options,
    spawn: () => {
      const manager = forkManager();
      spawned.push(manager);
      return manager;
    },
  });
  clients.push(made);
  return made;
}

const operation = (): VmOperation => ({ id: `op-${Math.random()}`, root: "root-1", folder: { path: dir, ...statSync(dir) }, kind: "run", args: {} });
const signal = () => new AbortController().signal;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "vm-client-"));
  mkdirSync(join(dir, "bin"));
  // A QEMU that never says hello while dir/slow is there, and otherwise cannot start.
  const qemu = join(dir, "bin", "qemu-system-x86_64");
  writeFileSync(qemu, `#!/bin/sh\nif [ -e '${dir}/slow' ]; then exec sleep ${SLEEP}; fi\necho 'no KVM here' >&2\nexit 1\n`);
  chmodSync(qemu, 0o755);
  path = process.env.PATH;
  process.env.PATH = `${join(dir, "bin")}:${path}`;
  spawned = [];
  clients = [];
});

afterEach(async () => {
  process.env.PATH = path;
  for (const made of clients) await made.stop();
  for (const manager of spawned) manager.kill();
  rmSync(dir, { recursive: true, force: true });
});

// *answer*, or "no answer" once *ms* pass.
const within = <T>(answer: Promise<T>, ms: number) =>
  Promise.race([answer, new Promise<"no answer">((resolve) => setTimeout(() => resolve("no answer"), ms))]);

describe("a manager that does not answer a teardown", () => {
  it("is killed once the teardown's bound passes, and its guest goes with it", async () => {
    let killed = false;
    const exits: Array<() => void> = [];
    // It takes its start and says it runs, then answers nothing more.
    const wedged: ManagerProcess = {
      send: () => {},
      onMessage: (listener) => void setTimeout(() => listener({ type: "ready" }), 10),
      onExit: (listener) => void exits.push(listener),
      kill: () => {
        killed = true;
        for (const exit of exits.splice(0)) exit();
      },
    };
    const vm = new VmClient({
      vm: { kernel: "/k", rootfs: "/r", agentDisk: "/a", sessions: join(dir, "s.img"), run: join(dir, "run"), console: join(dir, "c.log"), user: { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" } },
      spawn: () => wedged,
      teardownMs: 300,
    });
    clients.push(vm);
    const running = vm.perform(operation(), signal());
    await new Promise((resolve) => setTimeout(resolve, 50));
    const begun = performance.now();
    expect(await within(vm.teardown("root-1"), 3_000)).toBeUndefined();
    expect(performance.now() - begun).toBeLessThan(1_000);
    expect(killed).toBe(true);
    expect(await running).toEqual(SANDBOX_STOPPED);
  });
});

describe("a manager that cannot be started", () => {
  it("answers the operation, and starts another for the next", async () => {
    const vm = client();
    // fork runs the Node at process.execPath: one that is not there fails to spawn, and never exits.
    const node = process.execPath;
    process.execPath = join(dir, "no-node");
    let first: Promise<unknown>;
    try {
      first = vm.perform(operation(), signal());
    } finally {
      process.execPath = node;
    }
    expect(await within(first, 3_000)).toEqual({ error: { type: "unavailable", message: "This computer's sandbox did not start: its manager exited" } });
    expect(await within(vm.perform(operation(), signal()), 5_000)).toEqual({
      error: { type: "unavailable", message: "This computer's sandbox did not start: QEMU exited: no KVM here" },
    });
  });

  it("answers the operation when the spawn throws", async () => {
    const vm = new VmClient({
      vm: { kernel: "/k", rootfs: "/r", agentDisk: "/a", sessions: join(dir, "s.img"), run: join(dir, "run"), console: join(dir, "c.log"), user: { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" } },
      spawn: () => {
        throw new Error("spawn ENOMEM");
      },
    });
    clients.push(vm);
    expect(await vm.perform(operation(), signal())).toEqual({ error: { type: "unavailable", message: "This computer's sandbox did not start: spawn ENOMEM" } });
  });
});

describe("the VM manager's process", { timeout: 20_000 }, () => {
  it("is started at the first operation, and answers it", async () => {
    const vm = client();
    expect(spawned).toHaveLength(0);
    expect(await vm.perform(operation(), signal())).toEqual({
      error: { type: "unavailable", message: "This computer's sandbox did not start: QEMU exited: no KVM here" },
    });
    expect(await vm.perform(operation(), signal())).toMatchObject({ error: { type: "unavailable" } });
    expect(spawned).toHaveLength(1);
  });

  it("answers a cancel at once", async () => {
    writeFileSync(join(dir, "slow"), "");
    const vm = client();
    const cancel = new AbortController();
    const waiting = vm.perform(operation(), cancel.signal);
    await new Promise((resolve) => setTimeout(resolve, 300));
    cancel.abort();
    expect(await waiting).toEqual(CANCELLED);
  });

  it("answers what a manager that died was doing as stopped by the sandbox, takes its QEMU with it, and starts another for the next", async () => {
    writeFileSync(join(dir, "slow"), "");
    const vm = client();
    const waiting = vm.perform(operation(), signal());
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(running()).toBe(1);
    spawned[0]?.kill();
    expect(await waiting).toEqual(SANDBOX_STOPPED);
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(running()).toBe(0);
    rmSync(join(dir, "slow"));
    expect(await vm.perform(operation(), signal())).toMatchObject({ error: { type: "unavailable" } });
    expect(spawned).toHaveLength(2);
  });

  it("stops its manager, which stops the guest it boots at once, answers what it ran, and answers nothing more", async () => {
    writeFileSync(join(dir, "slow"), "");
    const vm = client();
    const waiting = vm.perform(operation(), signal());
    await new Promise((resolve) => setTimeout(resolve, 500));
    const begun = performance.now();
    await vm.stop();
    // Its own stop, well inside the kill that follows one that does not.
    expect(performance.now() - begun).toBeLessThan(2_000);
    expect(await waiting).toEqual({ error: { type: "unavailable", message: "This computer's sandbox is stopping" } });
    expect(running()).toBe(0);
    expect(await vm.perform(operation(), signal())).toEqual({ error: { type: "unavailable", message: "This computer's sandbox is stopping" } });
  });

  it("ends a manager that says it stopped, as Electron's utility process can lose what it sent just before its own exit", async () => {
    // A manager that answers each operation it runs with "is stopping" at the stop, says it stopped, and waits to be ended.
    const heard: Array<(message: FromManager) => void> = [];
    const exits: Array<() => void> = [];
    const tell = (message: FromManager) => heard.forEach((listener) => listener(message));
    const running: string[] = [];
    let ended = false;
    const manager: ManagerProcess = {
      send: (message) => {
        if (message.type === "start") tell({ type: "ready" });
        if (message.type === "op") running.push(message.operation.id);
        if (message.type !== "stop") return;
        for (const id of running) tell({ type: "result", id, outcome: unavailable("is stopping") });
        tell({ type: "stopped" });
      },
      onMessage: (listener) => void heard.push(listener),
      onExit: (listener) => void exits.push(listener),
      kill: () => {
        if (!ended) exits.forEach((listener) => listener());
        ended = true;
      },
    };
    const vm = new VmClient({ vm: { kernel: "", rootfs: "", agentDisk: "", sessions: "", run: "", console: "", user: { uid: 1, gid: 1, name: "ana", home: "/home/ana" } }, spawn: () => manager });
    clients.push(vm);
    const waiting = vm.perform(operation(), signal());
    const begun = performance.now();
    await vm.stop();
    expect(performance.now() - begun).toBeLessThan(1_000);
    expect(ended).toBe(true);
    expect(await waiting).toEqual(unavailable("is stopping"));
  });

  it("tells each listener of a root's processes, and of every root it heard of as gone when its manager goes", async () => {
    const heard: Array<(message: FromManager) => void> = [];
    const exits: Array<() => void> = [];
    const tell = (message: FromManager) => heard.forEach((listener) => listener(message));
    const manager: ManagerProcess = {
      send: (message) => {
        if (message.type === "start") tell({ type: "ready" });
      },
      onMessage: (listener) => void heard.push(listener),
      onExit: (listener) => void exits.push(listener),
      kill: () => exits.forEach((listener) => listener()),
    };
    const vm = new VmClient({ vm: { kernel: "", rootfs: "", agentDisk: "", sessions: "", run: "", console: "", user: { uid: 1, gid: 1, name: "ana", home: "/home/ana" } }, spawn: () => manager });
    clients.push(vm);
    const told: unknown[] = [];
    const other: unknown[] = [];
    const stop = vm.onProcesses((root, change) => told.push([root, change]));
    vm.onProcesses((root) => other.push(root));
    const waiting = vm.perform(operation(), signal());
    const handle = { id: "proc_000000000001", command: "sleep 9", cwd: dir, task_id: null, started_at: 1 };
    tell({ type: "processes", root: "root-1", change: { handles: [handle], live: 1 } });
    tell({ type: "processes", root: "root-2", change: { handles: [], live: 0 } });
    tell({ type: "processes", root: "root-2", change: { gone: true } });
    expect(told).toEqual([["root-1", { handles: [handle], live: 1 }], ["root-2", { handles: [], live: 0 }], ["root-2", { gone: true }]]);
    manager.kill();
    expect(await waiting).toEqual(SANDBOX_STOPPED);
    // root-2 was told gone already; root-1 goes with the manager.
    expect(told.at(-1)).toEqual(["root-1", { gone: true }]);
    expect(told).toHaveLength(4);
    stop();
    tell({ type: "processes", root: "root-3", change: { handles: [], live: 0 } });
    expect(told).toHaveLength(4);
    expect(other).toEqual(["root-1", "root-2", "root-2", "root-1", "root-3"]);
  });

  it("answers that the sandbox did not start when its manager cannot", async () => {
    const vm = new VmClient({
      vm: { kernel: "", rootfs: "", agentDisk: "", sessions: "", run: "", console: "", user: { uid: 1, gid: 1, name: "ana", home: "/home/ana" } },
      spawn: () => forkManager(join(dir, "no-such-manager.js")),
    });
    clients.push(vm);
    expect(await vm.perform(operation(), signal())).toEqual({
      error: { type: "unavailable", message: "This computer's sandbox did not start: its manager exited" },
    });
  });

  it("tells its manager a root's protected keys, and starts none to do it", async () => {
    const sent: unknown[] = [];
    let started = 0;
    const manager: ManagerProcess = { send: (message) => void sent.push(message), onMessage: () => {}, onExit: () => {}, kill: () => {} };
    const vm = new VmClient({ vm: { kernel: "", rootfs: "", agentDisk: "", sessions: "", run: "", console: "", user: { uid: 1, gid: 1, name: "ana", home: "/home/ana" } }, spawn: () => {
      started += 1;
      return manager;
    } });
    const keys: ProtectedKey[] = [["/f/.git/config", 41, "ro"]];
    vm.protect("root-1", keys);
    expect(started).toBe(0);
    void vm.perform(operation(), signal());
    vm.protect("root-1", keys);
    expect(sent.slice(1)).toEqual([expect.objectContaining({ type: "op" }), { type: "protect", root: "root-1", keys }]);
  });

  it("tears a root down through its manager, and starts none to do it", async () => {
    const vm = client();
    await vm.teardown("root-1");
    expect(spawned).toHaveLength(0);
    expect(await vm.perform(operation(), signal())).toMatchObject({ error: { type: "unavailable" } });
    await vm.teardown("root-1");
    expect(spawned).toHaveLength(1);
  });
});

describe("the VM's files", () => {
  const ana = { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" };

  it("are each app's own: a runtime folder for each data folder, short enough for a socket", () => {
    const env = { XDG_RUNTIME_DIR: "/run/user/1000" };
    const installed = vmOptions("/home/ana/.local/share/surogate", ana, env);
    expect(installed.run).toMatch(/^\/run\/user\/1000\/surogate\/vm-[0-9a-f]{8}$/);
    expect(vmOptions("/home/ana/.local/share/surogate", ana, env).run).toBe(installed.run);
    expect(vmOptions("/tmp/sd-x1/surogate", ana, env).run).not.toBe(installed.run);
    expect(Buffer.byteLength(join(installed.run, "vfs-8.sock"))).toBeLessThan(108);
    expect(installed.sessions).toBe("/home/ana/.local/share/surogate/vm/sessions.img");
    // Without XDG_RUNTIME_DIR: the user's own folder logind makes, never /tmp.
    expect(vmOptions("/d", { ...ana, uid: 1234 }, {}).run).toMatch(/^\/run\/user\/1234\/surogate\/vm-[0-9a-f]{8}$/);
  });
});
