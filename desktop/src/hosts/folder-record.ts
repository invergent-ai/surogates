// What the app knows about a folder a tool host works in, and the lock that gives
// a folder one host at a time. Both are outside every sandbox: the record in the
// app's data, the lock in the kernel (on macOS, a file of the app's data the kernel locks).

import { closeSync, fsyncSync, mkdirSync, openSync, readFileSync, renameSync, writeFileSync } from "node:fs";

import type { ProcessHandle } from "../guest/processes.js";
import { dirname } from "../host-path.js";
import { forThisOs, notBuilt } from "../platform.js";
import { type FolderLock, takeLock } from "./folder-lock.js";

// How long a host waits for another host on the same folder to let it go.
export const LOCK_WAIT_MS = 10_000;

export class FolderBusy extends Error {
  constructor() {
    super("another chat on this computer is working in this folder; this one can use it once that one is done");
  }
}

export interface FolderRecord {
  // running: a host works in the folder; stopped: the last one stopped cleanly.
  state: "running" | "stopped";
  // That host's baseline of the user's own hooks (see HookGuard), once it knew it.
  hooks: Record<string, string> | null;
  // The handles of the root's background processes in the VM, as the guest last told that host,
  // so a later host can say they ended when the app quit.
  processes: ProcessHandle[];
}

// One host per folder, by device and inode however it is spelled: the lock as this OS
// takes it (folder-lock.ts), in the app's data folder *dataDir* where it takes one there,
// tried again until *waitMs* have passed.
export async function lockFolder(dev: number, ino: number, waitMs = LOCK_WAIT_MS, dataDir?: string): Promise<FolderLock> {
  const uid = process.getuid?.() ?? 0;
  const deadline = Date.now() + waitMs;
  for (;;) {
    const lock = await takeLock(uid, dev, ino, dataDir);
    if (lock !== null) return lock;
    if (Date.now() >= deadline) throw new FolderBusy();
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}

export function readRecord(path: string): FolderRecord | null {
  try {
    const value = JSON.parse(readFileSync(path, "utf8")) as FolderRecord;
    const hooks = value.hooks === null || (typeof value.hooks === "object" && !Array.isArray(value.hooks));
    if (!(value.state === "running" || value.state === "stopped") || !hooks) return null;
    // A record written before records kept processes has none.
    const processes = Array.isArray(value.processes) ? value.processes.filter(isHandle) : [];
    // Only what a record holds now: one an earlier build wrote may name srt's placeholders too.
    return { state: value.state, hooks: value.hooks, processes };
  } catch {
    return null;
  }
}

// The longest string a handle holds, in UTF-16 units: a registry keeps 2 000 code
// points of a command, task id and output (guest/processes.ts, HANDLE_CHARS), and a
// folder a command can run in is a path of fewer than 4 096 bytes.
const HANDLE_UNITS = 4_096;
const short = (value: unknown): value is string => typeof value === "string" && value.length <= HANDLE_UNITS;

export function isHandle(value: unknown): value is ProcessHandle {
  const handle = value as ProcessHandle;
  return typeof handle === "object" && handle !== null && short(handle.id) && short(handle.command)
    && short(handle.cwd) && (handle.task_id === null || short(handle.task_id))
    && typeof handle.started_at === "number"
    && (handle.ended === undefined || (typeof handle.ended === "object" && handle.ended !== null
      && (handle.ended.exit_code === null || typeof handle.ended.exit_code === "number")
      && short(handle.ended.output) && (handle.ended.note === null || short(handle.ended.note))));
}

// Whether this OS syncs a folder: Linux and macOS do, by fsync on it; Windows does not, flushing no folder's handle
// (FlushFileBuffers on a folder opened to read fails), and journals each rename on NTFS itself.
const SYNCS_FOLDERS = forThisOs({ linux: true, darwin: true, win32: false });

// Whole or not at all, and on disk before it returns where this OS syncs a folder: a crash right after must find it.
export function writeRecord(path: string, record: FolderRecord): void {
  mkdirSync(dirname(path), { recursive: true });
  const temp = `${path}.${process.pid}`;
  syncFile(temp, "w", (fd) => writeFileSync(fd, JSON.stringify(record)));
  renameSync(temp, path);
  // The rename is on disk only once its folder is.
  if (SYNCS_FOLDERS ?? notBuilt("A folder's record")) syncFile(dirname(path), "r", () => {});
}

function syncFile(path: string, flags: string, write: (fd: number) => void): void {
  const fd = openSync(path, flags, 0o600);
  try {
    write(fd);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}
