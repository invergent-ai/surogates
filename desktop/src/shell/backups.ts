// What the system's backups leave out of the app's data, by this OS: on macOS the VM's disks, which
// Time Machine would copy every hour (backups-darwin.ts). Linux has no backup of the system's own
// that the app knows of, and leaves its data as it is. Nor does Windows: File History and OneDrive's
// folder backup take the user's libraries and known folders, and by default neither takes the user's
// local application data, where the app keeps its own (Microsoft, "Back up and restore with File
// History"; "Back up your Documents, Pictures, and Desktop folders with OneDrive"). Another OS refuses,
// before the VM's disks are kept in a backup the app knows nothing of. Electron-free.

import { forThisOs, notBuilt } from "../platform.js";
import * as darwin from "./backups-darwin.js";

const none = (): Promise<void> => Promise.resolve();

/** *folder*, the VM's under the data root, made and kept out of this OS's backups from now on, with all it holds. */
export const keepOutOfBackups: (folder: string) => Promise<void> = forThisOs<(folder: string) => Promise<void>>({
  linux: none, darwin: (folder) => darwin.keepOutOfTimeMachine(folder), win32: none,
}) ?? (async () => notBuilt("Keeping the VM's disks out of this computer's backups"));
