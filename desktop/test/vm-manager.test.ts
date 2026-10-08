import { spawn } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir, userInfo } from "node:os";
import { join } from "node:path";

import { createInterface } from "node:readline";
import { type ClientHttp2Session, connect as connectH2 } from "node:http2";
import { type Duplex, duplexPair } from "node:stream";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { CANCELLED, SANDBOX_STOPPED } from "../src/guest/command.js";
import { Control, type ControlRoots } from "../src/guest/control.js";
import { FOLDER_UNAVAILABLE } from "../src/hosts/messages.js";
import { bootLinux, emulation, missingTools, sweep } from "../src/vm/linux.js";
import {
  type Boot, type BootVm, bootFor, EMULATED_NOTICE, type Emulated, type Folder, Guest, type ProcessesChange, unavailable, type VmBackend, VmManager, type VmOptions, WAITS,
} from "../src/vm/manager.js";
import { VIRTIOFSD } from "../src/vm/qemu.js";

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "vm-manager-"));
  // A KVM device this user opens, so that no stand-in boot reads this computer's own.
  writeFileSync(join(dir, "kvm"), "");
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
  run: join(dir, "run"), console: join(dir, "logs", "console.log"), user: { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" }, kvm: join(dir, "kvm"),
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
// *powers*: its agent powers the fake VM off at a shutdown, as the guest's ends QEMU.
// *emulated*: why it runs emulated, or null with KVM.
const fakeVm = (roots?: ControlRoots, powers = true, emulated: Emulated | null = null): BootVm => async () => {
  const [host, guest] = duplexPair();
  agent = guest;
  const [net, guestNet] = duplexPair();
  agentNet = guestNet;
  let gone = (_said: string) => {};
  const exited = new Promise<string>((resolve) => {
    gone = resolve;
  });
  const kill = async () => {
    host.destroy();
    net.destroy();
    gone("");
  };
  if (roots) {
    const machine = powers ? { setClock: async () => {}, woke: () => {}, heard: () => {}, powerOff: kill } : undefined;
    const control = new Control((message) => void guest.write(`${JSON.stringify(message)}\n`), roots, machine);
    createInterface({ input: guest }).on("line", (line) => control.receive(line));
    control.hello();
  }
  return { control: host, net, exited, emulated, share: async () => ({ kind: "virtiofs", tag: "r1" }), unshare: async () => {}, kill };
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

  it("names each tool the VM lacks, a QEMU or virtiofsd older than it needs among them", async () => {
    const bin = join(dir, "tools");
    mkdirSync(bin);
    const tool = (name: string, script: string) => {
      writeFileSync(join(bin, name), `#!/bin/sh\n${script}\n`);
      chmodSync(join(bin, name), 0o755);
      return join(bin, name);
    };
    const path = process.env.PATH;
    // The stand-ins alone: no newuidmap or newgidmap on the PATH, and no zstd.
    process.env.PATH = bin;
    try {
      tool("qemu-system-x86_64", "echo 'QEMU emulator version 8.1.5 (Debian 1:8.1.5+ds-1ubuntu2)'");
      const virtiofsd = tool("virtiofsd", "echo 'virtiofsd 1.9.0'");
      const zstd = join(bin, "zstd");
      expect(await missingTools({ virtiofsd, zstd })).toEqual(["QEMU 8.2 or later", "virtiofsd 1.10 or later", "newuidmap and newgidmap", "zstd"]);
      // zstd only unpacks the image's download: not needed once the image is here, or with none to deliver.
      expect(await missingTools({ virtiofsd, zstd }, false)).toEqual(["QEMU 8.2 or later", "virtiofsd 1.10 or later", "newuidmap and newgidmap"]);
      tool("qemu-system-x86_64", "echo 'QEMU emulator version 10.1.0 (Debian 1:10.1.0+ds-5ubuntu2)'");
      tool("virtiofsd", "echo 'virtiofsd 1.13.2'");
      for (const name of ["newuidmap", "newgidmap", "zstd"]) tool(name, "true");
      expect(await missingTools({ virtiofsd, zstd })).toEqual([]);
      // One that says no version, or is not there, is missing too.
      tool("qemu-system-x86_64", "exit 1");
      rmSync(virtiofsd);
      expect(await missingTools({ virtiofsd, zstd })).toEqual(["QEMU 8.2 or later", "virtiofsd 1.10 or later"]);
    } finally {
      process.env.PATH = path;
    }
  });

  it("gives up on a tool whose --version has not ended in 5 s, though it ignores the timeout's SIGTERM", { timeout: 20_000 }, async () => {
    const virtiofsd = join(dir, "virtiofsd");
    writeFileSync(virtiofsd, "#!/bin/sh\ntrap '' TERM\nexec sleep 30\n");
    chmodSync(virtiofsd, 0o755);
    const begun = performance.now();
    expect(await missingTools({ virtiofsd })).toContain("virtiofsd 1.10 or later");
    expect(performance.now() - begun).toBeLessThan(8_000);
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
    // Its last root gone, the guest stopped: the next operation boots another.
    expect(asked).toEqual([["boot"], ["share"], ["setup", "root-1"], ["teardown", "root-1", R1], ["unshare", R1], ["boot"], ["share"], ["setup", "root-1"]]);
    await manager.stop();
  });

  it("keeps the share of a root whose processes would not end in the guest, and loses no guest for it", async () => {
    const asked: unknown[] = [];
    const roots: ControlRoots = {
      uid: () => 10_000,
      setup: async (root) => void asked.push(["setup", root]),
      // As the agent answers when something of the root waits on a share that stalled.
      teardown: async (root) => {
        if (root === "root-1") throw new Error("What this chat ran is waiting on its folder, which does not answer");
      },
      perform: async () => ({ ok: true }),
    };
    let boots = 0;
    const boot: BootVm = async (...args) => {
      boots += 1;
      const vm = await fakeVm(roots)(...args);
      return { ...vm, unshare: async (share) => void asked.push(["unshare", share]) };
    };
    const manager = new VmManager(options(), boot);
    const on = (root: string) => manager.perform({ id: `which-${Math.random()}`, root, folder: { path: dir, ...statSync(dir) }, kind: "which", args: {} }, new AbortController().signal);
    expect(await on("root-1")).toEqual({ ok: true });
    // Another root keeps the guest running.
    expect(await on("root-2")).toEqual({ ok: true });
    await manager.teardown("root-1");
    expect(await on("root-1")).toEqual({ ok: true });
    expect(asked).toEqual([["setup", "root-1"], ["setup", "root-2"], ["setup", "root-1"]]);
    expect(boots).toBe(1);
    await manager.stop();
  });

  it("lets the share of a root go when its teardown fails for any other reason, and loses no guest for it", async () => {
    const asked: unknown[] = [];
    const roots: ControlRoots = {
      uid: () => 10_000,
      setup: async () => {},
      teardown: async () => {
        throw new Error("not a share tag: ../r1");
      },
      perform: async () => ({ ok: true }),
    };
    let boots = 0;
    const boot: BootVm = async (...args) => {
      boots += 1;
      const vm = await fakeVm(roots)(...args);
      return { ...vm, unshare: async (share) => void asked.push(["unshare", share]) };
    };
    const manager = new VmManager(options(), boot);
    const on = (root: string) => manager.perform({ id: `which-${Math.random()}`, root, folder: { path: dir, ...statSync(dir) }, kind: "which", args: {} }, new AbortController().signal);
    expect(await on("root-1")).toEqual({ ok: true });
    // Another root keeps the guest running.
    expect(await on("root-2")).toEqual({ ok: true });
    await manager.teardown("root-1");
    expect(asked).toEqual([["unshare", R1]]);
    expect(boots).toBe(1);
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

describe("a root's network, through the guest's net port", () => {
  const roots: ControlRoots = {
    uid: () => 10_000, setup: async () => {}, teardown: async () => {},
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

  it("keeps what a root met for its next run that answers, past one that answers with an error", async () => {
    const answers = [{ ok: { output: "done\n", returncode: 0, timed_out: false } }, CANCELLED];
    const manager = new VmManager(options(), fakeVm({ ...roots, perform: async () => answers.shift() ?? { ok: { output: "done\n", returncode: 0, timed_out: false } } }));
    await run(manager);
    const session = connectH2("http://guest", { createConnection: () => agentNet as Duplex });
    expect(await connection(session, "127.0.0.1:9")).toEqual([403, "own"]);
    // A run the session cancelled while the connection was refused.
    expect(await run(manager)).toEqual(CANCELLED);
    expect(await run(manager)).toEqual({
      ok: { output: "done\n\nThis computer does not let a chat reach its own network services (127.0.0.1:9)", returncode: 0, timed_out: false },
    });
    await manager.stop();
  });

  it("starts a root set up again with nothing met, even by a connection that landed once it was torn down", async () => {
    const manager = new VmManager(options(), fakeVm(roots));
    await run(manager);
    // Another root keeps the guest running past this one's teardown.
    await run(manager, "root-2");
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
    const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) };
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
  const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) };
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

describe("a guest's stop", () => {
  const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) };

  it("asks the guest to power off, and settles once it has, asked once however often it is stopped", async () => {
    const asked: string[] = [];
    const listening: BootVm = async (...args) => {
      const vm = await fakeVm(roots)(...args);
      createInterface({ input: agent as Duplex }).on("line", (line) => asked.push((JSON.parse(line) as { type: string }).type));
      return vm;
    };
    const guest = await Guest.boot(listening, options());
    const begun = performance.now();
    await Promise.all([guest.stop(), guest.stop()]);
    expect(performance.now() - begun).toBeLessThan(500);
    expect(asked.filter((type) => type === "shutdown")).toHaveLength(1);
    expect([guest.ended, guest.lost]).toEqual([true, false]);
  });

  it("ends a VM still running once the power-off's bound has passed", async () => {
    // An agent with no power to switch off: the shutdown goes unheeded.
    const guest = await Guest.boot(fakeVm(roots, false), { ...options(), powerOffMs: 300 });
    const begun = performance.now();
    await guest.stop();
    expect(performance.now() - begun).toBeGreaterThanOrEqual(290);
    expect(performance.now() - begun).toBeLessThan(1_000);
    expect([guest.ended, guest.lost]).toEqual([true, false]);
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

describe("a guest's lifecycle", () => {
  const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) };
  const which = (manager: VmManager, root = "root-1", signal = new AbortController().signal) =>
    manager.perform({ id: `which-${Math.random()}`, root, folder: { path: dir, ...statSync(dir) }, kind: "which", args: {} }, signal);
  // A fake VM per boot, each telling *events* when it boots and when it powers off or is ended.
  const counted = (events: string[], answering: ControlRoots = roots): BootVm => async (...args) => {
    events.push("boot");
    const vm = await fakeVm(answering)(...args);
    void vm.exited.then(() => events.push("gone"));
    return vm;
  };
  // One whose agent cannot power off: its stop takes the power-off's bound, 300 ms.
  const slowToStop = (events: string[]): BootVm => async (...args) => {
    events.push("boot");
    const vm = await fakeVm(roots, false)(...args);
    void vm.exited.then(() => events.push("gone"));
    return vm;
  };

  it("stops a guest once its last root's folder is let go, and boots another at once for the next operation", async () => {
    const events: string[] = [];
    const manager = new VmManager(options(), counted(events));
    expect(await which(manager, "root-1")).toEqual({ ok: true });
    expect(await which(manager, "root-2")).toEqual({ ok: true });
    await manager.teardown("root-1");
    // Another root is still set up in it.
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(events).toEqual(["boot"]);
    await manager.teardown("root-2");
    await until(() => events.includes("gone"));
    // A guest that stopped is no failure to back off from.
    const begun = performance.now();
    expect(await which(manager)).toEqual({ ok: true });
    expect(performance.now() - begun).toBeLessThan(500);
    expect(events).toEqual(["boot", "gone", "boot"]);
    await manager.stop();
  });

  it("stops a guest booted for an operation cancelled while it booted", async () => {
    const events: string[] = [];
    let booted: (() => void) | null = null;
    const slow: BootVm = async (...args) => {
      await new Promise<void>((resolve) => {
        booted = resolve;
      });
      return counted(events)(...args);
    };
    const manager = new VmManager(options(), slow);
    const cancel = new AbortController();
    const answer = which(manager, "root-1", cancel.signal);
    cancel.abort();
    expect(await answer).toEqual(CANCELLED);
    await until(() => booted !== null);
    booted!();
    await until(() => events.includes("gone"));
    expect(events).toEqual(["boot", "gone"]);
    await manager.stop();
  });

  it("boots another for an operation that comes while its idle guest powers off, once that one has gone", async () => {
    const events: string[] = [];
    const manager = new VmManager({ ...options(), powerOffMs: 300 }, slowToStop(events));
    expect(await which(manager)).toEqual({ ok: true });
    await manager.teardown("root-1");
    expect(await which(manager)).toEqual({ ok: true });
    expect(events).toEqual(["boot", "gone", "boot"]);
    await manager.stop();
  });

  it("stops once, when it is stopped while its idle guest powers off", async () => {
    const events: string[] = [];
    const manager = new VmManager({ ...options(), powerOffMs: 300 }, slowToStop(events));
    expect(await which(manager)).toEqual({ ok: true });
    await manager.teardown("root-1");
    const begun = performance.now();
    await manager.stop();
    expect(performance.now() - begun).toBeLessThan(1_000);
    expect(events).toEqual(["boot", "gone"]);
  });

  it("answers what comes while a boot that failed backs off with that failure, and boots again once it has passed", async () => {
    let boots = 0;
    const failing: BootVm = async () => {
      boots += 1;
      throw new Error("QEMU exited: the protected-names rule attached 0 of 11 hooks");
    };
    const manager = new VmManager(options(), failing);
    const failed = { error: { type: "unavailable", message: "This computer's sandbox did not start: QEMU exited: the protected-names rule attached 0 of 11 hooks" } };
    expect(await which(manager)).toEqual(failed);
    expect(await which(manager)).toEqual(failed);
    expect(boots).toBe(1);
    await new Promise((resolve) => setTimeout(resolve, 1_100));
    expect(await which(manager)).toEqual(failed);
    expect(boots).toBe(2);
    await manager.stop();
  });

  it("boots again at once when asked to retry, the failure it backed off from forgotten", async () => {
    let boots = 0;
    const failing: BootVm = async () => {
      boots += 1;
      throw new Error("QEMU exited: no hello within 15 s");
    };
    const manager = new VmManager(options(), failing);
    const failed = unavailable("did not start: QEMU exited: no hello within 15 s");
    expect(await which(manager)).toEqual(failed);
    expect(await which(manager)).toEqual(failed);
    expect(boots).toBe(1);
    // The user's Retry, its image checked: the next operation boots, inside the backoff.
    manager.retry();
    expect(await which(manager)).toEqual(failed);
    expect(boots).toBe(2);
    await manager.stop();
  });

  it("waits a second before it boots again after a guest that crashed", async () => {
    const events: string[] = [];
    const vms: VmBackend[] = [];
    const boot: BootVm = async (...args) => {
      const vm = await counted(events)(...args);
      vms.push(vm);
      return vm;
    };
    const manager = new VmManager(options(), boot);
    expect(await which(manager)).toEqual({ ok: true });
    // Its control channel goes, as a guest's that panics does.
    vms[0]?.control.destroy();
    await until(() => events.includes("gone"));
    const begun = performance.now();
    expect(await which(manager)).toEqual({ ok: true });
    expect(performance.now() - begun).toBeGreaterThan(900);
    await manager.stop();
  });

  it("answers what waits out a crashed guest's backoff as stopping, at once, when it is stopped", async () => {
    const events: string[] = [];
    const vms: VmBackend[] = [];
    const boot: BootVm = async (...args) => {
      const vm = await counted(events)(...args);
      vms.push(vm);
      return vm;
    };
    const manager = new VmManager(options(), boot);
    expect(await which(manager)).toEqual({ ok: true });
    vms[0]?.control.destroy();
    await until(() => events.includes("gone"));
    const waiting = which(manager);
    await new Promise((resolve) => setTimeout(resolve, 100));
    const stopped = manager.stop();
    expect(await within(waiting, 500)).toEqual(unavailable("is stopping"));
    expect(await within(stopped, 500)).toBeUndefined();
    expect(events).toEqual(["boot", "gone"]);
  });

  it("boots again on a new sessions disk when the guest could not check its own, and keeps the old one beside it", async () => {
    const { sessions, console: log } = options();
    mkdirSync(join(dir, "data"), { recursive: true });
    mkdirSync(join(dir, "logs"), { recursive: true });
    writeFileSync(sessions, "the homes");
    let boots = 0;
    const boot: BootVm = async (...args) => {
      boots += 1;
      if (boots === 1) {
        writeFileSync(log, "surogate: e2fsck could not check the sessions disk (exit code 8), so the guest stops; the app keeps the disk aside and makes a new one\n");
        throw new Error("The VM exited");
      }
      return fakeVm(roots)(...args);
    };
    const manager = new VmManager(options(), boot);
    expect(await which(manager)).toEqual({ ok: true });
    expect([boots, existsSync(sessions), readFileSync(`${sessions}.unchecked`, "utf8")]).toEqual([2, false, "the homes"]);
    await manager.stop();
  });

  it("keeps the sessions disk where it is when a boot fails before its guest says why, whatever an earlier boot's console said", async () => {
    const { sessions, console: log } = options();
    mkdirSync(join(dir, "data"), { recursive: true });
    mkdirSync(join(dir, "logs"), { recursive: true });
    writeFileSync(sessions, "the homes");
    writeFileSync(log, "surogate: e2fsck could not check the sessions disk (exit code 8), so the guest stops; the app keeps the disk aside and makes a new one\n");
    const manager = new VmManager(options(), async () => {
      throw new Error("QEMU exited: Could not access KVM kernel module: Permission denied");
    });
    expect(await which(manager)).toEqual({
      error: { type: "unavailable", message: "This computer's sandbox did not start: QEMU exited: Could not access KVM kernel module: Permission denied" },
    });
    expect([readFileSync(sessions, "utf8"), existsSync(`${sessions}.unchecked`)]).toEqual(["the homes", false]);
    await manager.stop();
  });

  it("stops a guest whose agent does not answer a setup, and boots another for the next operation", async () => {
    const events: string[] = [];
    let answer = false;
    const stuck: ControlRoots = { ...roots, setup: () => (answer ? Promise.resolve() : new Promise<void>(() => {})) };
    const manager = new VmManager({ ...options(), setupMs: 200 }, counted(events, stuck));
    const begun = performance.now();
    expect(await which(manager)).toEqual(SANDBOX_STOPPED);
    expect(performance.now() - begun).toBeLessThan(1_000);
    answer = true;
    expect(await which(manager)).toEqual({ ok: true });
    expect(events.slice(0, 3)).toEqual(["boot", "gone", "boot"]);
    await manager.stop();
  });
});

describe("a command's timeout", () => {
  it("is this computer's to keep: at it the command is cancelled in the guest and answered as timed out", async () => {
    let cancelled = false;
    // An agent whose run never answers, as one whose backstop lies past the timeout.
    const roots: ControlRoots = {
      uid: () => 10_000, setup: async () => {}, teardown: async () => {},
      perform: (_root, _kind, _args, signal) => new Promise((resolve) => signal.addEventListener("abort", () => {
        cancelled = true;
        resolve(CANCELLED);
      })),
    };
    const manager = new VmManager(options(), fakeVm(roots));
    const begun = performance.now();
    const folder = { path: dir, ...statSync(dir) };
    expect(await manager.perform({ id: "1", root: "root-1", folder, kind: "run", args: { command: "sleep 9", workdir: null, timeout: 0.3 } }, new AbortController().signal))
      .toEqual({ ok: { output: "Command timed out after 0.3 seconds", returncode: 124, timed_out: true } });
    expect(performance.now() - begun).toBeLessThan(1_000);
    await until(() => cancelled);
    await manager.stop();
  });
});

describe("this computer's sleep", () => {
  it("is told the guest at the next look, from this computer's own two clocks, with how long it slept", async () => {
    const asked: Array<{ type: string; slept?: number }> = [];
    const listening: BootVm = async (...args) => {
      const vm = await fakeVm({ uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) })(...args);
      createInterface({ input: agent as Duplex }).on("line", (line) => asked.push(JSON.parse(line) as { type: string; slept?: number }));
      return vm;
    };
    const guest = await Guest.boot(listening, { ...options(), pingMs: 100 });
    // The wall clock moves on 90 s, the monotonic one does not: as across a sleep.
    const now = Date.now.bind(Date);
    const spied = vi.spyOn(Date, "now").mockImplementation(() => now() + 90_000);
    try {
      await until(() => asked.some((message) => message.type === "time"));
    } finally {
      spied.mockRestore();
    }
    expect(asked.find((message) => message.type === "time")?.slept).toBeGreaterThan(89_000);
    await guest.stop();
  });
});

describe("a guest frozen while the computer slept", () => {
  // A guest whose agent's answers wait in its port while it is frozen, and come at once when it
  // thaws; *asked* gets each time request, and *gone* its VM's end.
  const freezable = (frozen: { now: boolean; held: string[] }, asked: unknown[], gone: string[]): BootVm => async (...args) => {
    const vm = await fakeVm({ uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) })(...args);
    void vm.exited.then(() => gone.push("gone"));
    const written = (agent as Duplex).write.bind(agent);
    (agent as Duplex).write = ((line: string) => {
      if (frozen.now) frozen.held.push(line);
      else written(line);
      return true;
    }) as Duplex["write"];
    createInterface({ input: agent as Duplex }).on("line", (line) => {
      const message = JSON.parse(line) as { type: string };
      if (message.type === "time") asked.push(message);
    });
    return vm;
  };

  for (const resumed of [true, false]) {
    it(resumed ? "is kept at its wake, the pings it missed asleep not held against it, and told the time" : "is lost past three missed pings when nothing says the computer slept", async () => {
      const frozen = { now: false, held: [] as string[] };
      const asked: unknown[] = [];
      const gone: string[] = [];
      const manager = new VmManager({ ...options(), pingMs: 200 }, freezable(frozen, asked, gone));
      const folder = { path: dir, ...statSync(dir) };
      expect(await manager.perform({ id: "1", root: "root-1", folder, kind: "which", args: {} }, new AbortController().signal)).toEqual({ ok: true });
      frozen.now = true;
      await new Promise((resolve) => setTimeout(resolve, 500));
      if (resumed) manager.resume();
      await new Promise((resolve) => setTimeout(resolve, 300));
      frozen.now = false;
      for (const line of frozen.held.splice(0)) (agent as Duplex).write(line);
      await new Promise((resolve) => setTimeout(resolve, 400));
      expect(gone).toEqual(resumed ? [] : ["gone"]);
      if (resumed) expect(asked).toEqual([expect.objectContaining({ type: "time", now: expect.closeTo(Date.now(), -4) })]);
      await manager.stop();
    });
  }
});

describe("the emulated VM", () => {
  it("chooses KVM when this user can open it, and says why the guest runs emulated when it cannot", () => {
    const kvm = join(dir, "kvm");
    expect(emulation(join(dir, "missing"))).toBe("no-kvm");
    expect(emulation(kvm)).toBeNull();
    // A device this user may not open, as one not in its group finds it.
    chmodSync(kvm, 0o000);
    const groups = join(dir, "group");
    writeFileSync(groups, "kvm:x:4242:someone\n");
    expect(emulation(kvm, groups)).toBe("no-access");
    // In its group, though not in this login's groups: the install script added them.
    writeFileSync(groups, `adm:x:4:\nkvm:x:4242:someone,${userInfo().username}\n`);
    expect(emulation(kvm, groups)).toBe("relogin");
    expect(emulation(kvm, join(dir, "no-group-file"))).toBe("no-access");
  });

  it("boots emulated when QEMU cannot use the KVM it could open, on the same disks", async () => {
    const fake = join(dir, "qemu.cjs");
    writeFileSync(fake, [
      'const net = require("node:net");',
      "const run = process.argv[2];",
      'require("node:fs").writeFileSync(run + "/../argv", JSON.stringify(process.argv.slice(3)));',
      'for (const name of ["control", "net"]) net.createServer(() => {}).listen(run + "/" + name + ".sock");',
      "net.createServer((socket) => {",
      '  socket.write(\'{"QMP": {"version": {}, "capabilities": []}}\\n\');',
      '  socket.once("data", () => socket.write(\'{"return": {}}\\n\'));',
      '}).listen(run + "/qmp.sock");',
    ].join("\n"));
    const script = [
      'case "$*" in *accel=kvm*) echo "qemu-system-x86_64: failed to initialize kvm: Device or resource busy" >&2; exit 1;; esac',
      `exec '${process.execPath}' '${fake}' '${join(dir, "run")}' "$@"`,
    ].join("\n");
    await withQemu(script, async () => {
      const vm = await bootLinux(options(), undefined, performance.now() + 5_000);
      try {
        expect(vm.emulated).toBe("kvm-failed");
        const argv = JSON.parse(readFileSync(join(dir, "argv"), "utf8")) as string[];
        expect(argv).toEqual(expect.arrayContaining(["-accel", "tcg,thread=multi,tb-size=256", "-cpu", "max", "if=none,id=root,file=/i/rootfs.img,format=raw,readonly=on"]));
        expect(argv[argv.indexOf("-append") + 1]).toMatch(/ surogate\.emulated=1$/);
      } finally {
        await vm.kill();
      }
    });
  });

  it("waits as Section 11's table says, with KVM and emulated", () => {
    expect(WAITS).toEqual({
      kvm: { helloMs: 15_000, missed: 3, powerOffMs: 5_000, setupMs: 15_000, shareMs: 15_000 },
      emulated: { helloMs: 120_000, missed: 9, powerOffMs: 30_000, setupMs: 90_000, shareMs: 90_000 },
    });
  });

  it("gives an emulated guest nine missed pings, where one with KVM is lost at its third", async () => {
    const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) };
    const gone: Record<string, number> = {};
    const begun = performance.now();
    for (const emulated of [null, "no-kvm"] as const) {
      const guest = await Guest.boot(fakeVm(roots, true, emulated), { ...options(), pingMs: 100 });
      // From now on its agent answers nothing: it hangs.
      (agent as Duplex).write = (() => true) as Duplex["write"];
      void guest.gone.then(() => {
        gone[String(emulated)] = performance.now() - begun;
      });
    }
    await new Promise((resolve) => setTimeout(resolve, 700));
    expect(Object.keys(gone)).toEqual(["null"]);
    await until(() => "no-kvm" in gone, 2_000);
    expect(gone["no-kvm"]).toBeGreaterThan(900);
  });

  it("tells the agent once a chat, after its first run's output, that the chat's commands run emulated", async () => {
    const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: { output: "hi\n", returncode: 0, timed_out: false } }) };
    const folder = { path: dir, ...statSync(dir) };
    const run = (manager: VmManager, root: string) => manager.perform({ id: root, root, folder, kind: "run", args: { command: "echo hi", workdir: null, timeout: 30 } }, new AbortController().signal);
    const emulated = new VmManager(options(), fakeVm(roots, true, "no-kvm"));
    const told = { ok: { output: `hi\n\n${EMULATED_NOTICE}`, returncode: 0, timed_out: false } };
    const plain = { ok: { output: "hi\n", returncode: 0, timed_out: false } };
    expect(await run(emulated, "root-1")).toEqual(told);
    expect(await run(emulated, "root-1")).toEqual(plain);
    expect(await run(emulated, "root-2")).toEqual(told);
    await emulated.stop();
    const kvm = new VmManager(options(), fakeVm(roots));
    expect(await run(kvm, "root-1")).toEqual(plain);
    await kvm.stop();
    expect(EMULATED_NOTICE).toBe("This computer runs commands in an emulated sandbox, about 5 to 20 times slower than usual. Give long commands more time.");
  });

  it("tells each boot: with KVM, emulated and why, or why it did not start", async () => {
    const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) };
    const folder = { path: dir, ...statSync(dir) };
    const which = (manager: VmManager) => manager.perform({ id: "1", root: "root-1", folder, kind: "which", args: {} }, new AbortController().signal);
    for (const emulated of [null, "relogin"] as const) {
      const told: Boot[] = [];
      const manager = new VmManager(options(), fakeVm(roots, true, emulated), undefined, undefined, (boot) => told.push(boot));
      expect(await which(manager)).toEqual({ ok: true });
      expect(told).toEqual([{ emulated }]);
      await manager.stop();
    }
    const told: Boot[] = [];
    const failing: BootVm = async () => {
      throw new Error("QEMU exited: qemu-system-x86_64: -drive if=none,id=root: Could not open '/i/rootfs.img': No such file or directory");
    };
    const manager = new VmManager(options(), failing, undefined, undefined, (boot) => told.push(boot));
    expect(await which(manager)).toEqual(unavailable("did not start: QEMU exited: qemu-system-x86_64: -drive if=none,id=root: Could not open '/i/rootfs.img': No such file or directory"));
    expect(told).toEqual([{ failed: "QEMU exited: qemu-system-x86_64: -drive if=none,id=root: Could not open '/i/rootfs.img': No such file or directory" }]);
    await manager.stop();
  });
});
