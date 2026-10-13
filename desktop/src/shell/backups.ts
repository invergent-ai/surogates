// What the system's backups leave out of the app's data, by this OS: on macOS the VM's disks, which
// Time Machine would copy every hour (backups-darwin.ts). Linux has no backup of the system's own
// that the app knows of, and leaves its data as it is. Electron-free.

import { forThisOs } from "../platform.js";
import * as darwin from "./backups-darwin.js";

/** *folder*, the VM's under the data root, made and kept out of this OS's backups from now on, with all it holds. */
export const keepOutOfBackups: (folder: string) => Promise<void> = forThisOs<(folder: string) => Promise<void>>({ darwin: (folder) => darwin.keepOutOfTimeMachine(folder) })
  ?? (() => Promise.resolve());
