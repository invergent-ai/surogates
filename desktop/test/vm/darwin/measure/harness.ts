// What every measurement of the macOS VM shares (README in prepare.sh): the guest booted on this Mac
// through the real surogate-vm, as the app's VM manager boots it, a root set up on a folder of
// the measurement's own, its commands, and its findings written down. Run on a Mac alone: each
// measurement is a vitest file of this folder, run by its own config (vitest.config.ts here).
//
// surogate-vm is started with Node's own spawn here: what is measured is the VM's, and the app's
// clean spawn on macOS (src/clean-child.ts) is another part's. Everything else is the app's code.

import { execFileSync, spawn } from "node:child_process";
import { closeSync, mkdirSync, openSync, readSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir, tmpdir, userInfo } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { HostUser } from "../../../../src/guest/protocol.js";
import type { Outcome } from "../../../../src/link/protocol.js";
import { darwinBoot, runtimeFolder } from "../../../../src/vm/darwin.js";
import { type BootVm, type Folder, Guest, type VmBackend, type VmOptions } from "../../../../src/vm/manager.js";
import type { Egress } from "../../../../src/vm/proxy.js";
import type { Start } from "../../../../src/vm/vm-helper.js";

export const DESKTOP = realpathSync(fileURLToPath(new URL("../../../../", import.meta.url)));
/** The arm64 guest image (prepare.sh builds it): SUROGATE_VM_IMAGE, else the repository's images/guest/out. */
export const IMAGE = realpathSync(process.env.SUROGATE_VM_IMAGE ?? join(DESKTOP, "..", "images", "guest", "out"));
/**
 * The agent disks prepare.sh makes from dist/: the app's, in dist/; and the one whose init runs a measurement's
 * probe as the guest's root (probe.sh), kept out of dist/, which the app is built from, in a folder of this one's.
 */
export const AGENT_DISK = join(DESKTOP, "dist", "agent.img");
export const PROBING_DISK = join(DESKTOP, "test", "vm", "darwin", "measure", "probing", "agent.img");
/** surogate-vm as native/build.sh signs it. */
export const PROGRAM = process.env.SUROGATE_VM_PROGRAM ?? join(DESKTOP, "native", "out", "surogate-vm");
/** Where each measurement writes what it found. */
export const OUT = join(DESKTOP, "test", "vm", "darwin", "measure", "out");

const me = userInfo();
export const USER: HostUser = { uid: me.uid, gid: me.gid, name: me.username, home: homedir() };
export const ROOTS = ["0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f", "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f", "1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b", "9a8b7c6d-5e4f-4a3b-9c2d-1e0f2a3b4c5d"];
export const ALLOWING: Egress = { ask: () => Promise.resolve(true) };

/** What happened in a boot, *ms* after it was asked, on this Mac's monotonic clock. */
export interface Event {
  ms: number;
  what: string;
}

/**
 * Where a boot's time went: *events*, each surogate-vm's start and exit, each line it said, the boot line sent,
 * its own times on its started line (after it read the boot line) placed after the boot line, the console's first
 * byte, the backend's boot and the agent's hello; *console*, each line of the guest's console when it was first seen.
 */
export interface Timeline {
  events: Event[];
  console: Array<{ ms: number; line: string }>;
}

/** *timeline*'s events in order, each as "<ms> ms: <what>". */
export const told = (timeline: Timeline) => [...timeline.events].sort((a, b) => a.ms - b.ms).map(({ ms, what }) => `${ms} ms: ${what}`);

// Node's spawn, as the measurements start surogate-vm (above), with what it is told and says heard on the way into
// *timeline*, each *at()*.
function timedStart(at: () => number, timeline: Timeline): Start {
  return (program, args, options) => {
    const child = spawn(program, args, options);
    const command = args[0] ?? "";
    timeline.events.push({ ms: at(), what: `surogate-vm ${command} started` });
    child.once("exit", () => timeline.events.push({ ms: at(), what: `surogate-vm ${command} exited` }));
    if (command !== "run") return child;
    let sent: number | null = null;
    const stdin = child.stdin;
    if (stdin) {
      const write = stdin.write.bind(stdin) as (line: string) => boolean;
      stdin.write = ((line: string) => {
        if (line.startsWith('{"type":"boot"')) timeline.events.push({ ms: (sent = at()), what: "the boot line sent" });
        return write(line);
      }) as typeof stdin.write;
    }
    let said = "";
    child.stdout?.on("data", (chunk: Buffer) => {
      const now = at();
      said += chunk.toString();
      for (let end = said.indexOf("\n"); end >= 0; end = said.indexOf("\n")) {
        const line = said.slice(0, end);
        said = said.slice(end + 1);
        timeline.events.push({ ms: now, what: `surogate-vm said ${line}` });
        let times: { validated?: unknown; running?: unknown } | undefined;
        try {
          times = (JSON.parse(line) as { type?: string; ms?: typeof times }).ms;
        } catch {
          // Not a line of its protocol.
        }
        if (sent === null || !times) continue;
        if (typeof times.validated === "number") timeline.events.push({ ms: sent + times.validated, what: "surogate-vm validated the VM's configuration, by its own clock" });
        if (typeof times.running === "number") timeline.events.push({ ms: sent + times.running, what: "surogate-vm had the VM running, by its own clock" });
      }
    });
    return child;
  };
}

// *path*, the guest's console, read every 5 ms as it grows, each line into *timeline* when it is first seen, and its
// first byte as an event. Returns its stop, which reads what is left first.
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
  /** Where the boot's time went, its console read until the guest is stopped. */
  timeline: Timeline;
  /** *command* in *root*, set up on *folder* first if it is not. */
  run(root: string, command: string, timeout?: number): Promise<{ output: string; returncode: number; outcome: Outcome }>;
  stop(): Promise<void>;
}

/**
 * The guest booted for measurement *name*; *extra* goes into its options. Its data folder is new, or with *fresh*
 * false the last boot's of that name, its sessions disk kept, as every boot after the app's first finds it.
 */
export async function lab(name: string, extra: Partial<VmOptions> = {}, egress?: Egress, { fresh = true } = {}): Promise<Lab> {
  const data = join(realpathSync(tmpdir()), "surogate-measure", name);
  if (fresh) rmSync(data, { recursive: true, force: true });
  mkdirSync(join(data, "logs"), { recursive: true, mode: 0o700 });
  const options: VmOptions = {
    kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: AGENT_DISK,
    sessions: join(data, "vm", "sessions.img"), console: join(data, "logs", "vm-console.log"),
    run: runtimeFolder(data), user: USER, program: PROGRAM, data, ...extra,
  };
  // The last boot's console goes first, so that what is read is this boot's alone.
  rmSync(options.console, { force: true });
  const begun = performance.now();
  const at = () => Math.round((performance.now() - begun) * 10) / 10;
  const timeline: Timeline = { events: [], console: [] };
  const unwatch = watchConsole(options.console, at, timeline);
  let vm: VmBackend | undefined;
  const capturing: BootVm = async (...args) => {
    vm = await darwinBoot(timedStart(at, timeline))(...args);
    timeline.events.push({ ms: at(), what: "the backend's boot resolved: surogate-vm said started, and its sockets are connected" });
    return vm;
  };
  let guest: Guest;
  try {
    guest = await Guest.boot(capturing, options, undefined, () => {}, egress);
  } catch (error) {
    unwatch();
    throw error;
  }
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
