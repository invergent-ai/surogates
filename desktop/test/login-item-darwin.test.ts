// Start at login on macOS: the app itself, registered as the user's login item through Electron, which
// macOS keeps; no command of the app's to refuse; and a window that waits only where macOS says this
// launch was the start at login.

import { describe, expect, it } from "vitest";

import { loginItem } from "../src/shell/login-item-darwin.js";
import type { LoginSystem } from "../src/shell/login-item.js";
import { DARWIN } from "../src/shell/places-darwin.js";
import { importedOn } from "./platforms.js";

const PLACES = DARWIN.placesFor({}, "/Users/ana");

// Electron's login-item settings, as a Mac would keep them: *atLogin* is whether this launch was at login.
function system(atLogin: boolean): LoginSystem & { set: unknown[] } {
  let on = false;
  const set: unknown[] = [];
  return {
    set,
    getLoginItemSettings: () => ({ openAtLogin: on, wasOpenedAtLogin: atLogin }),
    setLoginItemSettings: (settings) => {
      set.push(settings);
      on = settings.openAtLogin;
    },
  };
}

describe("start at login on macOS", () => {
  it("registers the app itself, or no longer, and is on while the system says it is", () => {
    const kept = system(false);
    const item = loginItem(PLACES, kept);
    expect(item.on()).toBe(false);
    item.set(true, ["/Applications/Surogate.app/Contents/MacOS/Surogate"]);
    expect(item.on()).toBe(true);
    item.set(false, ["/Applications/Surogate.app/Contents/MacOS/Surogate"]);
    expect(item.on()).toBe(false);
    // Only the setting: no command, no arguments, no hidden start of the app's own.
    expect(kept.set).toEqual([{ openAtLogin: true }, { openAtLogin: false }]);
  });

  it("refuses no build, whatever its path holds", () => {
    expect(loginItem(PLACES, system(false)).refusal(["/Users/ana/100%/$HOME/Surogate.app/Contents/MacOS/Surogate"])).toBeNull();
  });

  it("starts with its window waiting only where the system says the app's own launch was at login, never for a second launch", () => {
    expect(loginItem(PLACES, system(true)).startedHidden(process.argv)).toBe(true);
    expect(loginItem(PLACES, system(false)).startedHidden(process.argv)).toBe(false);
    // A second launch's, as one from a terminal: macOS starts no second launch at login, and --hidden is Linux's.
    expect(loginItem(PLACES, system(true)).startedHidden([...process.argv, "--hidden"])).toBe(false);
  });

  it("is a Mac's", async () => {
    const { loginItemFor } = await importedOn("darwin", () => import("../src/shell/login-item.js"));
    expect(loginItemFor(PLACES, system(true)).refusal(["/a$b"])).toBeNull();
  });
});
