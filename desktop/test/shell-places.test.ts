// Where the app keeps what it keeps on Linux: its data, its cache, its entry that starts it at
// login, and the installed app's launcher, by the XDG Base Directory specification, as it always has.

import { join } from "node:path";

import { expect, it } from "vitest";

import { LINUX } from "../src/shell/places-linux.js";

const HOME = "/home/ana";

it("keeps its data, its cache and its entry for login in the XDG folders that are set, and in the home's own where they are not", () => {
  const set = { XDG_DATA_HOME: "/data", XDG_CONFIG_HOME: "/config", XDG_CACHE_HOME: "/cache" };
  // Each of the three set, or not, or set to a relative path, which the specification says to ignore.
  for (const data of [undefined, "/data", "data"]) {
    for (const config of [undefined, "/config", "config"]) {
      for (const cache of [undefined, "/cache", "cache"]) {
        const env = { ...(data ? { XDG_DATA_HOME: data } : {}), ...(config ? { XDG_CONFIG_HOME: config } : {}), ...(cache ? { XDG_CACHE_HOME: cache } : {}) };
        const named = JSON.stringify(env);
        expect(LINUX.placesFor(env, HOME), named).toEqual({
          root: data === set.XDG_DATA_HOME ? "/data/surogate" : join(HOME, ".local", "share", "surogate"),
          cache: cache === set.XDG_CACHE_HOME ? "/cache/surogate" : join(HOME, ".cache", "surogate"),
          autostartFolder: config === set.XDG_CONFIG_HOME ? "/config/autostart" : join(HOME, ".config", "autostart"),
          launcher: "/usr/local/bin/surogate",
        });
      }
    }
  }
  // Set and empty is not set.
  expect(LINUX.placesFor({ XDG_DATA_HOME: "", XDG_CONFIG_HOME: "", XDG_CACHE_HOME: "" }, HOME)).toEqual(LINUX.placesFor({}, HOME));
});
