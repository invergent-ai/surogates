// What every measurement of the Windows VM shares: the guest booted on this PC through the real surogate-vm.exe, as
// the app's VM manager boots it but for the clean spawn (the shell's helper), a root set up on a folder
// of the measurement's own, its commands, and its findings written down. Run on a Windows PC alone, each a vitest file
// of this folder, run by its own config (vitest.config.ts here), after an elevated dev-service.ps1 has installed the
// Surogate VM service (native/windows/dev-service.ps1) and the Rust programs are built.
//
// surogate-vm.exe is started with Node's own spawn here: what is measured is the VM's, and the app's clean spawn on
// Windows is the shell's. Everything else is the app's code.

import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import { closeSync, copyFileSync, existsSync, mkdirSync, openSync, readFileSync, readSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir, tmpdir, userInfo } from "node:os";
import { basename, join } from "node:path";
import { fileURLToPath } from "node:url";

import type { HostUser } from "../../../../src/guest/protocol.js";
import type { Outcome } from "../../../../src/link/protocol.js";
import { type BootVm, type Folder, Guest, type VmBackend, type VmOptions } from "../../../../src/vm/manager.js";
import type { Egress } from "../../../../src/vm/proxy.js";
import type { Start } from "../../../../src/vm/vm-helper.js";
import { identityOf } from "../../../../src/files/identity.js";
import { readManifest } from "../../../../src/vm/image.js";
import { addFooter } from "../../../../src/vm/vhd.js";
import { win32Boot } from "../../../../src/vm/win32.js";

export const DESKTOP = realpathSync(fileURLToPath(new URL("../../../../", import.meta.url)));
/** The x64 guest image (gh run download of the guest build's artifact): SUROGATE_VM_IMAGE, else the repository's out folder. */
export const IMAGE = realpathSync(process.env.SUROGATE_VM_IMAGE ?? join(DESKTOP, "..", "images", "guest", "out"));
/**
 * The measurements' data folder, as the app's own is to its VM: in this user's profile by its long names, as the
 * service takes no other and its walk refuses a short name (a runner's temp folder is under RUNNER~1), and holding
 * every file a boot names, as the service takes the kernel, the initrd and the disks from the caller's data folder
 * alone. Each measurement has a folder of its own in it.
 */
export const DATA = join(realpathSync.native(tmpdir()), "surogate-measure");

// The image, copied into the data folder unless the copy there is of the same build: its manifest
// (images/guest/build.sh), copied last, marks a whole copy. Its 2.9 GB are copied here, never at a boot that is timed.
// Its disk is a fixed VHD there, as Hyper-V opens a disk only as one: given its footer by the hash its manifest
// names, as the app's delivery gives it on Windows (vm/image.ts).
const IMAGE_COPY = join(DATA, "image");
const manifest = readFileSync(join(IMAGE, "manifest.json"));
if (!existsSync(join(IMAGE_COPY, "manifest.json")) || !readFileSync(join(IMAGE_COPY, "manifest.json")).equals(manifest)) {
  rmSync(IMAGE_COPY, { recursive: true, force: true });
  mkdirSync(IMAGE_COPY, { recursive: true });
  for (const name of ["initrd", "rootfs.img", "vmlinuz"]) copyFileSync(join(IMAGE, name), join(IMAGE_COPY, name));
  const rootfs = readManifest(join(IMAGE, "manifest.json"), "amd64").files.find(({ name }) => name === "rootfs.img")!;
  await addFooter(join(IMAGE_COPY, "rootfs.img"), rootfs.sha256);
  writeFileSync(join(IMAGE_COPY, "manifest.json"), manifest);
}

// The agent disk *from*, copied into the data folder's *folder*, and a fixed VHD there: given its footer, unless it
// has it already (vm/vhd.ts). None is, where *from* is not there, and the service says so at the boot.
async function copied(from: string, folder: string): Promise<string> {
  rmSync(join(DATA, folder), { recursive: true, force: true });
  mkdirSync(join(DATA, folder), { recursive: true });
  const to = join(DATA, folder, basename(from));
  if (existsSync(from)) {
    copyFileSync(from, to);
    await addFooter(to);
  }
  return to;
}

/** The agent disk as a fixed VHD, copied into the data folder: the app's, or the one whose init runs a measurement's probe as root. */
export const AGENT_DISK = await copied(process.env.SUROGATE_VM_AGENT_DISK ?? join(DESKTOP, "dist", "agent.vhd"), "agent");
export const PROBING_DISK = await copied(process.env.SUROGATE_VM_PROBING_DISK ?? join(DESKTOP, "dist", "agent-measure.vhd"), "probing");
/** surogate-vm.exe, the Cargo build's (native/windows/target/release). */
export const PROGRAM = process.env.SUROGATE_VM_PROGRAM ?? join(DESKTOP, "native", "windows", "target", "release", "surogate-vm.exe");
/** Where each measurement writes what it found. */
export const OUT = join(DESKTOP, "test", "vm", "win32", "measure", "out");

