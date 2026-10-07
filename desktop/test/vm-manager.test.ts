import { spawn, spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createInterface } from "node:readline";
import { type ClientHttp2Session, connect as connectH2 } from "node:http2";
import { type Duplex, duplexPair } from "node:stream";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { SANDBOX_STOPPED } from "../src/guest/command.js";
import { Control, type ControlRoots } from "../src/guest/control.js";
import type { ProtectedKey } from "../src/guest/protocol.js";
import { FOLDER_UNAVAILABLE } from "../src/hosts/messages.js";
import { bootLinux, sweep } from "../src/vm/linux.js";
import { type BootVm, bootFor, type Folder, Guest, type ProcessesChange, type VmBackend, VmManager, type VmOptions } from "../src/vm/manager.js";
import { VIRTIOFSD } from "../src/vm/qemu.js";

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "vm-manager-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

async function until(check: () => boolean, ms = 5_000): Promise<void> {
  for (const end = Date.now() + ms; !check(); await new Promise((resolve) => setTimeout(resolve, 20))) {
    if (Date.now() > end) throw new Error("timed out");
  }
}

const options = (): VmOptions => ({
  kernel: "/i/vmlinuz", rootfs: "/i/rootfs.img", agentDisk: "/a/agent.img", sessions: join(dir, "data", "sessions.img"),
  run: join(dir, "run"), console: join(dir, "logs", "console.log"), user: { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" },
});

// *script* as QEMU, first on the PATH, where setpriv looks it up, while *body* runs.
async function withQemu(script: string, body: () => Promise<void>): Promise<void> {
  const bin = join(dir, "bin");
  mkdirSync(bin);
  writeFileSync(join(bin, "qemu-system-x86_64"), `#!/bin/sh\n${script}\n`);
  chmodSync(join(bin, "qemu-system-x86_64"), 0o755);
  const path = process.env.PATH;
  process.env.PATH = `${bin}:${path}`;
  try {
    await body();
  } finally {
    process.env.PATH = path;
  }
}

// The agent's end of the latest fake VM's control port, to say what the agent says unasked,
// and of its net port, where the agent opens its HTTP/2 session.
let agent: Duplex | undefined;
let agentNet: Duplex | undefined;

// A VM whose control port reaches the guest's own Control on *roots*, with no QEMU:
// what the agent is asked, and when. Without roots, an agent that never says hello.
const fakeVm = (roots?: ControlRoots): BootVm => async () => {
  const [host, guest] = duplexPair();
  agent = guest;
  const [net, guestNet] = duplexPair();
  agentNet = guestNet;
  if (roots) {
    const control = new Control((message) => void guest.write(`${JSON.stringify(message)}\n`), roots);
    createInterface({ input: guest }).on("line", (line) => control.receive(line));
    control.hello();
  }
  let gone = (_said: string) => {};
  const exited = new Promise<string>((resolve) => {
    gone = resolve;
  });
  return {
    control: host,
    net,
    exited,
    share: async () => ({ kind: "virtiofs", tag: "r1" }),
    unshare: async () => {},
    kill: async () => {
      host.destroy();
      net.destroy();
      gone("");
    },
  };
};

// *answer*, or "no answer" once *ms* pass.
const within = <T>(answer: Promise<T>, ms: number) =>
  Promise.race([answer, new Promise<"no answer">((resolve) => setTimeout(() => resolve("no answer"), ms))]);

const alive = (pid: number) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};

