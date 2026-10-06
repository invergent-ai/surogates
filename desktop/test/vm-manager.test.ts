import { spawn } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { bootLinux, sweep } from "../src/vm/linux.js";
import { bootFor, VmManager, type VmOptions } from "../src/vm/manager.js";
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

  it("ends a QEMU that opens no sockets by the boot's deadline", async () => {
    await withQemu(`echo $$ > '${join(dir, "qemu-pid")}'\nexec sleep 30`, async () => {
      await expect(bootLinux(options(), undefined, performance.now() + 500)).rejects.toThrow("QEMU did not open its sockets");
      expect(alive(Number(readFileSync(join(dir, "qemu-pid"), "utf8")))).toBe(false);
    });
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
