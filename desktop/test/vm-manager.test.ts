import { spawn, spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createInterface } from "node:readline";
import { duplexPair } from "node:stream";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { SANDBOX_STOPPED } from "../src/guest/command.js";
import { Control, type ControlRoots } from "../src/guest/control.js";
import { FOLDER_UNAVAILABLE } from "../src/hosts/messages.js";
import { bootLinux, sweep } from "../src/vm/linux.js";
import { type BootVm, bootFor, type Folder, Guest, type VmBackend, VmManager, type VmOptions } from "../src/vm/manager.js";
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

// A VM whose control port reaches the guest's own Control on *roots*, with no QEMU:
// what the agent is asked, and when. Without roots, an agent that never says hello.
const fakeVm = (roots?: ControlRoots): BootVm => async () => {
  const [host, guest] = duplexPair();
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
    exited,
    share: async () => ({ kind: "virtiofs", tag: "r1" }),
    kill: async () => {
      host.destroy();
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
    // A QEMU with its two sockets, whose monitor greets and takes its capabilities, then answers nothing.
    const fake = join(dir, "qemu.cjs");
    writeFileSync(fake, [
      'const net = require("node:net");',
      "const run = process.argv[2];",
      'net.createServer(() => {}).listen(run + "/control.sock");',
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
    const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) };
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
