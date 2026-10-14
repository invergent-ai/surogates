// The lock that gives a folder one host at a time, as each OS takes it: one try, which a host repeats while it waits
// (folder-record.ts, lockFolder). The kernel lets go of it when its holder dies, however it dies.

import { forThisOs, notBuilt } from "../platform.js";
import { takeDarwinLock } from "./folder-lock-darwin.js";
import { takeLinuxLock } from "./folder-lock-linux.js";
import { takeWin32Lock } from "./folder-lock-win32.js";

export interface FolderLock {
  close(): void;
}

/**
 * The lock of the folder *dev*:*ino* for the user *uid*, or null while another holds it. macOS keeps its locks in
 * the app's data folder, *dataDir*; Linux's are the kernel's, and Windows's are pipes named by the user's SID: neither
 * takes one.
 */
export type TakeLock = (uid: number, dev: number, ino: number, dataDir?: string) => Promise<FolderLock | null>;

export const takeLock: TakeLock = forThisOs<TakeLock>({ linux: takeLinuxLock, darwin: takeDarwinLock, win32: takeWin32Lock }) ?? (async () => notBuilt("The folder lock"));