// A Windows user as the guest's roots run for them (host-user.ts, win32): uid and gid 1000, the name a Linux name, the
// home the profile's guest path. The guest gives each root its own uid from FIRST_UID up; this is the host user's.
const me = userInfo();
const linuxName = me.username.toLowerCase().replace(/[^a-z0-9_-]/g, "_") || "user";
export const USER: HostUser = { uid: 1000, gid: 1000, name: linuxName, home: `/mnt/c/Users/${me.username}` };
export const ROOTS = ["0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f", "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f", "1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b", "9a8b7c6d-5e4f-4a3b-9c2d-1e0f2a3b4c5d"];
export const ALLOWING: Egress = { ask: () => Promise.resolve(true) };

/** What happened in a boot, *ms* after it was asked, on this PC's monotonic clock. */
export interface Event {
  ms: number;
  what: string;
}

/**
 * Where a boot's time went: *begun*, when it was asked, by this PC's wall clock, to set it beside Windows's own logs;
 * *events*, surogate-vm.exe's start and exit, each line it said, the boot line sent, the console's first byte, the
 * backend's boot and the agent's hello, or why the boot failed; *console*, each line of the guest's console when it
 * was first seen.
 */
export interface Timeline {
  begun: string;
  events: Event[];
  console: Array<{ ms: number; line: string }>;
}

/** *timeline*'s events in order, each as "<ms> ms: <what>". */
export const told = (timeline: Timeline) => [...timeline.events].sort((a, b) => a.ms - b.ms).map(({ ms, what }) => `${ms} ms: ${what}`);

/** A boot that did not start: its words, the console's last lines among them, and where its time went. */
export class BootFailed extends Error {
  constructor(message: string, readonly timeline: Timeline) {
    super(message);
  }
}

// Node's spawn, as the measurements start surogate-vm.exe (above), with what it is told and says heard on the way
// into *timeline*, each *at()*.
function timedStart(at: () => number, timeline: Timeline): Start {
  return (program, args, options) => {
    const child = spawn(program, args, options);
    const command = args[0] ?? "";
    timeline.events.push({ ms: at(), what: `surogate-vm ${command} started` });
    child.once("exit", () => timeline.events.push({ ms: at(), what: `surogate-vm ${command} exited` }));
    if (command !== "run") return child;
    const stdin = child.stdin;
    if (stdin) {
      const write = stdin.write.bind(stdin) as (line: string) => boolean;
      stdin.write = ((line: string) => {
        if (line.startsWith('{"type":"boot"')) timeline.events.push({ ms: at(), what: "the boot line sent" });
        return write(line);
      }) as typeof stdin.write;
    }
    let said = "";
    child.stdout?.on("data", (chunk: Buffer) => {
      const now = at();
      said += chunk.toString();
      for (let end = said.indexOf("\n"); end >= 0; end = said.indexOf("\n")) {
        timeline.events.push({ ms: now, what: `surogate-vm said ${said.slice(0, end).replace(/\r$/, "")}` });
        said = said.slice(end + 1);
      }
    });
    return child;
  };
}

// *path*, the guest's console log, read every 5 ms as it grows, each line into *timeline* when it is first seen, and
// its first byte as an event. Returns its stop, which reads what is left first.
function watchConsole(path: string, at: () => number, timeline: Timeline): () => void {
  let fd: number | null = null;
  let offset = 0;
  let partial = "";
  const buffer = Buffer.alloc(65_536);
  const read = () => {
    try {
      fd ??= openSync(path, "r");
    } catch {
      return;
    }
    for (let got = readSync(fd, buffer, 0, buffer.length, offset); got > 0; got = readSync(fd, buffer, 0, buffer.length, offset)) {
      if (offset === 0) timeline.events.push({ ms: at(), what: "the console's first byte" });
      offset += got;
      partial += buffer.subarray(0, got).toString("utf8");
    }
    const now = at();
    // The guest's terminal ends each line with \r\n.
    for (let end = partial.indexOf("\n"); end >= 0; end = partial.indexOf("\n")) {
      timeline.console.push({ ms: now, line: partial.slice(0, end).replace(/\r$/, "") });
      partial = partial.slice(end + 1);
    }
  };
  const timer = setInterval(read, 5);
  timer.unref();
  return () => {
    clearInterval(timer);
    read();
    if (partial) timeline.console.push({ ms: at(), line: partial.replace(/\r$/, "") });
    if (fd !== null) closeSync(fd);
  };
}

export const ms = (since: number) => Math.round(performance.now() - since);
export const median = (values: number[]) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)] ?? 0;
export const signal = () => new AbortController().signal;
export const folderOf = (path: string): Folder => ({ path, ...identityOf(statSync(path)) });

/** A folder of the measurement's own, under %USERPROFILE%\SurogateMeasure, made empty. */
export function scratch(name: string, base = join(homedir(), "SurogateMeasure")): string {
  const path = join(base, name);
  rmSync(path, { recursive: true, force: true });
  mkdirSync(path, { recursive: true });
  return realpathSync(path);
}

