// The lock that gives a folder one host at a time, as each OS takes it: one try, which a host repeats while it waits
// (folder-record.ts, lockFolder). The kernel lets go of it when its holder dies, however it dies.

import { notBuilt } from "../platform.js";
import { takeDarwinLock } from "./folder-lock-darwin.js";
import { takeLinuxLock } from "./folder-lock-linux.js";

export interface FolderLock {
  close(): void;
}

/**
 * The lock of the folder *dev*:*ino* for the user *uid*, or null while another holds it. macOS keeps its locks in
 * the app's data folder, *dataDir*; Linux's are the kernel's, and take none.
 */
export type TakeLock = (uid: number, dev: number, ino: number, dataDir?: string) => Promise<FolderLock | null>;

const TAKE: Partial<Record<NodeJS.Platform, TakeLock>> = { linux: takeLinuxLock, darwin: takeDarwinLock };
export const takeLock: TakeLock = TAKE[process.platform] ?? (async () => notBuilt("The folder lock"));
