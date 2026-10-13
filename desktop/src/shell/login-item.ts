// Start at login (spec, Section 7), by this computer's OS: Linux's, an entry in the user's
// autostart folder (login-item-linux.ts); macOS's, the app itself as the user's login item
// (login-item-darwin.ts); Windows's, the app's command in the user's Run key (login-item-win32.ts).
// Electron-free.

import { forThisOs, notBuilt } from "../platform.js";
import * as darwin from "./login-item-darwin.js";
import * as linux from "./login-item-linux.js";
import * as win32 from "./login-item-win32.js";
import type { Places } from "./places.js";

export interface LoginItem {
  /** Whether the app starts at login now. */
  on(): boolean;
  /** Why *command* cannot start the app at login, or null. */
  refusal(command: readonly string[]): string | null;
  /** Start *command* at login from now on, or no longer. */
  set(on: boolean, command: readonly string[]): void;
  /** Whether a launch with *argv*, the app's own or a second one's, is the start at login: its window waits for the user. */
  startedHidden(argv: readonly string[]): boolean;
}

/** What of Electron's app keeps the login item where the OS keeps it: macOS's and Windows's, the program and its arguments Windows's alone. */
export interface LoginSystem {
  getLoginItemSettings(options?: { path?: string }): { openAtLogin: boolean; wasOpenedAtLogin: boolean; executableWillLaunchAtLogin?: boolean };
  setLoginItemSettings(settings: { openAtLogin: boolean; path?: string; args?: string[] }): void;
}

/** The login item of the app whose places are *places*, which *system*, Electron's app, keeps where the OS does. */
export const loginItemFor: (places: Places, system: LoginSystem) => LoginItem = forThisOs<(places: Places, system: LoginSystem) => LoginItem>({
  linux: linux.loginItem, darwin: darwin.loginItem, win32: win32.loginItem,
}) ?? (() => notBuilt("Surogate Desktop's start at login"));
