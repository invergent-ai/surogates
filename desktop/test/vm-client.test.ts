import { spawnSync } from "node:child_process";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CANCELLED, SANDBOX_STOPPED } from "../src/guest/command.js";
import { FOLDER_UNAVAILABLE } from "../src/hosts/messages.js";
import type { Outcome } from "../src/link/protocol.js";
import {
  forkManager, type FromManager, type ManagerProcess, REPO_IMAGE, type ToManager, VmClient, type VmClientOptions, vmEnv, vmOptions,
} from "../src/vm/client.js";
import { type HistoryRequest, NOT_A_REQUEST } from "../src/vm/history.js";
import { type Boot, type Place, unavailable, type VmOperation, type VmOptions } from "../src/vm/manager.js";

let dir: string;
let path: string | undefined;
let spawned: ManagerProcess[];
let clients: VmClient[];

// A sleep of a length only the stand-in QEMU uses, and how many of them run.
const SLEEP = "31.357";
const running = () => Number(spawnSync("pgrep", ["-fc", `^sleep ${SLEEP}$`], { encoding: "utf8" }).stdout.trim() || 0);

// *ready*: what the client waits for before a boot, as the app's image. Its stand-in boots open a KVM
// device of their own, which this test makes, never this computer's.
function client(ready?: (signal: AbortSignal) => Promise<void>): VmClient {
  writeFileSync(join(dir, "kvm"), "");
  const options: VmOptions = {
    kernel: "/i/vmlinuz", rootfs: "/i/rootfs.img", agentDisk: "/a/agent.img", sessions: join(dir, "sessions.img"), run: join(dir, "run"),
    console: join(dir, "console.log"), user: { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" }, kvm: join(dir, "kvm"),
  };
  const made = new VmClient({
    vm: options,
    ready,
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
  writeFileSync(qemu, `#!/bin/sh\necho run >> '${dir}/qemu-runs'\nif [ -e '${dir}/slow' ]; then exec sleep ${SLEEP}; fi\necho 'no KVM here' >&2\nexit 1\n`);
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

  it("asks the first device whose root an ask names, and answers its manager; with none, or one that fails, it is denied", async () => {
    const heard: Array<(message: FromManager) => void> = [];
    const exits: Array<() => void> = [];
    const answered: unknown[] = [];
    const tell = (message: FromManager) => heard.forEach((listener) => listener(message));
    const manager: ManagerProcess = {
      send: (message) => {
        if (message.type === "start") tell({ type: "ready" });
        if (message.type === "answer") answered.push(message);
        if (message.type === "stop") exits.forEach((listener) => listener());
      },
      onMessage: (listener) => void heard.push(listener),
      onExit: (listener) => void exits.push(listener),
      kill: () => exits.forEach((listener) => listener()),
    };
    const vm = new VmClient({ vm: { kernel: "", rootfs: "", agentDisk: "", sessions: "", run: "", console: "", user: { uid: 1, gid: 1, name: "ana", home: "/home/ana" } }, spawn: () => manager });
    clients.push(vm);
    const asked: Array<[string, unknown]> = [];
    vm.onAsk((root) => (root === "root-2" ? Promise.resolve("allow_session") : null));
    const stop = vm.onAsk((root, request) => {
      asked.push([root, request]);
      if (root === "root-3") throw new Error("broken");
      if (root === "root-4") return Promise.reject(new Error("failed"));
      return root === "root-1" ? Promise.resolve("allow") : null;
    });
    void vm.perform(operation(), signal());
    const ask = (id: number, root: string) => tell({ type: "ask", id, root, host: "example.com", port: 443, privateNetwork: false });
    for (const [id, root] of [[1, "root-1"], [2, "root-2"], [3, "root-3"], [4, "root-4"], [5, "root-5"]] as const) ask(id, root);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(answered).toEqual([
      { type: "answer", id: 1, allow: true }, { type: "answer", id: 2, allow: true }, { type: "answer", id: 3, allow: false },
      { type: "answer", id: 4, allow: false }, { type: "answer", id: 5, allow: false },
    ]);
    expect(asked.map(([root]) => root)).toEqual(["root-1", "root-3", "root-4", "root-5"]);
    stop();
    ask(6, "root-1");
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(answered.at(-1)).toEqual({ type: "answer", id: 6, allow: false });
  });

  it("asks the next device past one whose asker throws", async () => {
    const heard: Array<(message: FromManager) => void> = [];
    const exits: Array<() => void> = [];
    const answered: unknown[] = [];
    const tell = (message: FromManager) => heard.forEach((listener) => listener(message));
    const manager: ManagerProcess = {
      send: (message) => {
        if (message.type === "start") tell({ type: "ready" });
        if (message.type === "answer") answered.push(message);
        if (message.type === "stop") exits.forEach((listener) => listener());
      },
      onMessage: (listener) => void heard.push(listener),
      onExit: (listener) => void exits.push(listener),
      kill: () => exits.forEach((listener) => listener()),
    };
    const vm = new VmClient({ vm: { kernel: "", rootfs: "", agentDisk: "", sessions: "", run: "", console: "", user: { uid: 1, gid: 1, name: "ana", home: "/home/ana" } }, spawn: () => manager });
    clients.push(vm);
    // A device whose journal cannot be read, then the one whose root it is.
    vm.onAsk(() => {
      throw new Error("database is locked");
    });
    vm.onAsk((root) => (root === "root-1" ? Promise.resolve("allow") : null));
    void vm.perform(operation(), signal());
    tell({ type: "ask", id: 1, root: "root-1", host: "example.com", port: 443, privateNetwork: false });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(answered).toEqual([{ type: "answer", id: 1, allow: true }]);
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

  it("asks its manager whether a root listens on a port, starts none to ask, and takes one that went or does not say as no", async () => {
    const heard: Array<(message: FromManager) => void> = [];
    const exits: Array<() => void> = [];
    const sent: ToManager[] = [];
    const tell = (message: FromManager) => heard.forEach((listener) => listener(message));
    let starts = 0;
    const vm = new VmClient({
      vm: { kernel: "", rootfs: "", agentDisk: "", sessions: "", run: "", console: "", user: { uid: 1, gid: 1, name: "ana", home: "/home/ana" } },
      spawn: () => {
        starts += 1;
        return {
          send: (message) => {
            sent.push(message);
            if (message.type === "start") tell({ type: "ready" });
            // Port 9000 is one its manager never answers about.
            if (message.type === "listening" && message.port !== 9000) tell({ type: "result", id: message.id, outcome: { ok: message.port === 3000 } });
            if (message.type === "stop") exits.forEach((listener) => listener());
          },
          onMessage: (listener) => void heard.push(listener),
          onExit: (listener) => void exits.push(listener),
          kill: () => exits.splice(0).forEach((listener) => listener()),
        };
      },
    });
    clients.push(vm);
    // No manager runs: nothing listens, and none is started to say so.
    expect(await vm.listening("root-1", 3000)).toBe(false);
    expect(starts).toBe(0);
    void vm.perform(operation(), signal());
    await vi.waitFor(() => expect(sent.some((message) => message.type === "start")).toBe(true));
    expect([await vm.listening("root-1", 3000), await vm.listening("root-1", 8000)]).toEqual([true, false]);
    expect(sent.filter((message) => message.type === "listening")).toEqual([
      { type: "listening", id: expect.any(String), root: "root-1", port: 3000 }, { type: "listening", id: expect.any(String), root: "root-1", port: 8000 },
    ]);
    // An answer that is not a yes is a no.
    const odd = vm.listening("root-1", 9000);
    const last = sent.at(-1) as { id: string };
    tell({ type: "result", id: last.id, outcome: { ok: "yes" } });
    expect(await odd).toBe(false);
    // But for a sandbox that could not be asked, which is neither.
    const full = vm.listening("root-1", 9000);
    tell({ type: "result", id: (sent.at(-1) as { id: string }).id, outcome: { ok: "busy" } });
    expect(await full).toBe("busy");
    // A manager that never says is given up on, past the bound it has itself for its guest's agent.
    vi.useFakeTimers();
    try {
      const silent = vm.listening("root-1", 9000);
      let answered: boolean | "busy" | undefined;
      void silent.then((answer) => {
        answered = answer;
      });
      await vi.advanceTimersByTimeAsync(10_000);
      expect(answered).toBeUndefined();
      await vi.advanceTimersByTimeAsync(5_000);
      expect(answered).toBe(false);
    } finally {
      vi.useRealTimers();
    }
    // The manager went: what waited for its answer listens on nothing.
    const waiting = vm.listening("root-1", 9000);
    exits.splice(0).forEach((listener) => listener());
    expect(await waiting).toBe(false);
    expect(await vm.listening("root-1", 3000)).toBe(false);
    expect(starts).toBe(1);
  });

  it("tells its manager the ports each device's browser may open, a manager started later too, and starts none to tell", async () => {
    const heard: Array<(message: FromManager) => void> = [];
    const exits: Array<() => void> = [];
    const sent: ToManager[] = [];
    const tell = (message: FromManager) => heard.forEach((listener) => listener(message));
    let starts = 0;
    const vm = new VmClient({
      vm: { kernel: "", rootfs: "", agentDisk: "", sessions: "", run: "/run/user/1000/surogate/vm-1", console: "", user: { uid: 1, gid: 1, name: "ana", home: "/home/ana" } },
      spawn: () => {
        starts += 1;
        return {
          send: (message) => {
            sent.push(message);
            if (message.type === "start") tell({ type: "ready" });
            if (message.type === "stop") exits.forEach((listener) => listener());
          },
          onMessage: (listener) => void heard.push(listener),
          onExit: (listener) => void exits.push(listener),
          kill: () => exits.splice(0).forEach((listener) => listener()),
        };
      },
    });
    clients.push(vm);
    // Where the browser's proxy knocks: in the VM's own runtime folder.
    expect(vm.door).toBe("/run/user/1000/surogate/vm-1/browser.sock");
    const KEY = "ab".repeat(32);
    const OTHER_KEY = "cd".repeat(32);
    vm.forwards(KEY, [[3000, "root-1"]]);
    vm.forwards(OTHER_KEY, [[5000, "root-9"]]);
    vm.forwards(OTHER_KEY, [[5000, "root-9"], [5001, "root-9"]]);
    expect(starts).toBe(0);
    void vm.perform(operation(), signal());
    await vi.waitFor(() => expect(sent.filter((message) => message.type === "forwards")).toHaveLength(2));
    // Before anything else it is asked: the latest of each device.
    expect(sent.slice(0, 3)).toEqual([
      expect.objectContaining({ type: "start" }),
      { type: "forwards", key: KEY, ports: [[3000, "root-1"]] }, { type: "forwards", key: OTHER_KEY, ports: [[5000, "root-9"], [5001, "root-9"]] },
    ]);
    // A device with none left is forgotten, by a manager started after too.
    vm.forwards(KEY, []);
    expect(sent.at(-1)).toEqual({ type: "forwards", key: KEY, ports: [] });
    exits.splice(0).forEach((listener) => listener());
    sent.length = 0;
    void vm.perform(operation(), signal());
    // Started again once its backoff has passed.
    await vi.waitFor(() => expect(sent.some((message) => message.type === "forwards")).toBe(true), { timeout: 10_000 });
    expect(sent.filter((message) => message.type === "forwards")).toEqual([{ type: "forwards", key: OTHER_KEY, ports: [[5000, "root-9"], [5001, "root-9"]] }]);
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

  it("boot the image the app delivered with its agent disk, SUROGATE_VM_IMAGE's in its place when set, and open SUROGATE_VM_KVM for KVM", () => {
    const delivered = { image: "/d/vm/images/abc", agentDisk: "/opt/surogate/current/resources/vm/agent.img" };
    const installed = vmOptions("/d", ana, {}, delivered);
    expect([installed.kernel, installed.rootfs, installed.agentDisk, installed.kvm]).toEqual([
      "/d/vm/images/abc/vmlinuz", "/d/vm/images/abc/rootfs.img", "/opt/surogate/current/resources/vm/agent.img", undefined,
    ]);
    expect(vmOptions("/d", ana, { SUROGATE_VM_IMAGE: "/mine" }, delivered).rootfs).toBe("/mine/rootfs.img");
    expect(vmOptions("/d", ana, {}).rootfs).toBe(join(REPO_IMAGE, "rootfs.img"));
    expect(vmOptions("/d", ana, { SUROGATE_VM_KVM: "/nowhere" }).kvm).toBe("/nowhere");
  });

  it("take SUROGATE_VM_IMAGE and SUROGATE_VM_KVM only in a development build: a packaged app boots only the image its manifest checks", () => {
    const delivered = { image: "/d/vm/images/abc" };
    const env = { SUROGATE_VM_IMAGE: "/mine", SUROGATE_VM_KVM: "/nowhere", XDG_RUNTIME_DIR: "/run/user/1000" };
    const packaged = vmOptions("/d", ana, vmEnv(env, true), delivered);
    expect([packaged.rootfs, packaged.kvm]).toEqual(["/d/vm/images/abc/rootfs.img", undefined]);
    expect(packaged.run).toMatch(/^\/run\/user\/1000\/surogate\//);
    const developed = vmOptions("/d", ana, vmEnv(env, false), delivered);
    expect([developed.rootfs, developed.kvm]).toEqual(["/mine/rootfs.img", "/nowhere"]);
  });
});

describe("the delivered image, and each boot", () => {
  it("waits for the VM to be able to boot, its image here, before it starts a manager, and starts one once it is", async () => {
    let here = () => {};
    const vm = client(() => new Promise<void>((resolve) => {
      here = resolve;
    }));
    const answered = vm.perform(operation(), signal());
    expect(await within(answered, 300)).toBe("no answer");
    expect(spawned).toHaveLength(0);
    here();
    // The stand-in QEMU cannot start: the manager ran, and said so.
    expect(await answered).toEqual(unavailable("did not start: QEMU exited: no KVM here"));
    expect(spawned).toHaveLength(1);
  });

  it("has its manager boot again at once at the user's Retry, the boot that did not start forgotten", async () => {
    const vm = client();
    const runs = () => readFileSync(join(dir, "qemu-runs"), "utf8").split("\n").filter(Boolean).length;
    const failed = unavailable("did not start: QEMU exited: no KVM here");
    expect(await vm.perform(operation(), signal())).toEqual(failed);
    // Inside the boot's backoff: answered at once, with no boot.
    expect(await vm.perform(operation(), signal())).toEqual(failed);
    expect(runs()).toBe(1);
    vm.retry();
    // At once, not after the backoff's second.
    const begun = performance.now();
    expect(await vm.perform(operation(), signal())).toEqual(failed);
    expect(runs()).toBe(2);
    expect(performance.now() - begun).toBeLessThan(500);
  });

  it("answers what waits with why the VM cannot boot, as an image that could not be downloaded, and starts no manager", async () => {
    const vm = client(async () => {
      throw new Error("could not be downloaded: there is not enough free disk space: it needs 3.5 GB, and 1.2 GB is free");
    });
    expect(await vm.perform(operation(), signal())).toEqual(
      unavailable("could not be downloaded: there is not enough free disk space: it needs 3.5 GB, and 1.2 GB is free"),
    );
    expect(spawned).toHaveLength(0);
  });

  it("answers a cancel, and the app's quit, at once while it waits for the VM to be able to boot", async () => {
    const waiting = (cancel: AbortSignal) => new Promise<void>((_resolve, reject) => cancel.addEventListener("abort", () => reject(cancel.reason), { once: true }));
    const vm = client(waiting);
    const cancel = new AbortController();
    const cancelled = vm.perform(operation(), cancel.signal);
    cancel.abort();
    expect(await cancelled).toEqual(CANCELLED);
    const quitting = vm.perform(operation(), signal());
    await vm.stop();
    expect(await quitting).toEqual(unavailable("is stopping"));
  });

  for (const [emulated, bound] of [[null, 10_000], ["no-kvm", 35_000]] as const) {
    it(`tells each boot, and gives a manager whose last guest ran ${emulated ? "emulated" : "with KVM"} ${bound / 1000} s to stop before it is killed`, async () => {
      vi.useFakeTimers();
      try {
        const killed = { count: 0 };
        const exits: Array<() => void> = [];
        let tell = (_message: FromManager) => {};
        const vm = new VmClient({
          vm: { kernel: "/k", rootfs: "/r", agentDisk: "/a", sessions: join(dir, "s.img"), run: join(dir, "run"), console: join(dir, "c.log"), user: { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" } },
          // It says it runs, and from then on lets its stops go unheard.
          spawn: () => ({
            send: (message) => void (message.type === "start" && setTimeout(() => tell({ type: "ready" }), 1)),
            onMessage: (listener) => {
              tell = listener;
            },
            onExit: (listener) => void exits.push(listener),
            kill: () => {
              killed.count += 1;
              for (const exit of exits.splice(0)) exit();
            },
          }),
        });
        const boots: Boot[] = [];
        vm.onBoot((boot) => boots.push(boot));
        void vm.perform(operation(), signal());
        await vi.advanceTimersByTimeAsync(10);
        tell({ type: "boot", boot: { failed: "QEMU exited" } });
        tell({ type: "boot", boot: { emulated } });
        expect(boots).toEqual([{ failed: "QEMU exited" }, { emulated }]);
        const stopped = vm.stop();
        await vi.advanceTimersByTimeAsync(bound - 100);
        expect(killed.count).toBe(0);
        await vi.advanceTimersByTimeAsync(200);
        expect(killed.count).toBe(1);
        await stopped;
      } finally {
        vi.useRealTimers();
      }
    });
  }
});

describe("a manager that runs on", () => {
  // A manager that says it runs, then answers what *answers* says, or nothing; killed once.
  const fake = (answers: { pongs: boolean; held: Array<() => void> }, sent: ToManager[], exits: Array<() => void>, killed: { count: number }): ManagerProcess => {
    const heard: Array<(message: FromManager) => void> = [];
    return {
      send: (message) => {
        sent.push(message);
        if (message.type === "start") setTimeout(() => heard.forEach((listener) => listener({ type: "ready" })), 5);
        if (message.type === "stop") for (const exit of exits.splice(0)) exit();
        if (message.type !== "ping") return;
        const pong = () => heard.forEach((listener) => listener({ type: "pong" }));
        if (answers.pongs) pong();
        else answers.held.push(pong);
      },
      onMessage: (listener) => void heard.push(listener),
      onExit: (listener) => void exits.push(listener),
      kill: () => {
        killed.count += 1;
        for (const exit of exits.splice(0)) exit();
      },
    };
  };
  const vmOf = (manager: () => ManagerProcess) => {
    const vm = new VmClient({
      vm: { kernel: "/k", rootfs: "/r", agentDisk: "/a", sessions: join(dir, "s.img"), run: join(dir, "run"), console: join(dir, "c.log"), user: { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" } },
      spawn: manager,
      pingMs: 100,
    });
    clients.push(vm);
    return vm;
  };

  it("starts no manager for a wake when none runs", () => {
    let starts = 0;
    const vm = vmOf(() => {
      starts += 1;
      return fake({ pongs: true, held: [] }, [], [], { count: 0 });
    });
    vm.resume();
    expect(starts).toBe(0);
  });

  it("is kept at the computer's wake, told it, and the pings it missed asleep not held against it", async () => {
    const answers = { pongs: false, held: [] as Array<() => void> };
    const sent: ToManager[] = [];
    const killed = { count: 0 };
    const vm = vmOf(() => fake(answers, sent, [], killed));
    const running = vm.perform(operation(), signal());
    await new Promise((resolve) => setTimeout(resolve, 250));
    vm.resume();
    await new Promise((resolve) => setTimeout(resolve, 150));
    answers.pongs = true;
    for (const pong of answers.held.splice(0)) pong();
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(killed.count).toBe(0);
    expect(sent.filter((message) => message.type === "resume")).toHaveLength(1);
    expect(await within(running, 100)).toBe("no answer");
  });

  it("is killed once it has answered no ping three times, what it ran answered as stopped by the sandbox", async () => {
    const killed = { count: 0 };
    const vm = vmOf(() => fake({ pongs: false, held: [] }, [], [], killed));
    const begun = performance.now();
    expect(await within(vm.perform(operation(), signal()), 2_000)).toEqual(SANDBOX_STOPPED);
    expect(performance.now() - begun).toBeLessThan(1_000);
    expect(killed.count).toBeGreaterThan(0);
  });

  it("is started again, once it went by itself, only once a second has passed, and two seconds after the next", async () => {
    const starts: number[] = [];
    const vm = vmOf(() => {
      starts.push(performance.now());
      const exits: Array<() => void> = [];
      const manager = fake({ pongs: true, held: [] }, [], exits, { count: 0 });
      // It runs, then crashes.
      setTimeout(() => exits.splice(0).forEach((exit) => exit()), 50);
      return manager;
    });
    for (let n = 0; n < 3; n += 1) expect(await vm.perform(operation(), signal())).toEqual(SANDBOX_STOPPED);
    const [first = 0, second = 0, third = 0] = starts;
    expect(second - first).toBeGreaterThan(1_000);
    expect(third - second).toBeGreaterThan(2_000);
  });

  it("answers a cancel at once while it backs off", async () => {
    const vm = vmOf(() => {
      const exits: Array<() => void> = [];
      const manager = fake({ pongs: true, held: [] }, [], exits, { count: 0 });
      setTimeout(() => exits.splice(0).forEach((exit) => exit()), 50);
      return manager;
    });
    expect(await vm.perform(operation(), signal())).toEqual(SANDBOX_STOPPED);
    const cancel = new AbortController();
    const waiting = vm.perform(operation(), cancel.signal);
    setTimeout(() => cancel.abort(), 100);
    const begun = performance.now();
    expect(await waiting).toEqual(CANCELLED);
    expect(performance.now() - begun).toBeLessThan(500);
  });

  it("answers an operation waiting out its backoff as soon as the app quits, not when its wait ends", async () => {
    const vm = vmOf(() => {
      const exits: Array<() => void> = [];
      const manager = fake({ pongs: true, held: [] }, [], exits, { count: 0 });
      setTimeout(() => exits.splice(0).forEach((exit) => exit()), 50);
      return manager;
    });
    expect(await vm.perform(operation(), signal())).toEqual(SANDBOX_STOPPED);
    // Its wait is a second; up to a minute after repeated crashes.
    const waiting = vm.perform(operation(), signal());
    setTimeout(() => void vm.stop(), 100);
    const begun = performance.now();
    expect(await waiting).toEqual(unavailable("is stopping"));
    expect(performance.now() - begun).toBeLessThan(500);
  });
});

describe("a folder's place and its history, through the manager's process", { timeout: 20_000 }, () => {
  const THREAD = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
  const KEY = "0123456789abcdef";
  const OTHER = "fedcba9876543210";
  const THIRD = "00112233445566ff";
  const place = (key = KEY): Place => ({ key, history: join(dir, "place", key), real: { path: dir, dev: 1, ino: 2, boot: "" } });
  const request = (action = "open", key = KEY): HistoryRequest => ({ place: place(key), thread: THREAD, user: "u-1", action, args: {} });
  const options: VmOptions = { kernel: "", rootfs: "", agentDisk: "", sessions: "", run: "", console: "", user: { uid: 1, gid: 1, name: "ana", home: "/home/ana" } };
  const NOT_AN_ANSWER = {
    error: { type: "history", code: "not_an_answer", message: "This computer's sandbox answered what is not a history's answer, so it was not used" },
  };
  const LET_GO = unavailable("let this folder's history go before this was answered");
  const pause = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
  type Asked = Extract<ToManager, { type: "place" | "history" | "unplace" }>;

  // A client whose managers say they run, answer their pings and keep what they are sent. *answers* says what each
  // message is answered a moment later, if anything; a test answers the rest itself, with the newest manager's *tell*.
  function recording(answers: (message: Asked) => Outcome | undefined = () => undefined, more: Partial<VmClientOptions> = {}) {
    const sent: ToManager[] = [];
    const managers: Array<{ killed: boolean; tell(message: unknown): void; exit(): void }> = [];
    const vm = new VmClient({
      vm: options,
      spawn: () => {
        const heard: Array<(message: FromManager) => void> = [];
        const exits: Array<() => void> = [];
        const made = {
          killed: false,
          tell: (message: unknown) => heard.forEach((listener) => listener(message as FromManager)),
          exit: () => exits.splice(0).forEach((listener) => listener()),
        };
        managers.push(made);
        return {
          send: (message) => {
            sent.push(message);
            if (message.type === "start") made.tell({ type: "ready" });
            if (message.type === "ping") made.tell({ type: "pong" });
            if (message.type === "stop") made.exit();
            if (message.type !== "place" && message.type !== "history" && message.type !== "unplace") return;
            const outcome = answers(message);
            if (outcome !== undefined) setTimeout(() => made.tell({ type: "result", id: message.id, outcome }), 5);
          },
          onMessage: (listener) => void heard.push(listener),
          onExit: (listener) => void exits.push(listener),
          kill: () => {
            made.killed = true;
            made.exit();
          },
        };
      },
      ...more,
    });
    clients.push(vm);
    const asked = () => sent.filter((message): message is Asked => message.type === "place" || message.type === "history" || message.type === "unplace");
    const said = () => asked().map((message) => (message.type === "history" ? `history ${message.request.action}` : message.type));
    const answer = (id: string, outcome: unknown) => managers.at(-1)?.tell({ type: "result", id, outcome });
    return { vm, sent, managers, asked, said, answer };
  }

  it("asks its manager for a place, a request to its history and the place's letting go, each under an id of its own, and answers as the manager does", async () => {
    const { vm, asked, said } = recording((message) => {
      if (message.type === "place") return { ok: null };
      if (message.type === "history") return { ok: { copy: message.request.action === "open" ? "made" : "kept" } };
      // The place the guest holds for that folder and history has gone; it holds none for any other.
      return { ok: message.place.key === KEY };
    });
    expect(await vm.place(place(), signal())).toBeNull();
    expect(await vm.history(request(), signal())).toEqual({ ok: { copy: "made" } });
    expect(await vm.history(request("changed"), signal())).toEqual({ ok: { copy: "kept" } });
    expect(await vm.unplace(place())).toBe(true);
    expect(await vm.unplace(place(OTHER))).toBe(false);
    expect(said()).toEqual(["place", "history open", "history changed", "unplace", "unplace"]);
    expect(asked()[0]).toEqual({ type: "place", id: expect.any(String), place: place() });
    expect(asked()[1]).toEqual({ type: "history", id: expect.any(String), request: request() });
    expect(asked()[3]).toEqual({ type: "unplace", id: expect.any(String), place: place() });
    expect(new Set(asked().map(({ id }) => id)).size).toBe(5);
  });

  it("answers what its manager answered, each refusal of a place and of a request with its own type, code and words", async () => {
    const answers: Outcome[] = [
      { ok: { copy: "moved", set_asides: ["a".repeat(40)] } },
      { ok: { history: "off", reason: "names" } },
      { error: { type: "history", code: "no_whole_copy", message: "fatal: the thread's copy is gone" } },
      NOT_AN_ANSWER,
      NOT_A_REQUEST,
      SANDBOX_STOPPED,
      FOLDER_UNAVAILABLE,
      unavailable("could not add this folder's history: it is not where the app keeps it"),
      unavailable("did not start: QEMU exited: no KVM here"),
      { error: { type: "unavailable", message: "This folder's history is held by a request before this one, which has not ended" } },
      LET_GO,
      { error: { type: "other", message: "spawn /usr/bin/python3 EAGAIN" } },
    ];
    // A place added is answered null; why one was not, as a request's refusal is.
    const placed: Outcome[] = [{ ok: null }, ...answers.filter((answer) => "error" in answer)];
    let next = 0;
    const { vm } = recording((message) => (message.type === "history" ? answers[next++] : message.type === "place" ? placed[next++ - answers.length] : undefined));
    for (const answer of answers) expect(await vm.history(request(), signal())).toEqual(answer);
    for (const answer of placed) expect(await vm.place(place(), signal())).toEqual("ok" in answer ? null : answer);
  });

  it("answers a cancelled place or request at once, and tells its manager, which stops it there and in the guest; the manager's own answer to it is then nobody's", async () => {
    const { vm, sent, asked, answer } = recording();
    const other = vm.history(request("changed", OTHER), signal());
    for (const ask of [(stop: AbortSignal) => vm.history(request("commit"), stop), (stop: AbortSignal) => vm.place(place(), stop)]) {
      const before = asked().length;
      const cancel = new AbortController();
      const waiting = ask(cancel.signal);
      await vi.waitFor(() => expect(asked()).toHaveLength(Math.max(before, 1) + 1));
      const begun = performance.now();
      cancel.abort();
      expect(await waiting).toEqual(CANCELLED);
      expect(performance.now() - begun).toBeLessThan(500);
      const { id } = asked().at(-1)!;
      expect(sent.at(-1)).toEqual({ type: "cancel", id });
      // Its manager answers it as cancelled, or with what it had done by then: once is enough.
      answer(id, CANCELLED);
      answer(id, { ok: { commit: null } });
    }
    expect(await within(other, 100)).toBe("no answer");
    // One cancelled before it was asked for asks its manager nothing.
    const cancelled = new AbortController();
    cancelled.abort();
    const asks = sent.length;
    expect(await vm.history(request(), cancelled.signal)).toEqual(CANCELLED);
    expect(await vm.place(place(), cancelled.signal)).toEqual(CANCELLED);
    expect(sent).toHaveLength(asks);
  });

  it("answers what its manager died with as stopped by the sandbox, takes no place for held once no manager runs, and starts a manager again for the next request", async () => {
    const { vm, asked, managers, answer } = recording();
    const placing = vm.place(place(), signal());
    const asking = vm.history(request("record", OTHER), signal());
    await vi.waitFor(() => expect(asked()).toHaveLength(2));
    const leaving = vm.unplace(place(THIRD));
    await vi.waitFor(() => expect(asked()).toHaveLength(3));
    managers[0]!.exit();
    expect(await placing).toEqual(SANDBOX_STOPPED);
    expect(await asking).toEqual(SANDBOX_STOPPED);
    // What the manager held went with it: nothing is held, and no manager is started to say so.
    expect(await leaving).toBe(false);
    expect(await vm.unplace(place())).toBe(false);
    expect(managers).toHaveLength(1);
    const again = vm.history(request(), signal());
    // Once its backoff has passed.
    await vi.waitFor(() => expect(asked()).toHaveLength(4), { timeout: 5_000 });
    expect(managers).toHaveLength(2);
    answer(asked()[3]!.id, { ok: { copy: "made" } });
    expect(await again).toEqual({ ok: { copy: "made" } });
  });

  it("starts no manager to let a place go, nor for a request that names no thread, user or action", async () => {
    const { vm, managers } = recording();
    expect(await vm.unplace(place())).toBe(false);
    for (const change of [{ thread: "t1" }, { user: "" }, { action: 7 }, { args: null }, { place: null }]) {
      expect(await vm.history({ ...request(), ...change } as unknown as HistoryRequest, signal())).toEqual(NOT_A_REQUEST);
    }
    expect(managers).toHaveLength(0);
  });

  it("sends its manager no place that is not whole, which the manager's guest would read as it is, and starts none for it", async () => {
    const { vm, managers } = recording();
    const broken = [
      { key: KEY, history: join(dir, "place") }, { key: KEY, history: join(dir, "place"), real: {} }, { key: KEY, real: place().real },
      { history: join(dir, "place"), real: place().real }, null,
    ] as unknown as Place[];
    const refused = { error: { type: "value", message: "This names no place of a folder's history" } };
    for (const part of broken) {
      if (part !== null) expect(await vm.history({ ...request(), place: part }, signal())).toEqual(refused);
      expect(await vm.place(part, signal())).toEqual(refused);
      expect(await vm.unplace(part)).toBe(false);
    }
    expect(managers).toHaveLength(0);
  });

  it("sends its manager what is asked of a place in the order it was asked: a letting-go after what was asked before it, though the VM cannot boot yet, and what is asked after it once the place has gone", async () => {
    let here = () => {};
    const able = new Promise<void>((resolve) => {
      here = resolve;
    });
    const { vm, asked, said, managers, answer } = recording(undefined, { ready: () => able });
    const placing = vm.place(place(), signal());
    const opening = vm.history(request("open"), signal());
    const leaving = vm.unplace(place());
    const next = vm.history(request("changed"), signal());
    const last = vm.unplace(place());
    // Another place's letting go waits for none of it: no manager runs, and nothing of that place is held.
    expect(await vm.unplace(place(OTHER))).toBe(false);
    expect(await within(Promise.race([leaving, last]), 100)).toBe("no answer");
    expect(managers).toHaveLength(0);
    here();
    await vi.waitFor(() => expect(said()).toEqual(["place", "history open", "unplace"]));
    // What was asked after the letting-go waits until the place has gone, as it would in the manager.
    await pause(100);
    expect(asked()).toHaveLength(3);
    answer(asked()[0]!.id, { ok: null });
    answer(asked()[1]!.id, { ok: { copy: "made" } });
    answer(asked()[2]!.id, { ok: true });
    expect([await placing, await opening, await leaving]).toEqual([null, { ok: { copy: "made" } }, true]);
    await vi.waitFor(() => expect(said()).toEqual(["place", "history open", "unplace", "history changed", "unplace"]));
    answer(asked()[3]!.id, { ok: { paths: [] } });
    answer(asked()[4]!.id, { ok: true });
    expect([await next, await last]).toEqual([{ ok: { paths: [] } }, true]);
  });

  it("asks its manager nothing for a request cancelled while it waits for its place's letting go, and answers it at once", async () => {
    const { vm, sent, asked, said, answer } = recording((message) => (message.type === "place" ? { ok: null } : undefined));
    expect(await vm.place(place(), signal())).toBeNull();
    const leaving = vm.unplace(place());
    const cancel = new AbortController();
    const waiting = vm.history(request(), cancel.signal);
    const other = vm.history(request("changed"), signal());
    await pause(50);
    expect(said()).toEqual(["place", "unplace"]);
    const begun = performance.now();
    cancel.abort();
    expect(await waiting).toEqual(CANCELLED);
    expect(performance.now() - begun).toBeLessThan(500);
    answer(asked()[1]!.id, { ok: true });
    expect(await leaving).toBe(true);
    await vi.waitFor(() => expect(said()).toEqual(["place", "unplace", "history changed"]));
    expect(sent.some((message) => message.type === "cancel")).toBe(false);
    answer(asked()[2]!.id, { ok: { paths: [] } });
    expect(await other).toEqual({ ok: { paths: [] } });
  });

  it("lets a place go within a bound of its own though what was asked of it before cannot reach the manager: once a place's time has passed that is answered as let go, and is never sent", async () => {
    // The VM cannot boot yet, and what says so heeds no cancel.
    const { vm, managers } = recording(undefined, { ready: () => new Promise(() => {}), vm: { ...options, setupMs: 400 } });
    const opening = vm.history(request(), signal());
    const placing = vm.place(place(), signal());
    const begun = performance.now();
    const leaving = vm.unplace(place());
    expect(await within(Promise.race([opening, placing, leaving]), 200)).toBe("no answer");
    expect([await opening, await placing, await leaving]).toEqual([LET_GO, LET_GO, false]);
    expect(performance.now() - begun).toBeGreaterThan(350);
    expect(performance.now() - begun).toBeLessThan(3_000);
    expect(managers).toHaveLength(0);
    // A request asked after the letting-go is not let go with them: it waits for the VM, as an operation does.
    const cancel = new AbortController();
    const later = vm.history(request("changed"), cancel.signal);
    expect(await within(later, 600)).toBe("no answer");
    cancel.abort();
    expect(await later).toEqual(CANCELLED);
  });

  it("ends nothing its manager has by then: only what has not reached it is let go", async () => {
    // The VM can boot for the first that asks, and never says so to the second.
    let asks = 0;
    const { vm, sent, said, asked, answer } = recording(undefined, {
      ready: () => ((asks += 1) === 1 ? pause(150) : new Promise(() => {})), vm: { ...options, setupMs: 400 },
    });
    const first = vm.history(request("open"), signal());
    const second = vm.history(request("changed"), signal());
    const leaving = vm.unplace(place());
    expect(await second).toEqual(LET_GO);
    // The first reached the manager meanwhile: its letting go is the manager's to wait for, and to end.
    await vi.waitFor(() => expect(said()).toEqual(["history open", "unplace"]));
    expect(sent.some((message) => message.type === "cancel")).toBe(false);
    answer(asked()[0]!.id, { ok: { copy: "made" } });
    answer(asked()[1]!.id, { ok: true });
    expect([await first, await leaving]).toEqual([{ ok: { copy: "made" } }, true]);
  });

  it("gives what was asked between two letting-gos of a place its turn once the first is answered, however long that took", async () => {
    const { vm, said, asked, answer } = recording((message) => (message.type === "place" ? { ok: null } : undefined), { vm: { ...options, setupMs: 300 } });
    expect(await vm.place(place(), signal())).toBeNull();
    const first = vm.unplace(place());
    const between = vm.history(request(), signal());
    const second = vm.unplace(place());
    // Past a place's time: the request has not had its turn yet, and is not let go for that.
    expect(await within(Promise.race([first, between, second]), 600)).toBe("no answer");
    expect(said()).toEqual(["place", "unplace"]);
    answer(asked()[1]!.id, { ok: true });
    await vi.waitFor(() => expect(said()).toEqual(["place", "unplace", "history open", "unplace"]));
    answer(asked()[2]!.id, { ok: { copy: "made" } });
    answer(asked()[3]!.id, { ok: false });
    expect([await first, await between, await second]).toEqual([true, { ok: { copy: "made" } }, false]);
  });

  it("takes from its manager no message that is none, no answer to what it did not ask, and no second answer: each request is answered once, with its own", async () => {
    const { vm, asked, managers, answer } = recording();
    const asking = vm.history(request(), signal());
    const placing = vm.place(place(OTHER), signal());
    await vi.waitFor(() => expect(asked()).toHaveLength(2));
    const leaving = vm.unplace(place(THIRD));
    await vi.waitFor(() => expect(asked()).toHaveLength(3));
    const [history, placed, unplaced] = asked() as [Asked, Asked, Asked];
    const { tell } = managers[0]!;
    // What a manager that ended as it wrote may leave of a message, and what one that is not ours may send.
    const none = [
      null, undefined, 7, "result", [], {}, { type: "result" }, { type: "result", outcome: { ok: true } }, { type: "result", id: 7, outcome: { ok: true } },
      { type: "result", id: [history.id], outcome: { ok: true } }, { type: "result", id: "history-1", outcome: { ok: true } },
      { type: "result", id: `${history.id} `, outcome: { ok: true } }, { type: "answer", id: history.id, outcome: { ok: true } },
    ];
    for (const message of none) expect(() => tell(message)).not.toThrow();
    expect(await within(Promise.race([asking, placing, leaving]), 100)).toBe("no answer");
    // An answer that is none is said to be none: a place is not taken for added by it, nor for let go.
    tell({ type: "result", id: history.id });
    expect(await asking).toEqual(NOT_AN_ANSWER);
    tell({ type: "result", id: placed.id, outcome: { ok: "added" } });
    expect(await placing).toEqual(NOT_AN_ANSWER);
    tell({ type: "result", id: unplaced.id, outcome: { ok: "yes" } });
    expect(await leaving).toBe(false);
    for (const outcome of [null, 7, [], {}, { error: null }, { error: "it failed" }, { error: { type: 7, message: "it failed" } }, { error: { type: "other" } }, { ok: 1, error: {} }]) {
      const waiting = vm.history(request(), signal());
      const sent = asked().length;
      await vi.waitFor(() => expect(asked()).toHaveLength(sent + 1));
      answer(asked().at(-1)!.id, outcome);
      expect(await waiting, JSON.stringify(outcome)).toEqual(NOT_AN_ANSWER);
    }
    // A second answer to any of them is nobody's: what is asked next has its own, once.
    const again = vm.history(request("changed"), signal());
    const sent = asked().length;
    await vi.waitFor(() => expect(asked()).toHaveLength(sent + 1));
    for (const { id } of [history, placed, unplaced]) answer(id, { ok: { paths: ["theirs"] } });
    expect(await within(again, 100)).toBe("no answer");
    answer(asked().at(-1)!.id, { ok: { paths: ["notes.txt"] } });
    answer(asked().at(-1)!.id, { ok: { paths: ["another"] } });
    expect(await again).toEqual({ ok: { paths: ["notes.txt"] } });
  });

  // The manager's own bounds with a boot before them (manager.ts, WAITS), in seconds: a guest with KVM, an emulated
  // one, and a manager whose options set its waits.
  const BOOT = 60 + 2 * 120 + 30;
  for (const [name, emulated, vm, seconds] of [
    ["runs with KVM", null, {}, { place: BOOT + 15 + 4 * 15 + 5, history: BOOT + 15 + 4 * 15 + 5 + 1_215, unplace: BOOT + 3 * 15 + 5 * 15 + 5 }],
    ["runs emulated", "no-kvm", {}, { place: BOOT + 90 + 4 * 90 + 5, history: BOOT + 90 + 4 * 90 + 5 + 7_290, unplace: BOOT + 3 * 90 + 5 * 90 + 5 }],
    ["has waits of its options'", null, { setupMs: 1_000, shareMs: 2_000, historyMs: 60_000 }, { place: BOOT + 1 + 4 * 2 + 5, history: BOOT + 1 + 4 * 2 + 5 + 60, unplace: BOOT + 3 * 1 + 5 * 2 + 5 }],
  ] as const) {
    it(`kills a manager that answers its pings and not what was asked of a place, once it is past its own bounds for a guest that ${name}, and answers that the sandbox stopped`, async () => {
      expect(seconds).toEqual({
        "runs with KVM": { place: 410, history: 1_625, unplace: 455 }, "runs emulated": { place: 785, history: 8_075, unplace: 1_055 },
        "has waits of its options'": { place: 344, history: 404, unplace: 348 },
      }[name]);
      vi.useFakeTimers();
      try {
        const asks = {
          place: (client: VmClient) => client.place(place(), signal()),
          history: (client: VmClient) => client.history(request(), signal()),
          unplace: (client: VmClient) => client.unplace(place()),
        };
        for (const kind of ["place", "history", "unplace"] as const) {
          const made = recording(undefined, { vm: { ...options, ...vm } });
          // A manager runs, as a place's letting go starts none.
          const running = made.vm.perform(operation(), signal());
          await vi.advanceTimersByTimeAsync(1);
          // One it answers just inside its bound is kept.
          const answered = asks[kind](made.vm);
          await vi.advanceTimersByTimeAsync(1);
          // How the guest runs is told once it has booted: what was asked before that has that guest's time.
          made.managers[0]!.tell({ type: "boot", boot: { emulated } });
          await vi.advanceTimersByTimeAsync(seconds[kind] * 1000 - 100);
          made.answer(made.asked()[0]!.id, kind === "unplace" ? { ok: true } : { ok: null });
          await answered;
          await vi.advanceTimersByTimeAsync(seconds[kind] * 1000);
          expect(made.managers[0]!.killed).toBe(false);
          let answer: unknown = "none";
          void asks[kind](made.vm).then((given) => {
            answer = given;
          });
          await vi.advanceTimersByTimeAsync(seconds[kind] * 1000 - 100);
          expect([made.managers[0]!.killed, answer]).toEqual([false, "none"]);
          await vi.advanceTimersByTimeAsync(200);
          expect(made.managers[0]!.killed).toBe(true);
          expect(answer).toEqual(kind === "unplace" ? false : SANDBOX_STOPPED);
          expect(await running).toEqual(SANDBOX_STOPPED);
        }
      } finally {
        vi.useRealTimers();
      }
    });
  }

  it("reaches the VM manager in its own process: a request boots the guest, and is answered with why it did not start", async () => {
    const vm = client();
    const failed = unavailable("did not start: QEMU exited: no KVM here");
    expect(await vm.history(request(), signal())).toEqual(failed);
    // Inside the boot's backoff: answered at once, with no boot.
    expect(await vm.place(place(), signal())).toEqual(failed);
    expect(await vm.unplace(place())).toBe(false);
    expect(spawned).toHaveLength(1);
    expect(readFileSync(join(dir, "qemu-runs"), "utf8").trim().split("\n")).toHaveLength(1);
  });

  it("answers a request cancelled while its manager's process boots the guest at once, one its manager died with as stopped by the sandbox, and at the app's quit that the sandbox is stopping", async () => {
    writeFileSync(join(dir, "slow"), "");
    const vm = client();
    const cancel = new AbortController();
    const cancelled = vm.history(request(), cancel.signal);
    const died = vm.history(request("changed", OTHER), signal());
    await pause(500);
    expect(running()).toBe(1);
    cancel.abort();
    expect(await cancelled).toEqual(CANCELLED);
    spawned[0]?.kill();
    expect(await died).toEqual(SANDBOX_STOPPED);
    expect(await vm.unplace(place())).toBe(false);
    await pause(200);
    expect(running()).toBe(0);
    // Another manager, once its backoff has passed, boots another guest.
    const placing = vm.place(place(), signal());
    const asking = vm.history(request("changed", OTHER), signal());
    await vi.waitFor(() => expect(running()).toBe(1), { timeout: 5_000 });
    expect(spawned).toHaveLength(2);
    const begun = performance.now();
    await vm.stop();
    expect(performance.now() - begun).toBeLessThan(2_000);
    expect([await placing, await asking]).toEqual([unavailable("is stopping"), unavailable("is stopping")]);
    expect(running()).toBe(0);
    expect(await vm.history(request(), signal())).toEqual(unavailable("is stopping"));
    expect(await vm.place(place(), signal())).toEqual(unavailable("is stopping"));
    expect(await vm.unplace(place())).toBe(false);
  });
});
