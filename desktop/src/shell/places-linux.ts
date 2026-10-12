// Where the app keeps what it keeps on Linux, by the XDG Base Directory specification: a relative
// XDG_DATA_HOME, XDG_CONFIG_HOME or XDG_CACHE_HOME is ignored, as the specification says (Electron's
// appData takes one as it is), and so is an empty one.

import { join } from "node:path";

import type { Places } from "./places.js";

// The installed app's launcher (spec, Section 9): it stays where it is through updates.
export const LAUNCHER = "/usr/local/bin/surogate";

// The XDG folder *named* in *env*, when it is a whole path, else *home*'s own *fallback*.
const xdg = (env: NodeJS.ProcessEnv, named: string, home: string, ...fallback: string[]): string => {
  const set = env[named];
  return set?.startsWith("/") ? set : join(home, ...fallback);
};

export const LINUX = {
  placesFor: (env: NodeJS.ProcessEnv, home: string): Places => ({
    root: join(xdg(env, "XDG_DATA_HOME", home, ".local", "share"), "surogate"),
    cache: join(xdg(env, "XDG_CACHE_HOME", home, ".cache"), "surogate"),
    // The autostart folder of the user's XDG config folder, as the XDG Autostart specification reads one.
    autostartFolder: join(xdg(env, "XDG_CONFIG_HOME", home, ".config"), "autostart"),
    launcher: LAUNCHER,
  }),
};
