// The Windows VM backend (src/vm/win32.ts) against a stand-in for surogate-vm.exe (test/vm/win32/fake-vm.mjs), with
// the VM manager's guest above it: what it does with the program it drives, on Linux as on the Windows runner. The
// real backend starts surogate-vm.exe through the app's clean spawn, which on Windows is the shell's helper
// (surogate-helper.exe spawn, the shell's); the tests start the stand-in with Node's own spawn, through win32Boot's
// factory, so they need nothing of that, as macOS's measurements start surogate-vm with Node's own.

import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createServer } from "node:net";

import { PLAN9_PORT } from "../src/guest/protocol.js";
import { socketsIn } from "../src/vm/sockets.js";
import { backendFor } from "../src/vm/backends.js";
import { Door, Forwarded } from "../src/vm/inbound.js";
import { Guest, type VmBackend, type VmOptions } from "../src/vm/manager.js";
import type { Start } from "../src/vm/vm-helper.js";
import { addressesWin32, bootWin32, checkWin32, commandLine, freshDoorWin32, requirementsWin32, runtimeFolderWin32, win32Boot } from "../src/vm/win32.js";
import { built } from "./built.js";

const FAKE = fileURLToPath(new URL("./vm/win32/fake-vm.mjs", import.meta.url));
const WIN32 = fileURLToPath(new URL("../dist/vm/win32.js", import.meta.url));
const ANA = { uid: 1000, gid: 1000, name: "ana", home: "/mnt/c/Users/ana" };
const ROOT = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";

let dir: string;
let options: VmOptions;
const booted: VmBackend[] = [];

// The stand-in started with Node's own spawn, told *knobs* (fake-vm.mjs) through its environment: win32Boot's own
// program argument is ignored, as the stand-in is Node, not surogate-vm.exe.
const starting = (knobs: Record<string, string>): Start => (_program, args, options) => spawn(process.execPath, [FAKE, ...args], { ...options, env: { ...options.env, ...knobs } });
// Named pipes on Windows, Unix sockets on Linux, so the backend's driver runs on both; the fake listens on the same.
const testAddresses = (run: string) => (process.platform === "win32" ? addressesWin32(run) : socketsIn(run));

const boot = async (knobs: Record<string, string> = {}, extra: Partial<VmOptions> = {}) => {
  const run = extra.run ?? options.run;
  mkdirSync(run, { recursive: true });
  const vm = await win32Boot(starting(knobs), testAddresses)({ ...options, ...extra }, undefined, performance.now() + 10_000);
  booted.push(vm);
  return vm;
};

beforeEach(() => {
  built(fileURLToPath(new URL("../dist/guest/control.js", import.meta.url)));
  built(WIN32);
  dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-win32-")));
  for (const folder of ["data/vm", "data/logs", "image", "app", "run"]) mkdirSync(join(dir, folder), { recursive: true });
  for (const file of ["image/vmlinuz", "image/initrd", "image/rootfs.img", "app/agent.vhd"]) writeFileSync(join(dir, file), "");
  options = {
    kernel: join(dir, "image", "vmlinuz"), rootfs: join(dir, "image", "rootfs.img"), agentDisk: join(dir, "app", "agent.vhd"),
    sessions: join(dir, "data", "vm", "sessions.vhdx"), console: join(dir, "data", "logs", "vm-console.log"),
    run: join(dir, "run", "vm-0b6c1d3e"), user: ANA,
  };
});

afterEach(async () => {
  await Promise.all(booted.splice(0).map((vm) => vm.kill()));
  rmSync(dir, { recursive: true, force: true });
});

