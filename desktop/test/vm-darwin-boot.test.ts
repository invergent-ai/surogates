// The macOS VM backend (src/vm/darwin.ts) against a stand-in for surogate-vm (test/vm/darwin/fake-vm.mjs),
// with the VM manager's guest above it: what it does with the program it drives, on any OS, as the
// real one does on a Mac, which boots the guest besides. It starts the stand-in as the app starts
// surogate-vm, through spawnClean; what starts nothing is vm-darwin.test.ts's.

import { type ChildProcess, spawn } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { bootDarwin, requirementsDarwin, runtimeFolder } from "../src/vm/darwin.js";
import { Guest, type VmBackend, type VmOptions } from "../src/vm/manager.js";
import { built } from "./built.js";

const FAKE = fileURLToPath(new URL("./vm/darwin/fake-vm.mjs", import.meta.url));
const DARWIN = fileURLToPath(new URL("../dist/vm/darwin.js", import.meta.url));
const ANA = { uid: 501, gid: 20, name: "ana", home: "/Users/ana" };
const ROOT = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";

let dir: string;
let options: VmOptions;
const booted: VmBackend[] = [];

// The stand-in as surogate-vm, at a path of its own, told *knobs* (fake-vm.mjs) by its environment: the backend gives it PATH alone.
function program(knobs: Record<string, string> = {}): string {
  const path = join(dir, `surogate-vm-${Object.keys(knobs).length}-${Math.random().toString(16).slice(2, 8)}`);
  const set = Object.entries(knobs).map(([name, value]) => `export ${name}='${value}'\n`).join("");
  writeFileSync(path, `#!/bin/sh\n${set}exec '${process.execPath}' '${FAKE}' "$@"\n`);
  chmodSync(path, 0o755);
  return path;
}

const boot = async (knobs: Record<string, string> = {}, extra: Partial<VmOptions> = {}) => {
  const vm = await bootDarwin({ ...options, program: program(knobs), ...extra }, undefined, performance.now() + 10_000);
  booted.push(vm);
  return vm;
};

const alive = (pid: number) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};
async function until(check: () => boolean, ms = 5_000): Promise<void> {
  for (const end = Date.now() + ms; !check(); await new Promise((resolve) => setTimeout(resolve, 20))) {
    if (Date.now() > end) throw new Error("timed out");
  }
}
const pidOf = () => Number(readFileSync(join(options.run, "vm.pid"), "utf8"));

beforeEach(() => {
  built(fileURLToPath(new URL("../dist/guest/control.js", import.meta.url)));
  // A short name: the VM's sockets go in the runtime folder under it, and a Mac's temp folder takes 57 of a socket path's 103
  // bytes before it (/private/var/folders/<2>/<28>/T/).
  dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-")));
  for (const folder of ["data/vm", "data/logs", "image", "app", "work"]) mkdirSync(join(dir, folder), { recursive: true });
  for (const file of ["image/vmlinuz", "image/rootfs.img", "app/agent.img"]) writeFileSync(join(dir, file), "");
  options = {
    kernel: join(dir, "image", "vmlinuz"), rootfs: join(dir, "image", "rootfs.img"), agentDisk: join(dir, "app", "agent.img"),
    sessions: join(dir, "data", "vm", "sessions.img"), console: join(dir, "data", "logs", "vm-console.log"),
    run: runtimeFolder(join(dir, "data"), dir), user: ANA,
  };
});

afterEach(async () => {
  await Promise.all(booted.splice(0).map((vm) => vm.kill()));
  rmSync(dir, { recursive: true, force: true });
});

