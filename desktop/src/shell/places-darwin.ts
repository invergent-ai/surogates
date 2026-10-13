// Where the app keeps what it keeps on macOS, in the user's Library as Apple's File System Programming
// Guide places an app's files ("macOS Library Directory Details"): its state in Application Support,
// Electron's own data too, and what it can download again in Caches, each in a folder named after the
// app, in the home its environment names, as Node and every program of the user's take it, and where it
// names none the user's own (Electron's home, which on macOS is the account's whatever HOME says). No
// folder holds an entry that starts it at login, and no launcher starts it: the login item, and what
// starts after an update, is the app itself (login-item-darwin.ts).

import { isAbsolute, join } from "node:path";

import type { Places } from "./places.js";

export const DARWIN = {
  placesFor: (env: NodeJS.ProcessEnv, home: string): Places => {
    const own = env.HOME && isAbsolute(env.HOME) ? env.HOME : home;
    return {
      root: join(own, "Library", "Application Support", "Surogate"),
      cache: join(own, "Library", "Caches", "Surogate"),
      autostartFolder: null,
      launcher: null,
    };
  },
};
