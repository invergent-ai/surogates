// Where the app keeps what it keeps on macOS, in the user's Library as Apple's File System Programming
// Guide places an app's files ("macOS Library Directory Details"): its state in Application Support,
// Electron's own data too, and what it can download again in Caches, each in a folder named after the
// app. No folder holds an entry that starts it at login, and no launcher starts it: the login item, and
// what starts after an update, is the app itself (login-item-darwin.ts).

import { join } from "node:path";

import type { Places } from "./places.js";

export const DARWIN = {
  placesFor: (_env: NodeJS.ProcessEnv, home: string): Places => ({
    root: join(home, "Library", "Application Support", "Surogate"),
    cache: join(home, "Library", "Caches", "Surogate"),
    autostartFolder: null,
    launcher: null,
  }),
};