describe("the VM manager on the host", () => {
  it("ends the QEMU and the virtiofsd a manager that died left in its folder", async () => {
    const run = join(dir, "vm");
    mkdirSync(run);
    // A QEMU with no machine needs no KVM; it writes its own pidfile.
    const qemu = spawn("qemu-system-x86_64", ["-nodefaults", "-display", "none", "-machine", "none", "-pidfile", join(run, "qemu.pid")], { stdio: "ignore" });
    const daemon = spawn(VIRTIOFSD, [`--shared-dir=${dir}`, `--socket-path=${join(run, "vfs-1.sock")}`, "--sandbox=none"], { stdio: "ignore" });
    try {
      await until(() => existsSync(join(run, "qemu.pid")) && existsSync(join(run, "vfs-1.sock")));
      writeFileSync(join(run, "vfs-1.pid"), String(daemon.pid));
      sweep(run);
      await until(() => !alive(qemu.pid!) && !alive(daemon.pid!));
      expect(readdirSync(run)).toEqual([]);
      expect(statSync(run).mode & 0o777).toBe(0o700);
    } finally {
      qemu.kill("SIGKILL");
      daemon.kill("SIGKILL");
    }
  });

  it("spares a process its pidfiles name that is not their QEMU or virtiofsd, whatever its command line holds", async () => {
    const run = join(dir, "vm");
    mkdirSync(run);
    const forever = "setInterval(() => {}, 1000)";
    // Each names this folder's files, as a process that took a stale pidfile's pid could.
    const impostors = [
      spawn(process.execPath, ["-e", forever, join(run, "qemu.pid")], { stdio: "ignore" }),
      spawn(process.execPath, ["-e", forever, "--", `--socket-path=${join(run, "vfs-1.sock")}`], { stdio: "ignore" }),
      spawn(process.execPath, ["-e", forever, run], { stdio: "ignore" }),
    ];
    try {
      writeFileSync(join(run, "qemu.pid"), String(impostors[0]?.pid));
      writeFileSync(join(run, "vfs-1.pid"), String(impostors[1]?.pid));
      // virtiofsd's own pidfile beside its socket is not one of the manager's.
      writeFileSync(join(run, "vfs-1.sock.pid"), String(impostors[2]?.pid));
      sweep(run);
      await new Promise((resolve) => setTimeout(resolve, 200));
      expect(impostors.map((child) => alive(child.pid!))).toEqual([true, true, true]);
    } finally {
      for (const child of impostors) child.kill("SIGKILL");
    }
  });

  it("spares a QEMU or virtiofsd its pidfiles name that is another folder's", async () => {
    const run = join(dir, "vm");
    const other = join(dir, "other");
    mkdirSync(run);
    mkdirSync(other);
    // Another app's, at pids a stale pidfile here could name: the same programs, each naming its own folder's file.
    const qemu = spawn("qemu-system-x86_64", ["-nodefaults", "-display", "none", "-machine", "none", "-pidfile", join(other, "qemu.pid")], { stdio: "ignore" });
    const daemon = spawn(VIRTIOFSD, [`--shared-dir=${dir}`, `--socket-path=${join(other, "vfs-1.sock")}`, "--sandbox=none"], { stdio: "ignore" });
    try {
      await until(() => existsSync(join(other, "qemu.pid")) && existsSync(join(other, "vfs-1.sock")));
      writeFileSync(join(run, "qemu.pid"), String(qemu.pid));
      writeFileSync(join(run, "vfs-1.pid"), String(daemon.pid));
      sweep(run);
      await new Promise((resolve) => setTimeout(resolve, 200));
      expect([alive(qemu.pid!), alive(daemon.pid!)]).toEqual([true, true]);
    } finally {
      qemu.kill("SIGKILL");
      daemon.kill("SIGKILL");
    }
  });

  it("starts nothing for a boot stopped before it began", async () => {
    await withQemu(`echo $$ > '${join(dir, "qemu-pid")}'\nexec sleep 30`, async () => {
      const stop = new AbortController();
      stop.abort();
      await expect(bootLinux(options(), stop.signal, performance.now() + 2_000)).rejects.toThrow("The boot was stopped");
      expect(existsSync(join(dir, "qemu-pid"))).toBe(false);
    });
  });

  it("answers that the sandbox did not start, in QEMU's own words, and makes the sessions disk sparse", async () => {
    await withQemu("echo 'Could not access KVM kernel module: Permission denied' >&2\nexit 1", async () => {
      const manager = new VmManager(options());
      const folder = { path: dir, ...statSync(dir) };
      expect(await manager.perform({ id: "1", root: "root-1", folder, kind: "run", args: {} }, new AbortController().signal)).toEqual({
        error: {
          type: "unavailable",
          message: "This computer's sandbox did not start: QEMU exited: Could not access KVM kernel module: Permission denied",
        },
      });
      const disk = statSync(join(dir, "data", "sessions.img"));
      expect([disk.size, disk.blocks]).toEqual([32 * 1024 ** 3, 0]);
      expect(existsSync(join(dir, "run"))).toBe(true);
    });
  });

  it("does not start without newuidmap and newgidmap, which virtiofsd needs for its maps", async () => {
    await withQemu("exec sleep 30", async () => {
      // The stand-in QEMU alone.
      process.env.PATH = join(dir, "bin");
      await expect(bootLinux(options(), undefined, performance.now() + 500)).rejects.toThrow(
        "virtiofsd needs newuidmap and newgidmap (the uidmap package)",
      );
    });
  });

  it("ends a VM whose monitor does not answer a share in time, so its guest is lost", async () => {
    // A QEMU with its three sockets, whose monitor greets and takes its capabilities, then answers nothing.
    const fake = join(dir, "qemu.cjs");
    writeFileSync(fake, [
      'const net = require("node:net");',
      "const run = process.argv[2];",
      'net.createServer(() => {}).listen(run + "/control.sock");',
      'net.createServer(() => {}).listen(run + "/net.sock");',
      "net.createServer((socket) => {",
      '  socket.write(\'{"QMP": {"version": {}, "capabilities": []}}\\n\');',
      '  socket.once("data", () => socket.write(\'{"return": {}}\\n\'));',
      '}).listen(run + "/qmp.sock");',
    ].join("\n"));
    await withQemu(`exec '${process.execPath}' '${fake}' '${join(dir, "run")}'`, async () => {
      const vm = await bootLinux(options(), undefined, performance.now() + 5_000);
      try {
        let gone = false;
        void vm.exited.then(() => {
          gone = true;
        });
        const begun = performance.now();
        await expect(vm.share(dir, 10_000, performance.now() + 300)).rejects.toThrow("QEMU's monitor did not answer");
        // It fails once the VM has gone: the guest above sees its loss first.
        expect(gone).toBe(true);
        expect(performance.now() - begun).toBeLessThan(2_000);
      } finally {
        await vm.kill();
      }
    });
  });

  // A QEMU with its three sockets, whose monitor takes every command, writes each to dir/commands,
  // and says a device it was asked to delete is deleted, but while dir/stuck is there. As QEMU
  // does, it refuses to delete a device a second time.
  const answering = () => {
    const fake = join(dir, "qemu.cjs");
    writeFileSync(fake, [
      'const fs = require("node:fs");',
      'const net = require("node:net");',
      "const run = process.argv[2];",
      'net.createServer(() => {}).listen(run + "/control.sock");',
      'net.createServer(() => {}).listen(run + "/net.sock");',
      "const deleting = new Set();",
      "net.createServer((socket) => {",
      '  socket.write(\'{"QMP": {"version": {}, "capabilities": []}}\\n\');',
      '  require("node:readline").createInterface({ input: socket }).on("line", (line) => {',
      "    const command = JSON.parse(line);",
      '    fs.appendFileSync(run + "/../commands", line + "\\n");',
      '    if (command.execute === "device_del" && deleting.has(command.arguments.id)) {',
      '      return socket.write(JSON.stringify({ error: { class: "GenericError", desc: `Device ${command.arguments.id} is already in the process of unplug` } }) + "\\n");',
      "    }",
      '    if (command.execute === "device_del") deleting.add(command.arguments.id);',
      '    socket.write(\'{"return": {}}\\n\');',
      '    if (command.execute === "device_del" && !fs.existsSync(run + "/../stuck")) {',
      '      socket.write(JSON.stringify({ event: "DEVICE_DELETED", data: { device: command.arguments.id } }) + "\\n");',
      "    }",
      "  });",
      '}).listen(run + "/qmp.sock");',
    ].join("\n"));
    return `exec '${process.execPath}' '${fake}' '${join(dir, "run")}'`;
  };

  it("gives a removed share's root port to the next, under a number of its own, and ends its virtiofsd", async () => {
    await withQemu(answering(), async () => {
      const vm = await bootLinux(options(), undefined, performance.now() + 5_000);
      try {
        const first = await vm.share(dir, 10_000, performance.now() + 5_000);
        const daemon = Number(readFileSync(join(dir, "run", "vfs-1.pid"), "utf8"));
        // Nothing connects to this virtiofsd: it is ended at the removal's deadline. Asked twice at
        // once, as by two teardowns of one root, it is removed once.
        await Promise.all([vm.unshare(first, performance.now() + 500), vm.unshare(first, performance.now() + 500)]);
        await until(() => !alive(daemon));
        expect(existsSync(join(dir, "run", "vfs-1.pid"))).toBe(false);
        expect(await vm.share(dir, 10_000, performance.now() + 5_000)).toEqual({ kind: "virtiofs", tag: "r2" });
        const commands = readFileSync(join(dir, "commands"), "utf8").trim().split("\n").map((line) => JSON.parse(line) as { execute: string; arguments?: Record<string, unknown> });
        expect(commands.map(({ execute, arguments: args }) => [execute, args?.id, args?.bus])).toEqual([
          ["qmp_capabilities", undefined, undefined],
          ["chardev-add", "vfs1", undefined], ["device_add", "fs1", "rp1"], ["device_del", "fs1", undefined], ["chardev-remove", "vfs1", undefined],
          ["chardev-add", "vfs2", undefined], ["device_add", "fs2", "rp1"],
        ]);
      } finally {
        await vm.kill();
      }
    });
  });

  it("ends a VM whose guest does not let a share go by the removal's deadline", async () => {
    writeFileSync(join(dir, "stuck"), "");
    await withQemu(answering(), async () => {
      const vm = await bootLinux(options(), undefined, performance.now() + 5_000);
      try {
        let gone = false;
        void vm.exited.then(() => {
          gone = true;
        });
        const share = await vm.share(dir, 10_000, performance.now() + 5_000);
        await expect(vm.unshare(share, performance.now() + 300)).rejects.toThrow("the guest did not let r1 go in time");
        expect(gone).toBe(true);
      } finally {
        await vm.kill();
      }
    });
  });

  it("ends a QEMU that opens no sockets by the boot's deadline", async () => {
    await withQemu(`echo $$ > '${join(dir, "qemu-pid")}'\nexec sleep 30`, async () => {
      await expect(bootLinux(options(), undefined, performance.now() + 500)).rejects.toThrow("QEMU did not open its sockets");
      expect(alive(Number(readFileSync(join(dir, "qemu-pid"), "utf8")))).toBe(false);
    });
  });
});