describe("the Windows VM backend", () => {
  it("is Windows's row of the backend table: its boot, its named-pipe ports, and no socket-path limit", () => {
    const win32 = backendFor("win32")!;
    expect(win32.boot).toBe(bootWin32);
    expect(win32.socketMax).toBeNull();
    expect(win32.addresses(options.run, ANA)).toEqual(addressesWin32(options.run));
    expect(win32.runtimeFolder(ANA, { LOCALAPPDATA: "C:\\Users\\ana\\AppData\\Local" }, "C:\\Users\\ana\\AppData\\Local\\Surogate"))
      .toBe(runtimeFolderWin32({ LOCALAPPDATA: "C:\\Users\\ana\\AppData\\Local" }, "C:\\Users\\ana\\AppData\\Local\\Surogate"));
  });

  it("names its ports as pipes only this user may open, and the guest's command line names Hyper-V's devices and Windows's refused names", () => {
    // The runtime folder's own 8 hex names this boot's pipes apart; basename splits the OS's separator (host-path).
    const run = `${sep}run${sep}vm-0b6c1d3e`;
    expect(addressesWin32(run)).toEqual({
      control: "\\\\.\\pipe\\surogate-0b6c1d3e-control", net: "\\\\.\\pipe\\surogate-0b6c1d3e-net", inbound: "\\\\.\\pipe\\surogate-0b6c1d3e-inbound", door: "\\\\.\\pipe\\surogate-0b6c1d3e-door",
    });
    expect(commandLine()).toBe("console=ttyS0 panic=-1 quiet surogate.disks=scsi surogate.transport=vsock surogate.names=windows lsm=landlock,lockdown,yama,integrity,apparmor,bpf");
  });

  it("boots once its three ports are connected and it said started, its boot line naming the files and Windows's kernel line", async () => {
    const log = join(dir, "said.log");
    const vm = await boot({ FAKE_VM_LOG: log });
    expect([vm.emulated, vm.rootUid]).toEqual([null, undefined]);
    const [line] = readFileSync(log, "utf8").trim().split("\n").map((text) => JSON.parse(text) as Record<string, unknown>);
    expect(line).toEqual({
      type: "boot", kernel: options.kernel, initrd: join(dir, "image", "initrd"), rootfs: options.rootfs, agentDisk: options.agentDisk,
      sessions: options.sessions, console: options.console, cpus: expect.any(Number), commandLine: commandLine(),
    });
  });

  it("rejects a boot that failed with its words", async () => {
    await expect(boot({ FAKE_VM_BOOT: "fail" })).rejects.toThrow("the fake was told to fail");
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
    const deaf = await boot({ FAKE_VM_KILL: "ignore" }, { run: join(dir, "run", "vm-deaf1234") });
    begun = performance.now();
    await deaf.kill();
    expect(performance.now() - begun).toBeGreaterThanOrEqual(900);
    expect(await deaf.exited).toBe("");
  });

  it("stops the VM when its stdin ends", async () => {
    const vm = await boot();
    (vm as unknown as { helper: { child: { stdin: NodeJS.WritableStream } } }).helper.child.stdin.end();
    expect(await vm.exited).toBe("");
  });
});

describe("folders shared into the guest on Windows", () => {
  it("shares a folder as a Plan 9 share, its tag never given again, with the root's uid in its aname", async () => {
    const vm = await boot();
    const work = join(dir, "work");
    expect(await vm.share(work, 1000, performance.now() + 5_000)).toEqual({ kind: "p9", tag: "r1", port: PLAN9_PORT, aname: "uid=1000;gid=1000" });
    await vm.unshare({ kind: "p9", tag: "r1", port: PLAN9_PORT, aname: "uid=1000;gid=1000" }, performance.now() + 5_000);
    expect(await vm.share(work, 1000, performance.now() + 5_000)).toEqual({ kind: "p9", tag: "r2", port: PLAN9_PORT, aname: "uid=1000;gid=1000" });
    expect(await vm.share(work, 0, performance.now() + 5_000, { readonly: true })).toEqual({ kind: "p9", tag: "r3", port: PLAN9_PORT, aname: "uid=0;gid=0" });
  });

  it("says why a folder was not shared, and why no ninth one is", async () => {
    const refused = await boot({ FAKE_VM_SHARE: "refuse" });
    await expect(refused.share(join(dir, "work"), 1000, performance.now() + 5_000)).rejects.toThrow("the folder could not be shared: the fake refuses every folder");
    const full = await boot({}, { run: join(dir, "run", "vm-full0000") });
    for (let n = 0; n < 8; n += 1) await full.share(join(dir, "work"), n < 6 ? 1000 : 0, performance.now() + 5_000, n === 7 ? { readonly: true } : {});
    await expect(full.share(join(dir, "work"), 1000, performance.now() + 5_000)).rejects.toThrow("it holds as many folders as it has room for, 8: the folders of 6 chats, and 2 for the histories of project folders, which take two each");
  });

  it("ends the VM when a folder is not let go by the deadline, or not shared by it", async () => {
    const holds = await boot({ FAKE_VM_UNSHARE: "silent" });
    const share = await holds.share(join(dir, "work"), 1000, performance.now() + 5_000);
    await expect(holds.unshare(share, performance.now() + 300)).rejects.toThrow("surogate-vm did not answer in time");
    await holds.exited;
    const silent = await boot({ FAKE_VM_SHARE: "silent" }, { run: join(dir, "run", "vm-silent00") });
    await expect(silent.share(join(dir, "work"), 1000, performance.now() + 300)).rejects.toThrow("surogate-vm did not answer in time");
    await silent.exited;
  });
});

