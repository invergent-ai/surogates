// What the guest's VM tests beside this share: the image and agent disk they boot, and their helpers.

import { spawnSync } from "node:child_process";
import { closeSync, mkdirSync, mkdtempSync, openSync, readdirSync, readFileSync, readSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { userInfo } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect } from "vitest";

import type { HostUser, Share } from "../../src/guest/protocol.js";
import { emulation } from "../../src/vm/linux.js";
import type { Folder } from "../../src/vm/manager.js";

export const IMAGE = process.env.SUROGATE_VM_IMAGE ?? fileURLToPath(new URL("../../../images/guest/out", import.meta.url));
// The device each guest opens for KVM: /dev/kvm, unless the job hides it.
export const KVM = process.env.SUROGATE_VM_KVM;
// Each file's beforeAll. Without KVM they fail, as Section 11 has a VM job do: a job runs them
// emulated only by naming a device that does not exist in SUROGATE_VM_KVM, as the app takes it.
export function needsKvm(): void {
  if (process.env.SUROGATE_VM_TESTS !== "1" || KVM !== undefined) return;
  expect(emulation(), "the VM tests need KVM, or SUROGATE_VM_KVM naming a device that does not exist to run them emulated").toBeNull();
}
const AGENT_DISK = fileURLToPath(new URL("../../vm/agent-disk.sh", import.meta.url));
export const ROOT = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
export const OTHER = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
export const FIRST_UID = 10_000;
export const R1: Share = { kind: "virtiofs", tag: "r1" };
// Sockets of families whose modules a stock kernel loads on demand: AppleTalk,
// X.25, CAN, RxRPC, Phonet, AF_ALG and vsock, then SCTP.
export const SOCKETS = [
  "import socket",
  "for family in (5, 9, 29, 33, 35, 38, 40):",
  "    for kind in (1, 2, 5):",
  "        try: socket.socket(family, kind).close()",
  "        except OSError: pass",
  "try: socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_SCTP).close()",
  "except OSError: pass",
].join("\n");
// Empty files in /var/tmp and in /dev/shm until the next is refused: each holds
// guest memory that a tmpfs's size= does not count.
export const FILES = [
  "import errno, os",
  'for folder in ("/var/tmp/many", "/dev/shm/many"):',
  "    os.mkdir(folder)",
  "    try:",
  '        for n in range(10 ** 6): open(folder + "/" + str(n), "x").close()',
  '    except OSError as error: print(errno.errorcode[error.errno], end=" ")',
  "print()",
].join("\n");
// s_feature_incompat, in the superblock at 1024.
const INCOMPAT = 1024 + 0x60;
// Its folder's virtiofsd for the newest share in *run*: the daemon, and the child that serves the share.
export const shareDaemons = (run: string) => {
  const daemon = newestDaemon(run);
  return [daemon, ...spawnSync("pgrep", ["-P", String(daemon)], { encoding: "utf8" }).stdout.trim().split("\n").filter(Boolean).map(Number)];
};
// A background process that looks in its folder a second after it starts: once the share has stalled, it waits there.
export const STUCK = "env -i /usr/bin/setsid /usr/bin/nohup /bin/sh -c '/usr/bin/sleep 1; /usr/bin/stat ./stuck' < /dev/null > /dev/null 2>&1 & echo started";
// A disk's incompatible features, as its superblock names them.
export const incompat = (disk: string) => {
  const fd = openSync(disk, "r");
  try {
    const field = Buffer.alloc(4);
    readSync(fd, field, 0, 4, INCOMPAT);
    return field.readUInt32LE(0);
  } finally {
    closeSync(fd);
  }
};

export const signal = () => new AbortController().signal;
export const background = (command: string) => ({ command, workdir: null, task_id: "vm", pty: false, notify_on_complete: false, watcher_interval: null });

export async function until(check: () => boolean, ms = 10_000): Promise<void> {
  for (const end = Date.now() + ms; !check(); await new Promise((resolve) => setTimeout(resolve, 50))) {
    if (Date.now() > end) throw new Error("timed out");
  }
}
export const folderOf = (path: string): Folder => {
  const { dev, ino } = statSync(path);
  return { path, dev, ino };
};
// The pid of the newest share's virtiofsd in the runtime folder *run*: each share has a number of its own.
export const newestDaemon = (run: string) => {
  const newest = Math.max(...readdirSync(run).map((name) => Number(/^vfs-(\d+)\.pid$/.exec(name)?.[1] ?? 0)));
  return Number(readFileSync(join(run, `vfs-${newest}.pid`), "utf8"));
};
// How many descriptors the newest share's virtiofsd holds, its child that serves the share included.
export const descriptors = (run: string) => {
  const daemon = newestDaemon(run);
  const children = spawnSync("pgrep", ["-P", String(daemon)], { encoding: "utf8" }).stdout.trim().split("\n").filter(Boolean).map(Number);
  return [daemon, ...children].reduce((sum, pid) => sum + readdirSync(`/proc/${pid}/fd`).length, 0);
};
export const alive = (pid: number) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};
export const median = (values: number[]) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)] ?? 0;

// The agent disk from the built agent, into *dir*, by *script*.
export function agentDisk(dir: string, script = AGENT_DISK): string {
  const image = join(dir, "agent.img");
  const made = spawnSync(script, [image], { encoding: "utf8" });
  if (made.status !== 0) throw new Error(`agent-disk.sh failed: ${made.error?.message ?? made.stderr}`);
  return image;
}

// The guest's init, as the agent disk carries it.
export const INIT = readFileSync(join(dirname(AGENT_DISK), "init"), "utf8");
// INIT with each [from, to] made, each found.
export const altered = (...changes: Array<[string | RegExp, string]>) => changes.reduce((init, [from, to]) => {
  const made = init.replace(from, to);
  if (made === init) throw new Error(`vm/init has no ${String(from)}`);
  return made;
}, INIT);
// An agent disk as agentDisk() makes, from a folder of its own in *dir*, with *init* for vm/init.
export function agentDiskWith(dir: string, init: string): string {
  const desktop = mkdtempSync(join(dir, "desktop-"));
  mkdirSync(join(desktop, "vm"));
  for (const name of ["agent-disk.sh", "enter-root"]) symlinkSync(join(dirname(AGENT_DISK), name), join(desktop, "vm", name));
  symlinkSync(join(dirname(AGENT_DISK), "..", "dist"), join(desktop, "dist"));
  writeFileSync(join(desktop, "vm", "init"), init, { mode: 0o755 });
  return agentDisk(desktop, join(desktop, "vm", "agent-disk.sh"));
}

// *image*, an agent disk, with *main* in place of the history's way in (vm/history.py): what the
// guest's agent then runs, as its own user, for each request to a folder's history.
export function withHistory(image: string, main: string): string {
  const script = join(dirname(image), "history-main.py");
  writeFileSync(script, main);
  for (const command of ["rm /history/main.py", `write ${script} /history/main.py`]) {
    const done = spawnSync("debugfs", ["-w", image, "-R", command], { encoding: "utf8" });
    if (done.status !== 0) throw new Error(`debugfs failed: ${done.error?.message ?? done.stderr}`);
  }
  return image;
}

const host = userInfo();
export const USER: HostUser = { uid: host.uid, gid: host.gid, name: host.username, home: host.homedir };
