// What the app knows about a folder a tool host works in, and the lock that gives
// a folder one host at a time. Both are outside every sandbox: the record in the
// app's data, the lock in the kernel.

import { closeSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, renameSync, rmdirSync, unlinkSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:net";
import { dirname, join } from "node:path";

import type { ProcessHandle } from "./processes.js";

// srt 0.0.77 mounts a placeholder over each of these in the folder while a command
// runs, where it is absent: its dangerous files and folders, and .git's two when
// .git is a folder. .claude is also the empty folder run.ts makes. Children come
// before their parents, so a parent is empty when its turn comes.
export const PLACEHOLDERS = [
  ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile", ".ripgreprc", ".mcp.json",
  ".vscode", ".idea", ".claude/commands", ".claude/agents", ".claude", ".git/hooks", ".git/config",
];

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
  // The PLACEHOLDERS that were there when that host started.
  present: string[];
  // That host's baseline of the user's own hooks (see HookGuard), once it knew it.
  hooks: Record<string, string> | null;
  // The background processes it started, so a later host can say they ended when the app quit.
  processes: ProcessHandle[];
}

// One host per folder, by device and inode however it is spelled: a name in the
// kernel's abstract socket namespace, which one process at a time can hold and the
// kernel frees when that process dies, however it dies. Each sandbox has its own
// network namespace, so no command can take it.
export async function lockFolder(dev: number, ino: number, waitMs = LOCK_WAIT_MS): Promise<Server> {
  const name = `\0surogate-folder-${process.getuid?.() ?? 0}-${dev}-${ino}`;
  const deadline = Date.now() + waitMs;
  for (;;) {
    // Anything in the host's network namespace can connect to the name: drop it at once.
    const server = createServer((socket) => socket.destroy());
    try {
      await new Promise<void>((resolve, reject) => {
        // on, not once: a later error lands on the settled promise, not on the host.
        server.on("error", reject);
        server.listen(name, resolve);
      });
      // It holds the name; it does not keep the host running.
      server.unref();
      return server;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EADDRINUSE") throw error;
      if (Date.now() >= deadline) throw new FolderBusy();
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
}

export function readRecord(path: string): FolderRecord | null {
  try {
    const value = JSON.parse(readFileSync(path, "utf8")) as FolderRecord;
    const hooks = value.hooks === null || (typeof value.hooks === "object" && !Array.isArray(value.hooks));
    if (!(value.state === "running" || value.state === "stopped") || !Array.isArray(value.present) || !hooks) return null;
    // A record written before records kept processes has none.
    const processes = Array.isArray(value.processes) ? value.processes.filter(isHandle) : [];
    return { ...value, processes };
  } catch {
    return null;
  }
}

function isHandle(value: unknown): value is ProcessHandle {
  const handle = value as ProcessHandle;
  return typeof handle === "object" && handle !== null && typeof handle.id === "string" && typeof handle.command === "string"
    && typeof handle.cwd === "string" && (handle.task_id === null || typeof handle.task_id === "string")
    && typeof handle.started_at === "number"
    && (handle.ended === undefined || (typeof handle.ended === "object" && handle.ended !== null
      && (handle.ended.exit_code === null || typeof handle.ended.exit_code === "number")
      && typeof handle.ended.output === "string" && (handle.ended.note === null || typeof handle.ended.note === "string")));
}

// Whole or not at all, and on disk before it returns: a crash right after must find it.
export function writeRecord(path: string, record: FolderRecord): void {
  mkdirSync(dirname(path), { recursive: true });
  const temp = `${path}.${process.pid}`;
  syncFile(temp, "w", (fd) => writeFileSync(fd, JSON.stringify(record)));
  renameSync(temp, path);
  // The rename is on disk only once its folder is.
  syncFile(dirname(path), "r", () => {});
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

export function presentIn(folder: string): string[] {
  return PLACEHOLDERS.filter((name) => {
    try {
      lstatSync(join(folder, name));
      return true;
    } catch {
      return false;
    }
  });
}

// After a host that did not stop cleanly: what srt left over the names that were
// absent when that host started, and only what is provably srt's: an empty file
// with no write bits and one link (srt's mounts are 0444), or an empty folder.
export function removePlaceholders(folder: string, present: readonly string[]): void {
  for (const name of PLACEHOLDERS) {
    if (present.includes(name) || !throughFolder(folder, name)) continue;
    const path = join(folder, name);
    try {
      const stats = lstatSync(path);
      if (stats.isFile() && stats.size === 0 && stats.nlink === 1 && (stats.mode & 0o222) === 0) unlinkSync(path);
      // rmdir refuses a folder that is not empty.
      else if (stats.isDirectory()) rmdirSync(path);
    } catch {
      // Not there, or not empty.
    }
  }
}

// A name under a parent that is not a real folder (a .git a command made a link) lies outside the folder.
function throughFolder(folder: string, name: string): boolean {
  const slash = name.indexOf("/");
  return slash < 0 || lstatSync(join(folder, name.slice(0, slash)), { throwIfNoEntry: false })?.isDirectory() === true;
}
