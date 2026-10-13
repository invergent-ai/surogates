// Where the app keeps what it keeps on Windows: everything in a folder named after the app in the user's local
// application data (Microsoft, Known Folders, FOLDERID_LocalAppData, %LOCALAPPDATA%), Electron's own data too, and its
// cache in that folder's cache. Local, not roaming (FOLDERID_RoamingAppData): a roaming profile is copied to its server
// at every sign-out, and the VM's sessions disk is 32 GiB. The folder is the one the environment names, as Node and
// every program of the user's take it, and where it names none on a drive, the user's own under their profile. No
// folder holds an entry that starts it at login, and no launcher starts it: the login item, and what starts after an
// update, is the app itself (login-item-win32.ts).

import { win32 } from "node:path";

import type { Places } from "./places.js";

// A whole path on a drive: neither relative, nor on a network share, where the VM's disks could not be kept.
const ON_A_DRIVE = /^[A-Za-z]:[\\/]/;

export const WIN32 = {
  placesFor: (env: NodeJS.ProcessEnv, home: string): Places => {
    const named = env.LOCALAPPDATA;
    const local = named && ON_A_DRIVE.test(named) ? win32.normalize(named) : win32.join(home, "AppData", "Local");
    const root = win32.join(local, "Surogate");
    return { root, cache: win32.join(root, "cache"), autostartFolder: null, launcher: null };
  },
};