export interface Lab {
  guest: Guest;
  vm: VmBackend;
  options: VmOptions;
  data: string;
  /** Where the boot's time went, its console read until the guest is stopped. */
  timeline: Timeline;
  run(root: string, command: string, timeout?: number): Promise<{ output: string; returncode: number; outcome: Outcome }>;
  stop(): Promise<void>;
}

// The last *lines* of the console log at *path*, or nothing where there is none.
function tail(path: string, lines = 60): string {
  try {
    return readFileSync(path, "utf8").trim().split("\n").slice(-lines).join("\n");
  } catch {
    return "";
  }
}

/** The guest booted for measurement *name*, its own folder in the data folder new; *extra* goes into its options. */
export async function lab(name: string, extra: Partial<VmOptions> = {}, egress?: Egress): Promise<Lab> {
  const data = join(DATA, name);
  rmSync(data, { recursive: true, force: true });
  mkdirSync(join(data, "logs"), { recursive: true });
  mkdirSync(join(data, "run"), { recursive: true });
  const options: VmOptions = {
    kernel: join(IMAGE_COPY, "vmlinuz"), rootfs: join(IMAGE_COPY, "rootfs.img"), agentDisk: AGENT_DISK,
    sessions: join(data, "vm", "sessions.vhdx"), console: join(data, "logs", "vm-console.log"),
    run: join(data, "run", "vm-measure"), user: USER, program: PROGRAM, data: DATA, ...extra,
  };
  mkdirSync(options.run, { recursive: true });
  const begun = performance.now();
  const at = () => Math.round((performance.now() - begun) * 10) / 10;
  const timeline: Timeline = { begun: new Date().toISOString(), events: [], console: [] };
  const unwatch = watchConsole(options.console, at, timeline);
  let vm: VmBackend | undefined;
  const capturing: BootVm = async (...args) => {
    vm = await win32Boot(timedStart(at, timeline))(...args);
    timeline.events.push({ ms: at(), what: "the backend's boot resolved: surogate-vm said started, and its pipes are connected" });
    return vm;
  };
  // A boot that did not start says the console's last lines too, where the guest wrote any: its own words of why.
  const guest = await Guest.boot(capturing, options, undefined, () => {}, egress).catch((error: unknown) => {
    timeline.events.push({ ms: at(), what: `the boot failed: ${(error as Error).message}` });
    unwatch();
    const said = tail(options.console);
    throw new BootFailed(said ? `${(error as Error).message}\nThe console's last lines:\n${said}` : (error as Error).message, timeline);
  });
  timeline.events.push({ ms: at(), what: "the agent's hello" });
  return {
    guest, vm: vm!, options, data, timeline,
    async run(root, command, timeout = 60) {
      const outcome = await guest.op(root, "run", { command, workdir: null, timeout }, signal());
      const ok = "ok" in outcome ? (outcome.ok as { output: string; returncode: number }) : { output: JSON.stringify(outcome), returncode: -1 };
      return { output: ok.output, returncode: ok.returncode, outcome };
    },
    async stop() {
      try {
        await guest.stop();
      } finally {
        unwatch();
      }
    },
  };
}

/** *root* set up on *folder* in *guest*: throws why not. */
export async function ready(guest: Guest, root: string, folder: string): Promise<void> {
  const answer = await guest.ready(root, folderOf(folder));
  if (answer) throw new Error(`the root on ${folder} was not set up: ${JSON.stringify(answer)}`);
}

// This PC, as the findings name it (PowerShell, which every serviced Windows 11 has).
function pc(): Record<string, string> {
  const said = (command: string) => {
    try {
      return execFileSync("powershell.exe", ["-NoProfile", "-Command", command], { encoding: "utf8" }).trim();
    } catch {
      return "";
    }
  };
  return {
    cpu: said("(Get-CimInstance Win32_Processor).Name"),
    edition: said("(Get-CimInstance Win32_OperatingSystem).Caption"),
    build: said("(Get-ItemProperty 'HKLM:/SOFTWARE/Microsoft/Windows NT/CurrentVersion').DisplayVersion + ' ' + (Get-ItemProperty 'HKLM:/SOFTWARE/Microsoft/Windows NT/CurrentVersion').CurrentBuildNumber"),
  };
}

/**
 * What measurement *name* found, written to out/<name>.json with this PC's CPU, edition and build, and said: each
 * finding, then each check as PASS or FAIL. Returns the checks that failed.
 */
export function record(name: string, found: Record<string, unknown>, checks: Record<string, boolean> = {}): string[] {
  mkdirSync(OUT, { recursive: true });
  const at = new Date().toISOString();
  writeFileSync(join(OUT, `${name}.json`), `${JSON.stringify({ name, at, pc: pc(), found, checks }, null, 2)}\n`);
  for (const [key, value] of Object.entries(found)) console.log(`MEASURE ${name}: ${key} = ${typeof value === "string" ? value : JSON.stringify(value)}`);
  for (const [check, held] of Object.entries(checks)) console.log(`MEASURE ${name}: ${held ? "PASS" : "FAIL"} ${check}`);
  console.log(`MEASURE ${name}: written to ${join(OUT, `${name}.json`)}`);
  return Object.entries(checks).filter(([, held]) => !held).map(([check]) => check);
}
