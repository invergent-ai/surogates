// The lock that gives a folder one host at a time, as each OS takes it: one try, which a host repeats while it waits
// (folder-record.ts, lockFolder). The kernel lets go of it when its holder dies, however it dies.

import { notBuilt } from "../platform.js";
import { takeLinuxLock } from "./folder-lock-linux.js";

export interface FolderLock {
  close(): void;
}

/** The lock of the folder *dev*:*ino* for the user *uid*, or null while another holds it. */
export type TakeLock = (uid: number, dev: number, ino: number) => Promise<FolderLock | null>;

export const takeLock: TakeLock = process.platform === "linux" ? takeLinuxLock : async () => notBuilt("The folder lock");