describe("the VM manager's guest on the Windows backend", () => {
  const folder = () => {
    const path = join(dir, "work");
    mkdirSync(path, { recursive: true });
    const { dev, ino } = statSync(path);
    return { path, dev, ino };
  };

  // The manager's folder identity and realpath on Windows are the file host's; until its win32 row
  // lands, a folder under the runner's temp folder is seen as unavailable. The whole guest on Windows is run by hand
  // on a PC (the VM suite run by hand); here the test runs on Linux, where the backend's driver is what is exercised.
  it.skipIf(process.platform === "win32")("sets a root up on its folder and runs its operations", async () => {
    mkdirSync(options.run, { recursive: true });
    let captured: VmBackend | undefined;
    const guest = await Guest.boot(async (...args) => (captured = await win32Boot(starting({}), testAddresses)(...args)), { ...options });
    if (captured) booted.push(captured);
    expect(await guest.ready(ROOT, folder())).toBeNull();
    expect(await guest.op(ROOT, "run", { command: "echo hi", workdir: null, timeout: 10 }, new AbortController().signal))
      .toEqual({ ok: { output: `run in ${ROOT}: echo hi`, returncode: 0, timed_out: false } });
    await guest.stop();
  });
});

describe("what this Windows computer lacks of its sandbox", () => {
  it("answers each case in Section 11's words, from surogate-vm check's facts", async () => {
    expect(await requirementsWin32(async () => ({ virtualMachinePlatform: true, service: "present", hostCompute: "ok" }))).toEqual([]);
    expect(await requirementsWin32(async () => ({ virtualMachinePlatform: false, service: "present", hostCompute: "absent" })))
      .toEqual(["Windows's Virtual Machine Platform is off. An administrator turns it on from Surogate's Settings, and Windows restarts."]);
    expect(await requirementsWin32(async () => ({ virtualMachinePlatform: true, service: "missing", hostCompute: "absent" })))
      .toEqual(["Surogate's sandbox tools are missing. Install Surogate again"]);
    expect(await requirementsWin32(async () => ({ virtualMachinePlatform: true, service: "present", hostCompute: "old" })))
      .toEqual(["This Windows is too old to run Surogate's sandbox. Update to a serviced Windows 11."]);
  });

  it("says the tools are missing where surogate-vm does not run", async () => {
    expect(await checkWin32(join(dir, "missing.exe"))).toEqual({ virtualMachinePlatform: false, service: "missing", hostCompute: "absent" });
  });
});

describe("the browser's door on Windows", () => {
  it("is a fresh pipe name each start, 128 random bits, not derivable from the data folder", () => {
    const a = freshDoorWin32();
    const b = freshDoorWin32();
    expect(a).toMatch(/^\\\\\.\\pipe\\surogate-door-[0-9a-f]{32}$/);
    expect(a).not.toBe(b);
    // The data folder's own derivable name (addressesWin32) is never the door the app uses.
    for (const run of ["C:\\Users\\ana\\AppData\\Local\\Surogate\\run\\vm-0b6c1d3e", `${sep}run${sep}vm-0b6c1d3e`]) {
      expect(a).not.toBe(addressesWin32(run).door);
      expect(b).not.toBe(addressesWin32(run).door);
    }
  });

  // The door's pipe behaviour needs a real Windows named pipe; it runs on the Windows runner.
  const onWindows = it.skipIf(process.platform !== "win32");

  onWindows("listens on the fresh name though a pipe of the old derivable name was made first", async () => {
    const run = `${sep}run${sep}vm-abcdef01`;
    // A squatter pre-creates the old, derivable door name: it must not matter, because the app's door is the fresh one.
    const squat = createServer();
    await new Promise<void>((resolve, reject) => {
      squat.once("error", reject);
      squat.listen(addressesWin32(run).door, resolve);
    });
    const fresh = freshDoorWin32();
    const open = await new Promise<boolean>((resolve) => {
      const door = new Door(fresh, new Forwarded(), async () => "", {}, resolve);
      void door;
    });
    expect(open).toBe(true);
    squat.close();
  });

  onWindows("closes the door when its listen fails, so the browser is told it is closed", async () => {
    const name = freshDoorWin32();
    const first = await new Promise<boolean>((resolve) => {
      const door = new Door(name, new Forwarded(), async () => "", {}, resolve);
      void door;
    });
    expect(first).toBe(true);
    // A second listen on the same name is the first-instance conflict a squatter would cause: fatal, door closed.
    const second = await new Promise<boolean>((resolve) => {
      const door = new Door(name, new Forwarded(), async () => "", {}, resolve);
      void door;
    });
    expect(second).toBe(false);
  });
});