describe("the macOS VM backend", () => {
  it("boots once its three sockets are connected and it said started, its boot line naming the files, the machine and every OS's kernel command line", async () => {
    const log = join(dir, "said.log");
    const vm = await boot({ FAKE_VM_LOG: log });
    // Each root has a uid of its own, as on Linux: the backend names none for every root.
    expect([vm.emulated, "rootUid" in vm]).toEqual([null, false]);
    const [line] = readFileSync(log, "utf8").trim().split("\n").map((text) => JSON.parse(text) as Record<string, unknown>);
    expect(line).toEqual({
      type: "boot", kernel: options.kernel, rootfs: options.rootfs, agentDisk: options.agentDisk, sessions: options.sessions, console: options.console,
      cpus: expect.any(Number), memoryMiB: 2048, commandLine: `root=/dev/vda rootfstype=ext4 ro init=/usr/sbin/surogate-init console=hvc0 panic=-1 quiet surogate.disks=virtio surogate.transport=virtio-serial surogate.names=posix lsm=landlock,lockdown,yama,integrity,apparmor,bpf`, layout: "multiple",
    });
    // The sessions disk made, sparse and the user's alone; the runtime folder theirs alone too.
    expect([statSync(options.sessions).size, statSync(options.sessions).mode & 0o777, statSync(options.run).mode & 0o777]).toEqual([32 * 1024 ** 3, 0o600, 0o700]);
  });

  it("rejects a boot that failed with its words, and leaves nothing of it running", async () => {
    await expect(boot({ FAKE_VM_BOOT: "fail" })).rejects.toThrow("the fake was told to fail");
    expect(alive(pidOf())).toBe(false);
  });

  it("settles its exit with surogate-vm's last words when it exits", async () => {
    const vm = await boot({ FAKE_VM_STOP: "error" });
    expect(await vm.exited).toBe("the hypervisor failed");
  });

  it("ends surogate-vm at a kill within a second, and kills one that does not heed it", async () => {
    const heeds = await boot();
    let begun = performance.now();
    await heeds.kill();
    expect(performance.now() - begun).toBeLessThan(1_000);
    expect(alive(pidOf())).toBe(false);
    const deaf = await boot({ FAKE_VM_KILL: "ignore" });
    begun = performance.now();
    await deaf.kill();
    expect(performance.now() - begun).toBeGreaterThanOrEqual(900);
    expect(alive(pidOf())).toBe(false);
    expect(await deaf.exited).toBe("");
  });

  it("leaves surogate-vm's stdin ended when the manager is killed, and surogate-vm exits", async () => {
    built(DARWIN);
    const fake = program();
    const manager: ChildProcess = spawn(process.execPath, ["--input-type=module", "-e", [
      `const { bootDarwin } = await import(${JSON.stringify(DARWIN)});`,
      `await bootDarwin({ ...${JSON.stringify(options)}, program: ${JSON.stringify(fake)} }, undefined, performance.now() + 10000);`,
      'console.log("booted"); setInterval(() => {}, 1000);',
    ].join("\n")], { stdio: ["ignore", "pipe", "inherit"] });
    await new Promise<void>((resolve) => manager.stdout!.on("data", (chunk: Buffer) => chunk.toString().includes("booted") && resolve()));
    const pid = pidOf();
    expect(alive(pid)).toBe(true);
    manager.kill("SIGKILL");
    await until(() => !alive(pid));
  });

  it("sweeps a surogate-vm its manager left before it boots another, and leaves its folder empty for the next", async () => {
    const left = spawn(program(), ["run", options.run, "--data", join(dir, "data"), "--image", join(dir, "image"), "--bundle", join(dir, "app")], { stdio: ["pipe", "ignore", "ignore"] });
    mkdirSync(options.run, { recursive: true, mode: 0o700 });
    await until(() => existsSync(join(options.run, "vm.pid")) && existsSync(join(options.run, "inbound.sock")));
    expect(alive(left.pid!)).toBe(true);
    await boot();
    await until(() => !alive(left.pid!));
    expect(pidOf()).not.toBe(left.pid);
  });

  it("answers what this Mac lacks as surogate-vm's check says: nothing, or why it cannot run the sandbox", async () => {
    expect(await requirementsDarwin(program())).toEqual([]);
    expect(await requirementsDarwin(program({ FAKE_VM_SUPPORTED: "0" }))).toEqual(["This Mac cannot run Surogate's sandbox: the fake cannot"]);
    expect(await requirementsDarwin(join(dir, "missing"))).toEqual([expect.stringMatching(/^This Mac cannot run Surogate's sandbox: surogate-vm did not answer/)]);
  });
});

describe("folders shared into the guest on macOS", () => {
  it("shares a folder as a directory of the one device for every folder, its name never given again, or as one of eight by its tag", async () => {
    const vm = await boot();
    const work = join(dir, "work");
    expect(await vm.share(work, 501, performance.now() + 5_000)).toEqual({ kind: "vz", tag: "surogate", name: "r1" });
    await vm.unshare({ kind: "vz", tag: "surogate", name: "r1" }, performance.now() + 5_000);
    expect(await vm.share(work, 501, performance.now() + 5_000)).toEqual({ kind: "vz", tag: "surogate", name: "r2" });
    const single = await boot({}, { layout: "single", run: runtimeFolder(join(dir, "other"), dir) });
    expect(await single.share(work, 501, performance.now() + 5_000)).toEqual({ kind: "vz", tag: "s1" });
  });

  it("says why a folder was not shared, and why no ninth one is", async () => {
    const refused = await boot({ FAKE_VM_SHARE: "refuse" });
    await expect(refused.share(join(dir, "work"), 501, performance.now() + 5_000)).rejects.toThrow("the folder could not be shared: the fake refuses every folder");
    const full = await boot({}, { run: runtimeFolder(join(dir, "full"), dir) });
    for (let n = 0; n < 8; n += 1) await full.share(join(dir, "work"), n < 6 ? 501 : 0, performance.now() + 5_000, n === 7 ? { readonly: true } : {});
    await expect(full.share(join(dir, "work"), 501, performance.now() + 5_000)).rejects.toThrow("it holds as many folders as it has room for, 8: the folders of 6 chats, and 2 for the histories of project folders, which take two each");
  });

  it("ends the VM when a folder is not let go by the deadline, or not shared by it", async () => {
    const holds = await boot({ FAKE_VM_UNSHARE: "silent" });
    const share = await holds.share(join(dir, "work"), 501, performance.now() + 5_000);
    await expect(holds.unshare(share, performance.now() + 300)).rejects.toThrow("surogate-vm did not answer in time");
    await holds.exited;
    const silent = await boot({ FAKE_VM_SHARE: "silent" }, { run: runtimeFolder(join(dir, "silent"), dir) });
    await expect(silent.share(join(dir, "work"), 501, performance.now() + 300)).rejects.toThrow("surogate-vm did not answer in time");
    await silent.exited;
  });
});

describe("the VM manager's guest on the macOS backend", () => {
  const folder = () => {
    const path = join(dir, "work");
    const { dev, ino } = statSync(path);
    return { path, dev, ino };
  };

  it("sets a root up on its folder and runs its operations, the root on a uid of its own from 10000 up, as on Linux", async () => {
    const guest = await Guest.boot(bootDarwin, { ...options, program: program() });
    expect(await guest.ready(ROOT, folder())).toBeNull();
    expect(await guest.op(ROOT, "run", { command: "echo hi", workdir: null, timeout: 10 }, new AbortController().signal))
      .toEqual({ ok: { output: `run in ${ROOT}: echo hi`, returncode: 0, timed_out: false } });
    await guest.stop();
    expect(alive(pidOf())).toBe(false);
  });

  it("shares no folder for a root whose uid is below 10000, which the rule would not judge, as on Linux", async () => {
    const guest = await Guest.boot(bootDarwin, { ...options, program: program({ FAKE_VM_UID: "501" }) });
    expect(await guest.ready(ROOT, folder())).toMatchObject({ error: { type: "unavailable", message: expect.stringContaining("the guest gave no uid a root can have") } });
    await guest.stop();
  });

  it("ends a VM whose guest has not powered off by the power-off wait: its kill, then SIGKILL", async () => {
    const guest = await Guest.boot(bootDarwin, { ...options, program: program({ FAKE_VM_POWEROFF: "ignore", FAKE_VM_KILL: "ignore" }), powerOffMs: 300 });
    const pid = pidOf();
    const begun = performance.now();
    await guest.stop();
    // The power-off's wait, then the kill line's second, then SIGKILL.
    expect(performance.now() - begun).toBeGreaterThanOrEqual(1_200);
    expect(alive(pid)).toBe(false);
  });
});
