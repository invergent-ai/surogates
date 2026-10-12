// What every measurement of the macOS VM shares (README in prepare.sh): the guest booted on this Mac
// through the real surogate-vm, as the app's VM manager boots it, a root set up on a folder of
// the measurement's own, its commands, and its findings written down. Run on a Mac alone: each
// measurement is a vitest file of this folder, run by its own config (vitest.config.ts here).
//
// surogate-vm is started with Node's own spawn here: what is measured is the VM's, and the app's
// clean spawn on macOS (src/clean-child.ts) is another part's. Everything else is the app's code.

import { execFileSync, spawn } from "node:child_process";
import { mkdirSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir, tmpdir, userInfo } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { HostUser } from "../../../../src/guest/protocol.js";
import type { Outcome } from "../../../../src/link/protocol.js";
import { darwinBoot, runtimeFolder, type Start } from "../../../../src/vm/darwin.js";
import { type BootVm, type Folder, Guest, type VmBackend, type VmOptions } from "../../../../src/vm/manager.js";
import type { Egress } from "../../../../src/vm/proxy.js";

export const DESKTOP = realpathSync(fileURLToPath(new URL("../../../../", import.meta.url)));
/** The arm64 guest image (prepare.sh builds it): SUROGATE_VM_IMAGE, else the repository's images/guest/out. */
export const IMAGE = realpathSync(process.env.SUROGATE_VM_IMAGE ?? join(DESKTOP, "..", "images", "guest", "out"));
/** The agent disk prepare.sh makes from dist/: the app's, or the one whose init runs a measurement's probe as root. */
export const AGENT_DISK = join(DESKTOP, "dist", "agent.img");
export const PROBING_DISK = join(DESKTOP, "dist", "agent-measure.img");
/** surogate-vm as native/build.sh signs it. */
export const PROGRAM = process.env.SUROGATE_VM_PROGRAM ?? join(DESKTOP, "native", "out", "surogate-vm");
/** Where each measurement writes what it found. */
export const OUT = join(DESKTOP, "test", "vm", "darwin", "measure", "out");

const me = userInfo();
export const USER: HostUser = { uid: me.uid, gid: me.gid, name: me.username, home: homedir() };
export const ROOTS = ["0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f", "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f", "1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b", "9a8b7c6d-5e4f-4a3b-9c2d-1e0f2a3b4c5d"];
export const ALLOWING: Egress = { ask: () => Promise.resolve(true) };

// Node's spawn, as the measurements start surogate-vm (above).
const start: Start = (program, args, options) => spawn(program, args, options);

export const ms = (since: number) => Math.round(performance.now() - since);
export const median = (values: number[]) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)] ?? 0;
export const signal = () => new AbortController().signal;
export const folderOf = (path: string): Folder => {
  const { dev, ino } = statSync(path);
  return { path, dev, ino };
};

/** A folder of the measurement's own, under ~/SurogateMeasure (no privacy consent asked there), made empty. */
export function scratch(name: string, base = join(homedir(), "SurogateMeasure")): string {
  const path = join(base, name);
  rmSync(path, { recursive: true, force: true });
  mkdirSync(path, { recursive: true });
  return realpathSync(path);
}

export interface Lab {
  guest: Guest;
  /** The backend under the guest, for what a measurement asks of it directly. */
  vm: VmBackend;
  options: VmOptions;
  /** The data folder of this boot: its sessions disk and console log. */
  data: string;
  /** *command* in *root*, set up on *folder* first if it is not. */
  run(root: string, command: string, timeout?: number): Promise<{ output: string; returncode: number; outcome: Outcome }>;
  stop(): Promise<void>;
}

/** The guest booted for measurement *name*, its data folder new; *extra* goes into its options. */
export async function lab(name: string, extra: Partial<VmOptions> = {}, egress?: Egress): Promise<Lab> {
  const data = join(realpathSync(tmpdir()), "surogate-measure", name);
  rmSync(data, { recursive: true, force: true });
  mkdirSync(join(data, "logs"), { recursive: true, mode: 0o700 });
  const options: VmOptions = {
    kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: AGENT_DISK,
    sessions: join(data, "vm", "sessions.img"), console: join(data, "logs", "vm-console.log"),
    run: runtimeFolder(data), user: USER, program: PROGRAM, data, ...extra,
  };
  let vm: VmBackend | undefined;
  const capturing: BootVm = async (...args) => (vm = await darwinBoot(start)(...args));
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

// This Mac, as the findings name it.
function mac(): Record<string, string> {
  const said = (program: string, args: string[]) => {
    try {
      return execFileSync(program, args, { encoding: "utf8" }).trim();
    } catch {
      return "";
    }
  };
  return {
    model: said("/usr/sbin/sysctl", ["-n", "hw.model"]), chip: said("/usr/sbin/sysctl", ["-n", "machdep.cpu.brand_string"]),
    macos: said("/usr/bin/sw_vers", ["-productVersion"]), build: said("/usr/bin/sw_vers", ["-buildVersion"]),
  };
}

/**
 * What measurement *name* found, written to out/<name>.json with this Mac's model, chip and macOS, and
 * said: each finding, then each check as PASS or FAIL. Returns the checks that failed.
 */
export function record(name: string, found: Record<string, unknown>, checks: Record<string, boolean> = {}): string[] {
  mkdirSync(OUT, { recursive: true });
  const at = new Date().toISOString();
  writeFileSync(join(OUT, `${name}.json`), `${JSON.stringify({ name, at, mac: mac(), found, checks }, null, 2)}\n`);
  for (const [key, value] of Object.entries(found)) console.log(`MEASURE ${name}: ${key} = ${typeof value === "string" ? value : JSON.stringify(value)}`);
  for (const [check, held] of Object.entries(checks)) console.log(`MEASURE ${name}: ${held ? "PASS" : "FAIL"} ${check}`);
  console.log(`MEASURE ${name}: written to ${join(OUT, `${name}.json`)}`);
  return Object.entries(checks).filter(([, held]) => !held).map(([check]) => check);
}
