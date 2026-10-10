import { spawn } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir, userInfo } from "node:os";
import { join } from "node:path";

import { createInterface } from "node:readline";
import { type ClientHttp2Session, connect as connectH2 } from "node:http2";
import { connect, createServer, type Socket } from "node:net";
import { type Duplex, duplexPair } from "node:stream";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { CANCELLED, SANDBOX_STOPPED } from "../src/guest/command.js";
import { Control, type ControlRoots } from "../src/guest/control.js";
import { Inbound } from "../src/guest/inbound.js";
import { FOLDER_UNAVAILABLE } from "../src/hosts/messages.js";
import { Carrier, DOOR, Door, Forwarded } from "../src/vm/inbound.js";
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
// What the latest fake VM's agent answers the host's streams on its inbound port with: a connection, or why none.
// Unset, its agent takes no stream there.
let reach: ((root: string, port: number, first: 4 | 6) => Promise<Socket | string>) | undefined;

// A VM whose control port reaches the guest's own Control on *roots*, with no QEMU:
// what the agent is asked, and when. Without roots, an agent that never says hello.
// *powers*: its agent powers the fake VM off at a shutdown, as the guest's ends QEMU.
// *emulated*: why it runs emulated, or null with KVM.
const fakeVm = (roots?: ControlRoots, powers = true, emulated: Emulated | null = null): BootVm => async () => {
  const [host, guest] = duplexPair();
  agent = guest;
  const [net, guestNet] = duplexPair();
  agentNet = guestNet;
  const [inbound, guestInbound] = duplexPair();
  const reaching = reach;
  if (reaching) new Inbound(guestInbound, reaching);
  let gone = (_said: string) => {};
  const exited = new Promise<string>((resolve) => {
    gone = resolve;
  });
  const kill = async () => {
    host.destroy();
    net.destroy();
    inbound.destroy();
    gone("");
  };
  if (roots) {
    const machine = powers ? { setClock: async () => {}, woke: () => {}, heard: () => {}, powerOff: kill } : undefined;
    const control = new Control((message) => void guest.write(`${JSON.stringify(message)}\n`), roots, machine);
    createInterface({ input: guest }).on("line", (line) => control.receive(line));
    control.hello();
  }
  return { control: host, net, inbound, exited, emulated, share: async () => ({ kind: "virtiofs", tag: "r1" }), unshare: async () => {}, kill };
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
      'net.createServer(() => {}).listen(run + "/inbound.sock");',
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
      'net.createServer(() => {}).listen(run + "/inbound.sock");',
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

  it("looks for the VM's tools where the sandbox's are looked for: on no relative entry of the PATH, and in no folder a chat is bound to, where a command may have written a program", async () => {
    const tool = (bin: string, name: string, script: string) => {
      mkdirSync(bin, { recursive: true });
      writeFileSync(join(bin, name), `#!/bin/sh\n${script}\n`);
      chmodSync(join(bin, name), 0o755);
      return join(bin, name);
    };
    const [bound, system] = [join(dir, "bound"), join(dir, "system")];
    const ran = join(dir, "ran");
    const virtiofsd = tool(system, "virtiofsd", "echo 'virtiofsd 1.13.2'");
    for (const name of ["newuidmap", "newgidmap"]) tool(join(bound, "bin"), name, "true");
    tool(join(bound, "bin"), "qemu-system-x86_64", `echo bound >>${ran}; echo 'QEMU emulator version 10.1.0'`);
    tool(join(bound, "node_modules", ".bin"), "qemu-system-x86_64", `echo relative >>${ran}; echo 'QEMU emulator version 10.1.0'`);
    const [path, cwd] = [process.env.PATH, process.cwd()];
    process.chdir(bound);
    try {
      // The only QEMU and id-map tools are in the chat's folder, by a whole entry and by a relative one: none is found, and none is run.
      process.env.PATH = `node_modules/.bin:${join(bound, "bin")}:${system}`;
      expect(await missingTools({ virtiofsd }, false, [statSync(bound)])).toEqual(["QEMU 8.2 or later", "newuidmap and newgidmap"]);
      expect(existsSync(ran)).toBe(false);
      // Those of the system, behind them on the PATH, are the ones found.
      tool(system, "qemu-system-x86_64", `echo system >>${ran}; echo 'QEMU emulator version 10.1.0'`);
      for (const name of ["newuidmap", "newgidmap"]) tool(system, name, "true");
      expect(await missingTools({ virtiofsd }, false, [statSync(bound)])).toEqual([]);
      expect(readFileSync(ran, "utf8")).toBe("system\n");
    } finally {
      process.env.PATH = path;
      process.chdir(cwd);
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

describe("whether a chat's own server listens, asked through the guest's inbound port", () => {
  const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) };
  const run = (manager: VmManager, root = "root-1") => manager.perform({
    id: `run-${Math.random()}`, root, folder: { path: dir, ...statSync(dir) }, kind: "run", args: {},
  }, new AbortController().signal);
  // Each root and port the fake guest's agent was asked to reach, and the server here its roots' loopback stands for:
  // each connection it took, and what each sent before it ended.
  let reached: Array<[string, number]>;
  let server: ReturnType<typeof createServer>;
  let taken: Socket[];
  let heard: string[];
  let port: number;
  const dial = () => new Promise<Socket>((done) => {
    const socket = connect({ host: "127.0.0.1", port, allowHalfOpen: true });
    socket.once("connect", () => done(socket));
  });
  const gone = () => vi.waitFor(() => expect(taken.every((socket) => socket.destroyed)).toBe(true));

  beforeEach(async () => {
    reached = [];
    taken = [];
    heard = [];
    server = createServer((socket) => {
      const at = taken.push(socket) - 1;
      heard[at] = "";
      socket.on("error", () => {});
      socket.on("data", (chunk: Buffer) => {
        heard[at] += chunk.toString();
      });
    });
    await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
    port = (server.address() as { port: number }).port;
    // Root 1 listens on 3000, and on its own proxies' ports as every root does; nothing else listens.
    reach = (root, to) => {
      reached.push([root, to]);
      // On 7000 the root has every connection it takes from the browser already.
      if (to === 7000) return Promise.resolve("EMFILE");
      return root === "root-1" && [3000, 3128, 1080].includes(to) ? dial() : Promise.resolve("ECONNREFUSED");
    };
  });

  afterEach(async () => {
    reach = undefined;
    for (const socket of taken) socket.destroy();
    await new Promise<void>((done) => server.close(() => done()));
  });

  it("says whether something in a root listens on a port, by a connection made and let go", async () => {
    const manager = new VmManager(options(), fakeVm(roots));
    await run(manager);
    await run(manager, "root-2");
    expect([await manager.listening("root-1", 3000), await manager.listening("root-1", 8000), await manager.listening("root-2", 3000)]).toEqual([true, false, false]);
    expect(reached).toEqual([["root-1", 3000], ["root-1", 8000], ["root-2", 3000]]);
    // The root's server saw one connection, which said nothing and is gone.
    await gone();
    expect(heard).toEqual([""]);
    // The root's own proxies for its commands are no server of the chat's, though they listen: its runner is asked for neither.
    reached = [];
    expect([await manager.listening("root-1", 3128), await manager.listening("root-1", 1080), reached]).toEqual([false, false, []]);
    // A root that takes no more connections could not be asked: that is said, and is not that nothing listens.
    expect(await manager.listening("root-1", 7000)).toBe("busy");
    await manager.stop();
    expect(await manager.listening("root-1", 3000)).toBe(false);
  });

  it("asks nothing of a root that is not set up in its guest, and boots no guest for the question", async () => {
    let boots = 0;
    const boot = fakeVm(roots);
    const manager = new VmManager(options(), (...args) => (boots += 1, boot(...args)));
    expect(await manager.listening("root-1", 3000)).toBe(false);
    expect(boots).toBe(0);
    // Every root listens on 3000 here. Root 2 keeps the guest: root 1, torn down, is in it no more.
    reach = (root, to) => (reached.push([root, to]), to === 3000 ? dial() : Promise.resolve("ECONNREFUSED"));
    await run(manager);
    await run(manager, "root-2");
    await manager.teardown("root-1");
    expect(await manager.listening("root-1", 3000)).toBe(false);
    // Nor one the guest never heard of, whatever it is called.
    expect(await manager.listening("root-3", 3000)).toBe(false);
    expect([reached, boots]).toEqual([[], 1]);
    // Nor one whose runner the guest lost, until an operation of the chat's sets it up again.
    expect(await manager.listening("root-2", 3000)).toBe(true);
    agent?.write(`${JSON.stringify({ type: "lost", root: "root-2" })}\n`);
    await vi.waitFor(async () => expect(await manager.listening("root-2", 3000)).toBe(false));
    const before = reached.length;
    expect([await manager.listening("root-2", 3000), reached.length]).toEqual([false, before]);
    await run(manager, "root-2");
    expect([await manager.listening("root-2", 3000), boots]).toEqual([true, 1]);
    await manager.stop();
  });

  it("takes what the guest answers as data, and gives up on an agent that does not answer, whose connection is let go when it comes", async () => {
    // An agent that answers the first stream with a reason of its own making, the second with a status of its own,
    // and the third only once the host has given it up.
    let asked = 0;
    const late = Promise.withResolvers<void>();
    reach = async () => {
      asked += 1;
      if (asked === 1) return "403 denied\r\nx";
      if (asked === 2) throw new Error("broken");
      await late.promise;
      return dial();
    };
    const manager = new VmManager({ ...options(), reachMs: 200 }, fakeVm(roots));
    await run(manager);
    expect([await manager.listening("root-1", 3000), await manager.listening("root-1", 3000)]).toEqual([false, false]);
    const begun = performance.now();
    expect(await manager.listening("root-1", 3000)).toBe(false);
    expect(performance.now() - begun).toBeGreaterThanOrEqual(190);
    late.resolve();
    await vi.waitFor(() => expect(taken).toHaveLength(1));
    await gone();
    await manager.stop();
  });

  it("passes on why the guest took no connection only when it is a reason its agent gives, and that it did not answer in time or is gone", async () => {
    const [ours, guests] = duplexPair();
    const answers: Array<Socket | string> = ["ECONNREFUSED", "sandbox", "EMFILE", "403 denied\r\nx", "econnrefused", "A".repeat(17), ""];
    const stalled = new Promise<string>(() => {});
    new Inbound(guests, () => Promise.resolve(answers.shift() ?? stalled));
    const carrier = new Carrier(ours, 200);
    const said: string[] = [];
    for (let n = answers.length; n > 0; n -= 1) said.push(String(await carrier.open("root-1", 3000)));
    expect(said).toEqual(["ECONNREFUSED", "sandbox", "EMFILE", "unreachable", "unreachable", "unreachable", "unreachable"]);
    // A root that is none, or a port that is none, is the agent's to refuse: asked as they are, and answered in its words.
    expect([await carrier.open("../root", 3000), await carrier.open("root-1", 0)]).toEqual(["unreachable", "unreachable"]);
    expect(await carrier.open("root-1", 3000)).toBe("ETIMEDOUT");
    carrier.close();
    expect(await carrier.open("root-1", 3000)).toBe("sandbox");
  });

  it("answers that nothing listens when the guest goes while it is asked", async () => {
    const answering = Promise.withResolvers<Socket | string>();
    reach = (root, to) => (reached.push([root, to]), answering.promise);
    const manager = new VmManager(options(), fakeVm(roots));
    await run(manager);
    const asking = manager.listening("root-1", 3000);
    await vi.waitFor(() => expect(reached).toHaveLength(1));
    await manager.stop();
    expect(await asking).toBe(false);
    answering.resolve("ECONNREFUSED");
  });
});

describe("a chat's own servers, through the manager's door", () => {
  const KEY = "ab".repeat(32);
  const OTHER_KEY = "cd".repeat(32);
  const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) };
  const run = (manager: VmManager, root = "root-1") => manager.perform({
    id: `run-${Math.random()}`, root, folder: { path: dir, ...statSync(dir) }, kind: "run", args: {},
  }, new AbortController().signal);
  const door = () => join(dir, "run", DOOR);
  // A browser's proxy knocking at the manager's door with *line*: the door's answer line, then what comes
  // back for *data*; an errno where there is no door, and "" for a knock dropped unanswered.
  const knocking = (line: string, data = "hello") => new Promise<string>((resolve) => {
    const socket = connect({ path: door(), allowHalfOpen: true });
    let said = "";
    const done = () => {
      socket.destroy();
      resolve(said);
    };
    socket.on("error", (error: NodeJS.ErrnoException) => resolve(String(error.code)));
    socket.on("data", (chunk: Buffer) => {
      said += chunk.toString();
      if (said === "200\n") socket.write(data);
      if (said === `200\nserved ${data}`) done();
    });
    // The door ends what it does not carry, whether its proxy closes it or not.
    socket.on("end", done);
    socket.write(line);
  });
  const knock = (key: string, port: number | string, data?: string) => knocking(`${key} ${port}\n`, data);
  // Each root, port and family the fake guest's agent was asked to reach, and the server here its roots' loopback
  // stands for: each connection it took.
  let reached: Array<[string, number, number]>;
  let server: ReturnType<typeof createServer>;
  let taken: Socket[];
  let port: number;
  const dial = () => new Promise<Socket>((done) => {
    const socket = connect({ host: "127.0.0.1", port, allowHalfOpen: true });
    socket.once("connect", () => done(socket));
  });

  beforeEach(async () => {
    reached = [];
    taken = [];
    server = createServer((socket) => {
      taken.push(socket);
      socket.on("error", () => {});
      socket.on("data", (chunk: Buffer) => socket.write(`served ${chunk.toString()}`));
    });
    await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
    port = (server.address() as { port: number }).port;
    // Root 1 listens on 3000, and on its own proxies' ports as every root does; nothing else listens.
    reach = (root, to, first) => {
      reached.push([root, to, first]);
      return root === "root-1" && [3000, 3128, 1080].includes(to) ? dial() : Promise.resolve("ECONNREFUSED");
    };
  });

  afterEach(async () => {
    reach = undefined;
    for (const socket of taken) socket.destroy();
    await new Promise<void>((done) => server.close(() => done()));
  });

  it("carries a connection that knocks with a device's key to the root its port is forwarded to, by the family its knock names first", async () => {
    const manager = new VmManager(options(), fakeVm(roots));
    manager.forwards(KEY, [[3000, "root-1"], [8000, "root-1"]]);
    // With no guest there is no door, and none is booted for a knock.
    expect(await knock(KEY, 3000)).toBe("ENOENT");
    await run(manager);
    await until(() => existsSync(door()));
    // The door is this user's alone.
    expect(statSync(door()).mode & 0o777).toBe(0o600);
    expect(await knock(KEY, 3000)).toBe("200\nserved hello");
    expect(await knock(KEY, "3000 6")).toBe("200\nserved hello");
    expect(await knock(KEY, 8000)).toBe("502 ECONNREFUSED\n");
    expect(reached).toEqual([["root-1", 3000, 4], ["root-1", 3000, 6], ["root-1", 8000, 4]]);
    // A connection held through the door when its guest stops ends with the guest, and the door goes too.
    const held = connect({ path: door() });
    held.on("error", () => {});
    const ended = new Promise<void>((done) => held.once("close", () => done()));
    held.write(`${KEY} 3000\n`);
    await new Promise((done) => held.once("data", done));
    // So does one that has not knocked yet.
    const silent = connect({ path: door() });
    silent.on("error", () => {});
    const dropped = new Promise<void>((done) => silent.once("close", () => done()));
    await new Promise((done) => silent.once("connect", done));
    const door0 = door();
    const stopped = performance.now();
    await manager.stop();
    await Promise.all([ended, dropped]);
    // At the stop, not when a knock's time is up.
    expect(performance.now() - stopped).toBeLessThan(2_000);
    expect(existsSync(door0)).toBe(false);
    expect(await knock(KEY, 3000)).toBe("ENOENT");
  });

  it("opens the door to no key but a device's own, no port but one forwarded for it, neither of the sandbox's own proxies' ports, and nothing that is not a knock", async () => {
    const manager = new VmManager(options(), fakeVm(roots));
    // A port past the last, forwarded by mistake, is no port either.
    manager.forwards(KEY, [[3000, "root-1"], [3128, "root-1"], [1080, "root-1"], [65_536, "root-1"]]);
    manager.forwards(OTHER_KEY, [[9000, "root-1"]]);
    await run(manager);
    await until(() => existsSync(door()));
    for (const [key, to] of [
      // Another device's key, for this one's port; a key nobody has; this key, for another device's port, or one nobody forwarded.
      [OTHER_KEY, 3000], ["ef".repeat(32), 3000], [KEY, 9000], [KEY, 3001], [KEY, 80],
      // The sandbox's own proxies for its commands, though the app named them.
      [KEY, 3128], [KEY, 1080],
      // What is no knock: a short key, an upper-case one, a port that is none, a family that is none.
      ["ab".repeat(31), 3000], [KEY.toUpperCase(), 3000], [KEY, 0], [KEY, 65_536], [KEY, "3000 "], [KEY, "03000"], [KEY, "3000/"],
      [KEY, "3000 4"], [KEY, "3000 7"], [KEY, "3000  6"], [KEY, "3000 6 "], [KEY, " 3000"],
    ] as const) {
      expect(await knock(key, to), `${key} ${to}`).toBe("403 refused\n");
    }
    // A line that never ends, and bytes sent before the door answers, are dropped unheard: one past a knock's size at once, not when its time is up.
    const begun = performance.now();
    expect(await knocking("x".repeat(300))).toBe("");
    expect(performance.now() - begun).toBeLessThan(2_000);
    expect(await knocking(`${KEY} 3000\nGET / HTTP/1.1\r\n\r\n`)).toBe("");
    expect(reached).toEqual([]);
    await manager.stop();
  });

  it("gives a knock that does not come whole its time and no more", async () => {
    const forwarded = new Forwarded();
    forwarded.set(KEY, [[3000, "root-1"]]);
    mkdirSync(join(dir, "run"));
    const slow = new Door(door(), forwarded, () => Promise.resolve("ECONNREFUSED"), { knockMs: 150 });
    await until(() => existsSync(door()));
    const begun = performance.now();
    expect(await knocking(`${KEY} 30`)).toBe("");
    expect(performance.now() - begun).toBeGreaterThanOrEqual(140);
    slow.close();
    expect(existsSync(door())).toBe(false);
  });

  it("forwards a port to the root told last, and to none once its device has none left", async () => {
    const manager = new VmManager(options(), fakeVm(roots));
    manager.forwards(KEY, [[3000, "root-2"]]);
    await run(manager);
    await run(manager, "root-2");
    await until(() => existsSync(door()));
    expect(await knock(KEY, 3000)).toBe("502 ECONNREFUSED\n");
    manager.forwards(KEY, [[3000, "root-1"]]);
    expect(await knock(KEY, 3000)).toBe("200\nserved hello");
    manager.forwards(KEY, []);
    expect(await knock(KEY, 3000)).toBe("403 refused\n");
    expect(reached).toEqual([["root-2", 3000, 4], ["root-1", 3000, 4]]);
    await manager.stop();
  });

  it("reaches into no root that is not set up in its guest, and takes what the guest answers as data", async () => {
    const manager = new VmManager({ ...options(), reachMs: 200 }, fakeVm(roots));
    manager.forwards(KEY, [[3000, "root-1"], [4000, "root-2"]]);
    // Root 2 keeps the guest: root 1, torn down, is in it no more.
    await run(manager);
    await run(manager, "root-2");
    await until(() => existsSync(door()));
    await manager.teardown("root-1");
    expect(await knock(KEY, 3000)).toBe("502 sandbox\n");
    expect(reached).toEqual([]);
    expect(await knock(KEY, 4000)).toBe("502 ECONNREFUSED\n");
    // An agent that answers one stream with a reason of its own making, and the next not at all.
    let asked = 0;
    reach = () => ((asked += 1) === 1 ? Promise.resolve("403 denied\r\nx") : new Promise(() => {}));
    await manager.stop();
    const again = new VmManager({ ...options(), reachMs: 200 }, fakeVm(roots));
    again.forwards(KEY, [[3000, "root-1"]]);
    await run(again);
    await until(() => existsSync(door()));
    expect(await knock(KEY, 3000)).toBe("502 unreachable\n");
    expect(await knock(KEY, 3000)).toBe("502 ETIMEDOUT\n");
    await again.stop();
  });

  it("lets go of a connection its browser lets go: one left partway through the answer, and one left before the guest answered", async () => {
    const answering = Promise.withResolvers<void>();
    let slow = false;
    reach = async (root, to, first) => {
      reached.push([root, to, first]);
      if (slow) await answering.promise;
      return dial();
    };
    const manager = new VmManager(options(), fakeVm(roots));
    manager.forwards(KEY, [[3000, "root-1"]]);
    await run(manager);
    await until(() => existsSync(door()));
    // Held open, with the server's answer still to come.
    const held = connect({ path: door() });
    held.on("error", () => {});
    held.write(`${KEY} 3000\n`);
    await vi.waitFor(() => expect(taken).toHaveLength(1));
    held.destroy();
    await vi.waitFor(() => expect(taken[0]?.destroyed).toBe(true));
    // Gone while the guest's agent still dials: the connection is let go when it comes.
    slow = true;
    const early = connect({ path: door() });
    early.on("error", () => {});
    early.write(`${KEY} 3000\n`);
    await vi.waitFor(() => expect(reached).toHaveLength(2));
    early.destroy();
    await new Promise((done) => early.once("close", done));
    answering.resolve();
    await vi.waitFor(() => expect(taken).toHaveLength(2));
    await vi.waitFor(() => expect(taken[1]?.destroyed).toBe(true));
    await manager.stop();
  });

  it("gives the browser the whole of an answer whose server ended it, however slowly the browser reads", async () => {
    // A root's server that sends 8 MiB and ends, which the guest's side of the door gets at once.
    const whole = Buffer.alloc(8 << 20, 97);
    const forwarded = new Forwarded();
    forwarded.set(KEY, [[3000, "root-1"]]);
    mkdirSync(join(dir, "run"));
    const own = new Door(door(), forwarded, async () => {
      const [ours, theirs] = duplexPair();
      theirs.end(whole);
      theirs.resume();
      // Gone as soon as it has ended, as the guest's stream is once the root's server has closed.
      ours.once("end", () => ours.destroy());
      return ours;
    });
    await until(() => existsSync(door()));
    const got = await new Promise<number>((resolve) => {
      const socket = connect({ path: door() });
      let bytes = -4;
      socket.on("error", () => {});
      // Read a little, stop for a while, then the rest.
      socket.once("data", (chunk: Buffer) => {
        bytes += chunk.length;
        socket.pause();
        setTimeout(() => socket.on("data", (more: Buffer) => (bytes += more.length)).resume(), 300);
      });
      socket.on("close", () => resolve(bytes));
      socket.write(`${KEY} 3000\n`);
    });
    expect(got).toBe(whole.length);
    own.close();
  });

  // A door of its own with small bounds, into roots whose every connection is held open: each connection's ends
  // towards the guest, by the order it was made, what the far one heard, and which were let go when each was asked for.
  const bounded = async (bounds: { perRoot?: number; perKey?: number; open?: number }) => {
    const forwarded = new Forwarded();
    forwarded.set(KEY, [[3000, "root-1"], [3001, "root-1"], [4000, "root-2"]]);
    forwarded.set(OTHER_KEY, [[5000, "root-3"]]);
    const far: Duplex[] = [];
    const near: Duplex[] = [];
    const heard: string[] = [];
    const gone: boolean[][] = [];
    mkdirSync(join(dir, "run"));
    const own = new Door(door(), forwarded, async (root, to) => {
      reached.push([root, to, 4]);
      gone.push(near.map((end) => end.destroyed));
      const [ours, theirs] = duplexPair();
      const at = far.push(theirs) - 1;
      heard[at] = "";
      theirs.on("data", (chunk: Buffer) => {
        heard[at] += chunk.toString();
      });
      near.push(ours);
      return ours;
    }, bounds);
    await until(() => existsSync(door()));
    // A connection held open through the door: what the door answered, once it has, and whether it is still open.
    const hold = async (key: string, to: number) => {
      const socket = connect({ path: door() });
      const held = { socket, said: "", open: true };
      socket.on("error", () => {});
      socket.on("data", (chunk: Buffer) => {
        held.said += chunk.toString();
      });
      socket.on("close", () => {
        held.open = false;
      });
      socket.write(`${key} ${to}\n`);
      await vi.waitFor(() => expect(held.said !== "" || !held.open).toBe(true));
      return held;
    };
    return { own, far, near, heard, gone, hold };
  };

  it("ends the connection idle longest to admit a new one at a root's bound, a byte either way counted, and none while there is room", async () => {
    const { own, far, near, heard, gone, hold } = await bounded({ perRoot: 3 });
    const first = await hold(KEY, 3000);
    const second = await hold(KEY, 3001);
    // With two of three held, nothing is ended for the third.
    const third = await hold(KEY, 3000);
    expect([first, second, third].map(({ said, open }) => [said, open])).toEqual([["200\n", true], ["200\n", true], ["200\n", true]]);
    // A byte either way counts: the first hears from the browser alone, the second from its server alone; the third carries none.
    first.socket.write("x");
    far[1]?.write("y");
    await vi.waitFor(() => expect([heard[0], second.said]).toEqual(["x", "200\ny"]));
    const fourth = await hold(KEY, 3000);
    await vi.waitFor(() => expect(third.open).toBe(false));
    expect([first.open, second.open, fourth.said, fourth.open]).toEqual([true, true, "200\n", true]);
    // Let go towards the guest before the new one is asked for there, so that the guest has room for it: that one, and no other.
    expect(gone[3]).toEqual([false, false, true]);
    expect(near.map((end) => end.destroyed)).toEqual([false, false, true, false]);
    // And again: the server's byte is older than the browser's now, so the second goes.
    first.socket.write("x");
    fourth.socket.write("z");
    await vi.waitFor(() => expect([heard[0], heard[3]]).toEqual(["xx", "z"]));
    const fifth = await hold(KEY, 3001);
    await vi.waitFor(() => expect(second.open).toBe(false));
    expect([first.open, fourth.open, fifth.open, reached.length]).toEqual([true, true, true, 5]);
    own.close();
    await vi.waitFor(() => expect([first.open, fourth.open, fifth.open]).toEqual([false, false, false]));
  });

  it("keeps one chat's bound from another's, and a device's from another device's: past a device's bound a knock is answered busy, and nothing is ended", async () => {
    const { own, hold } = await bounded({ perRoot: 2, perKey: 3 });
    const held = [await hold(KEY, 3000), await hold(KEY, 3001)];
    // The first chat is at its bound: another chat's port still opens, and ends none of the first's.
    held.push(await hold(KEY, 4000));
    expect(held.map(({ said, open }) => [said, open])).toEqual([["200\n", true], ["200\n", true], ["200\n", true]]);
    // The device is at its own now: its next knock is refused, for either chat, and nothing is asked of the guest for it.
    const before = reached.length;
    for (const to of [4000, 3000]) {
      const refused = await hold(KEY, to);
      await vi.waitFor(() => expect(refused.open).toBe(false));
      expect(refused.said).toBe("502 busy\n");
    }
    expect([held.map(({ open }) => open), reached.length]).toEqual([[true, true, true], before]);
    // Another device's browser has its own.
    expect((await hold(OTHER_KEY, 5000)).said).toBe("200\n");
    // One that ends gives its place back.
    held[2]?.socket.destroy();
    await vi.waitFor(async () => expect((await hold(KEY, 4000)).said).toBe("200\n"));
    own.close();
  });

  it("takes no more connections at once than its own bound, those that have not knocked among them", async () => {
    const { own, hold } = await bounded({ open: 3 });
    const silent = [connect({ path: door() }), connect({ path: door() })];
    for (const socket of silent) socket.on("error", () => {});
    await Promise.all(silent.map((socket) => new Promise((done) => socket.once("connect", done))));
    expect((await hold(KEY, 3000)).said).toBe("200\n");
    const past = await hold(KEY, 3000);
    expect([past.said, past.open]).toEqual(["", false]);
    for (const socket of silent) socket.destroy();
    await vi.waitFor(async () => expect((await hold(KEY, 3000)).said).toBe("200\n"));
    own.close();
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
    // The user's Retry, its image checked: the next operation boots at once, not after the backoff's second.
    manager.retry();
    const begun = performance.now();
    expect(await which(manager)).toEqual(failed);
    expect(boots).toBe(2);
    expect(performance.now() - begun).toBeLessThan(500);
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
      'for (const name of ["control", "net", "inbound"]) net.createServer(() => {}).listen(run + "/" + name + ".sock");',
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
      // The agent's answer to a request to a folder's history: twenty minutes and a quarter, past the agent's own ten for its wait and ten for its run.
      kvm: { helloMs: 15_000, missed: 3, powerOffMs: 5_000, setupMs: 15_000, shareMs: 15_000, historyMs: 1_215_000 },
      emulated: { helloMs: 120_000, missed: 9, powerOffMs: 30_000, setupMs: 90_000, shareMs: 90_000, historyMs: 7_290_000 },
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