describe("the VM manager on a guest", () => {
  it("tears a root down only once its setup under way has landed", async () => {
    const asked: string[] = [];
    let landed = () => {};
    const roots: ControlRoots = {
      uid: () => 10_000,
      setup: () => {
        asked.push("setup");
        return new Promise<void>((resolve) => {
          landed = () => {
            asked.push("set up");
            resolve();
          };
        });
      },
      teardown: async () => void asked.push("teardown"),
      protect: async () => {},
      perform: async () => ({ ok: true }),
    };
    const manager = new VmManager(options(), fakeVm(roots));
    const folder = { path: dir, ...statSync(dir) };
    const answer = manager.perform({ id: "1", root: "root-1", folder, kind: "which", args: { name: "sh" } }, new AbortController().signal);
    await until(() => asked.includes("setup"));
    const tearing = manager.teardown("root-1");
    await new Promise((resolve) => setTimeout(resolve, 100));
    landed();
    await tearing;
    expect(asked).toEqual(["setup", "set up", "teardown"]);
    expect(await answer).toEqual({ ok: true });
    await manager.stop();
  });
});

describe("a root torn down", () => {
  const R1 = { kind: "virtiofs", tag: "r1" } as const;
  const which = (manager: VmManager) => manager.perform({ id: `which-${Math.random()}`, root: "root-1", folder: { path: dir, ...statSync(dir) }, kind: "which", args: {} }, new AbortController().signal);
  // A guest whose agent and backend say what they were asked, in turn; *unshare* is the backend's removal.
  const recording = (asked: unknown[], unshare: () => Promise<void>): BootVm => {
    const roots: ControlRoots = {
      uid: () => 10_000,
      setup: async (root) => void asked.push(["setup", root]),
      teardown: async (root, share) => void asked.push(["teardown", root, share]),
      protect: async () => {},
      perform: async () => ({ ok: true }),
    };
    return async (...args) => {
      const vm = await fakeVm(roots)(...args);
      asked.push(["boot"]);
      return {
        ...vm,
        share: async () => {
          asked.push(["share"]);
          return R1;
        },
        unshare: async (share) => {
          asked.push(["unshare", share]);
          await unshare();
        },
      };
    };
  };

  it("ends in the guest before its folder leaves it, and has its folder shared and itself set up again by its next operation", async () => {
    const asked: unknown[] = [];
    const manager = new VmManager(options(), recording(asked, async () => {}));
    expect(await which(manager)).toEqual({ ok: true });
    await manager.teardown("root-1");
    expect(await which(manager)).toEqual({ ok: true });
    expect(asked).toEqual([["boot"], ["share"], ["setup", "root-1"], ["teardown", "root-1", R1], ["unshare", R1], ["share"], ["setup", "root-1"]]);
    await manager.stop();
  });

  it("loses a guest that does not let its folder go, and boots a new one for the next operation", async () => {
    const asked: unknown[] = [];
    const manager = new VmManager(options(), recording(asked, async () => {
      throw new Error("the guest did not let r1 go in time");
    }));
    expect(await which(manager)).toEqual({ ok: true });
    await manager.teardown("root-1");
    expect(await which(manager)).toEqual({ ok: true });
    expect(asked.filter((step) => (step as string[])[0] === "boot")).toHaveLength(2);
    await manager.stop();
  });
});

