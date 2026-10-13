// Start at login on Windows: the app's own command as a value of the user's Run key, which Electron's login-item
// settings write, named by the app's AppUserModelID; on while Windows would start it at login, as the Run key and its
// Startup apps switch (Task Manager, Settings) say; and the start at login told apart by its own argument.

import { describe, expect, it } from "vitest";

import type { LoginSystem } from "../src/shell/login-item.js";
import { LOGIN, loginItem } from "../src/shell/login-item-win32.js";
import { WIN32 } from "../src/shell/places-win32.js";
import { importedOn } from "./platforms.js";

const PLACES = WIN32.placesFor({ LOCALAPPDATA: "C:\\Users\\Ana\\AppData\\Local" }, "C:\\Users\\Ana");
const INSTALLED = ["C:\\Program Files\\Surogate\\Surogate.exe"];
const DEVELOPMENT = ["C:\\Users\\Ana\\surogates\\desktop\\node_modules\\electron\\dist\\electron.exe", "C:\\Users\\Ana\\my work\\surogates\\desktop\\dist\\shell\\main.js"];

// Electron's login-item settings as Windows keeps them: the Run key's value, and whether its Startup apps switch is on.
function system(): LoginSystem & { set: unknown[]; asked: unknown[]; switchedOff: boolean } {
  let run: { path: string; args: string[] } | null = null;
  const kept = {
    set: [] as unknown[],
    asked: [] as unknown[],
    switchedOff: false,
    getLoginItemSettings: (options?: { path?: string; args?: string[] }) => {
      kept.asked.push(options);
      const willLaunch = run !== null && run.path === options?.path && !kept.switchedOff;
      return { openAtLogin: run !== null, wasOpenedAtLogin: false, executableWillLaunchAtLogin: willLaunch };
    },
    setLoginItemSettings: (settings: { openAtLogin: boolean; path?: string; args?: string[] }) => {
      kept.set.push(settings);
      run = settings.openAtLogin ? { path: settings.path!, args: settings.args! } : null;
    },
  };
  return kept;
}

describe("start at login on Windows", () => {
  it("writes the app's own command to the Run key with its argument for a start at login, or takes it out", () => {
    const kept = system();
    const item = loginItem(PLACES, kept);
    item.set(true, INSTALLED);
    item.set(false, INSTALLED);
    expect(kept.set).toEqual([
      { openAtLogin: true, path: "C:\\Program Files\\Surogate\\Surogate.exe", args: [LOGIN] },
      { openAtLogin: false, path: "C:\\Program Files\\Surogate\\Surogate.exe", args: [LOGIN] },
    ]);
    // A development build's Electron on its main, each argument as it is: Electron quotes it (windows.e2e.ts).
    item.set(true, DEVELOPMENT);
    expect(kept.set.at(-1)).toEqual({ openAtLogin: true, path: DEVELOPMENT[0], args: [DEVELOPMENT[1], LOGIN] });
  });

  it("is on while Windows would start the app's program at login, and off once its Startup apps switch is off", () => {
    const kept = system();
    const item = loginItem(PLACES, kept);
    expect(item.on()).toBe(false);
    item.set(true, [process.execPath]);
    expect(item.on()).toBe(true);
    expect(kept.asked.at(-1)).toEqual({ path: process.execPath });
    kept.switchedOff = true;
    expect(item.on()).toBe(false);
  });

  it("refuses no build, whatever its path holds", () => {
    expect(loginItem(PLACES, system()).refusal(["C:\\Users\\Ana\\100%\\$HOME\\a`b\\Surogate.exe"])).toBeNull();
  });

  it("starts with its window waiting where its arguments hold the one it is started with at login, the first launch or a second", () => {
    const item = loginItem(PLACES, system());
    expect(item.startedHidden(["C:\\Program Files\\Surogate\\Surogate.exe", LOGIN])).toBe(true);
    expect(item.startedHidden(["C:\\Program Files\\Surogate\\Surogate.exe"])).toBe(false);
    // Linux's argument is Linux's.
    expect(item.startedHidden(["C:\\Program Files\\Surogate\\Surogate.exe", "--hidden"])).toBe(false);
  });

  it("is a Windows PC's", async () => {
    const { loginItemFor } = await importedOn("win32", () => import("../src/shell/login-item.js"));
    expect(loginItemFor(PLACES, system()).startedHidden([LOGIN])).toBe(true);
  });
});
