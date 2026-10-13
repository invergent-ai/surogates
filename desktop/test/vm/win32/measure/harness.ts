// What every measurement of the Windows VM shares: the guest booted on this PC through the real surogate-vm.exe, as
// the app's VM manager boots it but for the clean spawn (the shell's helper, milestone E), a root set up on a folder
// of the measurement's own, its commands, and its findings written down. Run on a Windows PC alone, each a vitest file
// of this folder, run by its own config (vitest.config.ts here), after an elevated dev-service.ps1 has installed the
// Surogate VM service (native/windows/dev-service.ps1) and the Rust programs are built.
//
// surogate-vm.exe is started with Node's own spawn here: what is measured is the VM's, and the app's clean spawn on
// Windows is the shell's. Everything else is the app's code.

import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import { mkdirSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir, tmpdir, userInfo } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { HostUser } from "../../../../src/guest/protocol.js";
import type { Outcome } from "../../../../src/link/protocol.js";
import { type BootVm, type Folder, Guest, type VmBackend, type VmOptions } from "../../../../src/vm/manager.js";
import type { Egress } from "../../../../src/vm/proxy.js";
import type { Start } from "../../../../src/vm/vm-helper.js";
import { identityOf } from "../../../../src/files/identity.js";
import { win32Boot } from "../../../../src/vm/win32.js";

export const DESKTOP = realpathSync(fileURLToPath(new URL("../../../../", import.meta.url)));
/** The x64 guest image (gh run download of milestone B's artifact): SUROGATE_VM_IMAGE, else the repository's out folder. */
export const IMAGE = realpathSync(process.env.SUROGATE_VM_IMAGE ?? join(DESKTOP, "..", "images", "guest", "out"));
/** The agent disk as a fixed VHD: the app's, or the one whose init runs a measurement's probe as root. */
export const AGENT_DISK = process.env.SUROGATE_VM_AGENT_DISK ?? join(DESKTOP, "dist", "agent.vhd");
export const PROBING_DISK = process.env.SUROGATE_VM_PROBING_DISK ?? join(DESKTOP, "dist", "agent-measure.vhd");
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

// Node's spawn, as the measurements start surogate-vm.exe (above).
const start: Start = (program, args, options) => spawn(program, args, options);

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
  run(root: string, command: string, timeout?: number): Promise<{ output: string; returncode: number; outcome: Outcome }>;
  stop(): Promise<void>;
}

/** The guest booted for measurement *name*, its data folder new; *extra* goes into its options. */
export async function lab(name: string, extra: Partial<VmOptions> = {}, egress?: Egress): Promise<Lab> {
  const data = join(realpathSync(tmpdir()), "surogate-measure", name);
  rmSync(data, { recursive: true, force: true });
  mkdirSync(join(data, "logs"), { recursive: true });
  mkdirSync(join(data, "run"), { recursive: true });
  const options: VmOptions = {
    kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: AGENT_DISK,
    sessions: join(data, "vm", "sessions.vhdx"), console: join(data, "logs", "vm-console.log"),
    run: join(data, "run", "vm-measure"), user: USER, program: PROGRAM, data, ...extra,
  };
  mkdirSync(options.run, { recursive: true });
  let vm: VmBackend | undefined;
  const capturing: BootVm = async (...args) => (vm = await win32Boot(start)(...args));
  const guest = await Guest.boot(capturing, options, undefined, () => {}, egress);
  return {
    guest, vm: vm!, options, data,
    async run(root, command, timeout = 60) {
      const outcome = await guest.op(root, "run", { command, workdir: null, timeout }, signal());
      const ok = "ok" in outcome ? (outcome.ok as { output: string; returncode: number }) : { output: JSON.stringify(outcome), returncode: -1 };
      return { output: ok.output, returncode: ok.returncode, outcome };
    },
    stop: () => guest.stop(),
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