describe("a root's protected keys in the guest", () => {
  const KEYS: ProtectedKey[] = [["/f/.git", 40, "rw"], ["/f/.git/config", 41, "ro"]];
  let asked: unknown[];
  let refuse: boolean;
  // Binds that take a while, as a few hundred do: what has been bound is told once they are.
  let slow: boolean;
  const roots = (): ControlRoots => ({
    uid: () => 10_000,
    setup: async (root) => void asked.push(["setup", root]),
    teardown: async () => {},
    protect: async (root, keys) => {
      asked.push(["protect", root, keys]);
      if (slow) {
        await new Promise((resolve) => setTimeout(resolve, 300));
        asked.push(["bound", root]);
      }
      if (refuse) throw new Error("Blocked: the computer could not make these protected files read-only in its sandbox");
    },
    perform: async (root, kind) => {
      asked.push([kind, root]);
      return { ok: true };
    },
  });
  const run = (manager: VmManager, protect?: ProtectedKey[], kind = "run") => manager.perform({
    id: `${kind}-${Math.random()}`, root: "root-1", folder: { path: dir, ...statSync(dir) }, kind, args: {}, ...(protect ? { protect } : {}),
  }, new AbortController().signal);

  beforeEach(() => {
    asked = [];
    refuse = false;
    slow = false;
  });

  it("are made read-only after the root's setup and before its command, asked again only once they change or it is set up again", async () => {
    const manager = new VmManager(options(), fakeVm(roots()));
    expect(await run(manager, KEYS)).toEqual({ ok: true });
    expect(await run(manager, KEYS)).toEqual({ ok: true });
    // A kind that runs no command brings none.
    expect(await run(manager, undefined, "poll")).toEqual({ ok: true });
    const changed: ProtectedKey[] = [["/f/.git", 40, "rw"], ["/f/.git/config", 42, "ro"]];
    expect(await run(manager, changed)).toEqual({ ok: true });
    // The root's runner is lost: its namespace is made again, and its keys bound there again.
    agent?.write(`${JSON.stringify({ type: "lost", root: "root-1" })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(await run(manager, changed)).toEqual({ ok: true });
    expect(asked).toEqual([
      ["setup", "root-1"], ["protect", "root-1", KEYS], ["run", "root-1"], ["run", "root-1"], ["poll", "root-1"],
      ["protect", "root-1", changed], ["run", "root-1"],
      ["setup", "root-1"], ["protect", "root-1", changed], ["run", "root-1"],
    ]);
    await manager.stop();
  });

  it("refuse the root's command while they cannot be made read-only, and are asked again with the next", async () => {
    const manager = new VmManager(options(), fakeVm(roots()));
    refuse = true;
    expect(await run(manager, KEYS)).toEqual({
      error: { type: "sandbox", message: "Blocked: the computer could not make these protected files read-only in its sandbox" },
    });
    refuse = false;
    expect(await run(manager, KEYS)).toEqual({ ok: true });
    expect(asked).toEqual([["setup", "root-1"], ["protect", "root-1", KEYS], ["protect", "root-1", KEYS], ["run", "root-1"]]);
    await manager.stop();
  });

  it("are made read-only as the root's file host finds them between commands, once a guest has the root set up", async () => {
    const manager = new VmManager(options(), fakeVm(roots()));
    await manager.protect("root-1", KEYS);
    expect(await run(manager, undefined, "which")).toEqual({ ok: true });
    await manager.protect("root-1", KEYS);
    await manager.protect("root-1", KEYS);
    await manager.protect("root-2", KEYS);
    expect(asked).toEqual([["setup", "root-1"], ["which", "root-1"], ["protect", "root-1", KEYS]]);
    await manager.stop();
  });

  // The root's runner is lost while a command's keys are being bound, and another operation of the root sets it up again first.
  const lostWhileBinding = async (manager: VmManager, other: () => Promise<unknown>) => {
    slow = true;
    const first = run(manager, KEYS);
    await until(() => asked.length === 2);
    agent?.write(`${JSON.stringify({ type: "lost", root: "root-1" })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(await other()).toEqual({ ok: true });
    expect(await first).toEqual({ ok: true });
  };

  it("are bound again in a namespace set up again by a poll while they were being bound, before the command runs there", async () => {
    const manager = new VmManager(options(), fakeVm(roots()));
    await lostWhileBinding(manager, () => run(manager, undefined, "poll"));
    expect(asked).toEqual([
      ["setup", "root-1"], ["protect", "root-1", KEYS], ["setup", "root-1"], ["poll", "root-1"], ["bound", "root-1"],
      ["protect", "root-1", KEYS], ["bound", "root-1"], ["run", "root-1"],
    ]);
    await manager.stop();
  });

  it("are bound in a namespace set up again by another command while they were being bound, before either command runs there", async () => {
    const manager = new VmManager(options(), fakeVm(roots()));
    await lostWhileBinding(manager, () => run(manager, KEYS));
    expect(asked).toEqual([
      ["setup", "root-1"], ["protect", "root-1", KEYS], ["setup", "root-1"], ["bound", "root-1"],
      ["protect", "root-1", KEYS], ["bound", "root-1"], ["run", "root-1"], ["run", "root-1"],
    ]);
    await manager.stop();
  });
});

describe("a root's network, through the guest's net port", () => {
  const roots: ControlRoots = {
    uid: () => 10_000, setup: async () => {}, teardown: async () => {}, protect: async () => {},
    perform: async () => ({ ok: { output: "done\n", returncode: 0, timed_out: false } }),
  };
  const run = (manager: VmManager, root = "root-1") => manager.perform({
    id: `run-${Math.random()}`, root, folder: { path: dir, ...statSync(dir) }, kind: "run", args: {},
  }, new AbortController().signal);
  // A connection the agent opens for *root* in its one session on the net port, as its tunnels
  // do: the status the host proxy answers, and its reason.
  const connection = (session: ClientHttp2Session, authority: string, root = "root-1") => new Promise<[number, unknown]>((resolve) => {
    const stream = session.request({ ":method": "CONNECT", ":authority": authority, "surogate-root": root });
    stream.on("response", (headers) => resolve([Number(headers[":status"]), headers["surogate-reason"]]));
  });

  it("is judged by the host proxy, for the root the agent names, and told in that root's next run", async () => {
    const asked: unknown[] = [];
    const manager = new VmManager(options(), fakeVm(roots), () => {}, { ask: async (root, request) => (asked.push([root, request]), false) });
    expect(await run(manager)).toEqual({ ok: { output: "done\n", returncode: 0, timed_out: false } });
    const session = connectH2("http://guest", { createConnection: () => agentNet as Duplex });
    expect(await connection(session, "127.0.0.1:9")).toEqual([403, "own"]);
    // TEST-NET-1: an address elsewhere, which the egress denies.
    expect(await connection(session, "192.0.2.1:9")).toEqual([403, "denied"]);
    // A root named that is no root id at all.
    expect(await connection(session, "192.0.2.1:9", "../etc")).toEqual([403, "invalid"]);
    expect(asked).toEqual([["root-1", { host: "192.0.2.1", port: 9, privateNetwork: false }]]);
    expect(await run(manager)).toEqual({
      ok: {
        output: "done\n\nThis computer does not let a chat reach its own network services (127.0.0.1:9)\nThis computer did not allow network access to 192.0.2.1:9.",
        returncode: 0,
        timed_out: false,
      },
    });
    await manager.stop();
  });

  it("starts a root set up again with nothing met, even by a connection that landed once it was torn down", async () => {
    const manager = new VmManager(options(), fakeVm(roots));
    await run(manager);
    const session = connectH2("http://guest", { createConnection: () => agentNet as Duplex });
    await manager.teardown("root-1");
    // As a connection still in flight at the teardown lands.
    expect(await connection(session, "127.0.0.1:9")).toEqual([403, "own"]);
    expect(await run(manager)).toEqual({ ok: { output: "done\n", returncode: 0, timed_out: false } });
    await manager.stop();
  });

  it("refuses every destination off the package hosts without an egress to ask", async () => {
    const manager = new VmManager(options(), fakeVm(roots));
    await run(manager);
    const session = connectH2("http://guest", { createConnection: () => agentNet as Duplex });
    expect(await connection(session, "192.0.2.1:9")).toEqual([403, "denied"]);
    await manager.stop();
  });
});

describe("a root's processes in the guest", () => {
  it("are told as they change, refused whole past a registry's count or a handle's shape and size, and told gone with every root of a guest that goes", async () => {
    const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, protect: async () => {}, perform: async () => ({ ok: true }) };
    const told: Array<[string, ProcessesChange]> = [];
    const vms: VmBackend[] = [];
    const boot: BootVm = async (...args) => {
      const vm = await fakeVm(roots)(...args);
      vms.push(vm);
      return vm;
    };
    const manager = new VmManager(options(), boot, (root, change) => told.push([root, change]));
    const folder = { path: dir, ...statSync(dir) };
    expect(await manager.perform({ id: "1", root: "root-1", folder, kind: "which", args: {} }, new AbortController().signal)).toEqual({ ok: true });
    const handle = { id: "proc_000000000001", command: "sleep 9", cwd: dir, task_id: null, started_at: 1 };
    const warned = vi.spyOn(console, "warn").mockImplementation(() => {});
    const say = (handles: unknown[]) => agent?.write(`${JSON.stringify({ type: "handles", root: "root-1", handles, live: 1 })}\n`);
    try {
      say([handle, { id: 7 }]);
      say(Array.from({ length: 65 }, () => handle));
      say([{ ...handle, command: "x".repeat(4097) }]);
      say([handle]);
      await until(() => told.length === 1);
      expect(warned).toHaveBeenCalledTimes(3);
    } finally {
      warned.mockRestore();
    }
    expect(told).toEqual([["root-1", { handles: [handle], live: 1 }]]);
    // The guest goes on.
    expect(await manager.perform({ id: "2", root: "root-1", folder, kind: "which", args: {} }, new AbortController().signal)).toEqual({ ok: true });
    vms[0]?.control.destroy();
    await until(() => told.length === 2);
    expect(told[1]).toEqual(["root-1", { gone: true }]);
    await manager.stop();
  });
});

describe("a guest that goes", () => {
  it("is followed by a guest booted once all of its VM has gone, as its disks are then free", async () => {
    const roots: ControlRoots = {
      uid: () => 10_000,
      setup: async () => {},
      teardown: async () => {},
      protect: async () => {},
      // A run that never ends; which answers at once.
      perform: (_root, kind) => new Promise((resolve) => kind === "which" && resolve({ ok: true })),
    };
    const events: string[] = [];
    const vms: VmBackend[] = [];
    const boot: BootVm = async (...args) => {
      events.push("boot");
      const vm = await fakeVm(roots)(...args);
      vms.push(vm);
      // Its hypervisor takes a while to end, as QEMU does to let its disks go.
      return { ...vm, kill: async () => {
        await new Promise((resolve) => setTimeout(resolve, 200));
        await vm.kill();
        events.push("gone");
      } };
    };
    const manager = new VmManager(options(), boot);
    const folder = { path: dir, ...statSync(dir) };
    const which = () => manager.perform({ id: `which-${Math.random()}`, root: "root-1", folder, kind: "which", args: {} }, new AbortController().signal);
    expect(await which()).toEqual({ ok: true });
    const running = manager.perform({ id: "run", root: "root-1", folder, kind: "run", args: {} }, new AbortController().signal);
    await new Promise((resolve) => setTimeout(resolve, 50));
    // Its control channel goes, as a guest's that panics does.
    vms[0]?.control.destroy();
    expect(await running).toEqual(SANDBOX_STOPPED);
    expect(await which()).toEqual({ ok: true });
    expect(events.slice(0, 3)).toEqual(["boot", "gone", "boot"]);
    await manager.stop();
  });
});

describe("a chat's folder, checked again before it is shared", () => {
  const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, protect: async () => {}, perform: async () => ({ ok: true }) };
  const which = (manager: VmManager, root: string, folder: Folder) =>
    manager.perform({ id: `which-${root}`, root, folder, kind: "which", args: {} }, new AbortController().signal);

  // st_dev belongs to a mount, which a reboot can number anew: after one, only the inode is compared.
  it("is taken after a reboot that changed its device number", async () => {
    const manager = new VmManager(options(), fakeVm(roots));
    const { dev, ino } = statSync(dir);
    expect(await which(manager, "root-1", { path: dir, dev: dev + 1, ino, boot: "another-boot" })).toEqual({ ok: true });
    await manager.stop();
  });

  // An unreadable boot id, or none, compares as this boot.
  it("is refused on another device in the boot it was bound in, or with another inode after a reboot", async () => {
    const manager = new VmManager(options(), fakeVm(roots));
    const { dev, ino } = statSync(dir);
    const cases: Folder[] = [
      { path: dir, dev: dev + 1, ino, boot: BOOT_ID },
      { path: dir, dev: dev + 1, ino, boot: "" },
      { path: dir, dev: dev + 1, ino },
      { path: dir, dev, ino: ino + 1, boot: "another-boot" },
    ];
    for (const [n, folder] of cases.entries()) expect(await which(manager, `root-${n}`, folder)).toEqual(FOLDER_UNAVAILABLE);
    await manager.stop();
  });
});

// A folder on a FUSE mount whose daemon is stopped: every look into it waits, as on a
// dead network mount. Bound first, as a chat's folder is; let go by *release*.
function stalledFolder(): { folder: Folder; release: () => void } {
  const fuse = join(dir, "fuse");
  for (const name of ["lower/folder", "upper", "work", "mnt"]) mkdirSync(join(fuse, name), { recursive: true });
  const mnt = join(fuse, "mnt");
  const mounted = spawnSync("fuse-overlayfs", ["-o", `lowerdir=${fuse}/lower,upperdir=${fuse}/upper,workdir=${fuse}/work,timeout=0`, mnt]);
  if (mounted.status !== 0) throw new Error(`fuse-overlayfs: ${mounted.stderr}`);
  const path = join(mnt, "folder");
  const { dev, ino } = statSync(path);
  const pid = Number(spawnSync("pgrep", ["-f", `^fuse-overlayfs .* ${mnt}$`], { encoding: "utf8" }).stdout.trim());
  process.kill(pid, "SIGSTOP");
  return {
    folder: { path, dev, ino },
    release: () => {
      process.kill(pid, "SIGCONT");
      spawnSync("fusermount3", ["-u", mnt]);
    },
  };
}

describe("a chat's folder on a mount that does not answer", () => {
  it("is answered as unavailable within the share's bound, and its root torn down without waiting on it", async () => {
    const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, protect: async () => {}, perform: async () => ({ ok: true }) };
    const manager = new VmManager({ ...options(), shareMs: 300 }, fakeVm(roots));
    const { folder, release } = stalledFolder();
    try {
      const begun = performance.now();
      const answer = manager.perform({ id: "1", root: "root-1", folder, kind: "which", args: {} }, new AbortController().signal);
      expect(await within(answer, 3_000)).toEqual({
        error: { type: "unavailable", message: "This computer's sandbox could not add this chat's folder: it did not answer within 0.3 s" },
      });
      expect(await within(manager.teardown("root-1"), 1_000)).toBeUndefined();
      expect(performance.now() - begun).toBeLessThan(2_000);
      await manager.stop();
    } finally {
      release();
    }
  });
});

describe("a guest's boot", () => {
  it("ends a VM whose boot is stopped just as its backend returns it", async () => {
    const stop = new AbortController();
    const boot: BootVm = async (...args) => {
      const vm = await fakeVm()(...args);
      // After the backend's own listener has gone, before the guest's is added.
      stop.abort();
      return vm;
    };
    const begun = performance.now();
    await expect(Guest.boot(boot, options(), stop.signal)).rejects.toThrow("The VM exited");
    expect(performance.now() - begun).toBeLessThan(1_000);
  });
});

describe("the VM's backend", () => {
  it("is Linux's on Linux, and none yet elsewhere, where an operation answers so", async () => {
    expect(bootFor("linux")).toBe(bootLinux);
    expect([bootFor("win32"), bootFor("darwin")]).toEqual([null, null]);
    const manager = new VmManager(options(), null);
    const folder = { path: dir, ...statSync(dir) };
    expect(await manager.perform({ id: "1", root: "root-1", folder, kind: "run", args: {} }, new AbortController().signal)).toEqual({
      error: { type: "unavailable", message: "This computer's sandbox is not available on this platform yet" },
    });
    expect(existsSync(join(dir, "run"))).toBe(false);
  });
});
