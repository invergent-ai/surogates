// Where the app keeps what it keeps, by this computer's OS: Linux's (places-linux.ts), macOS's (places-darwin.ts) and
// Windows's (places-win32.ts). Electron-free.

import { forThisOs, notBuilt } from "../platform.js";
import { DARWIN } from "./places-darwin.js";
import { LINUX } from "./places-linux.js";
import { WIN32 } from "./places-win32.js";

export interface Places {
  // Everything the app keeps lives under one root, Electron's own data too: the folder guards
  // refuse it as a chat's folder, so no agent reaches a device token through one.
  root: string;
  // The app's own cache folder: the updates it downloads go in it, and no chat's folder may hold it or lie in it.
  cache: string;
  // Where an entry that starts the app at login goes, or null where the OS keeps no such folder.
  autostartFolder: string | null;
  // What starts the installed app, and stays where it is through updates; null where the app itself does.
  launcher: string | null;
}

/** The app's places for the user whose home is *home*, by *env*. */
export const placesFor: (env: NodeJS.ProcessEnv, home: string) => Places = forThisOs({
  linux: LINUX.placesFor, darwin: DARWIN.placesFor, win32: WIN32.placesFor,
})
  ?? (() => notBuilt("Surogate Desktop's data folder"));
