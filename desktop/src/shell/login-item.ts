// Start at login (spec, Section 7), by this computer's OS: Linux's, an entry in the user's
// autostart folder (login-item-linux.ts). Electron-free.

import { notBuilt } from "../platform.js";
import { loginItem } from "./login-item-linux.js";
import type { Places } from "./places.js";

export interface LoginItem {
  /** Whether the app starts at login now. */
  on(): boolean;
  /** Why *command* cannot start the app at login, or null. */
  refusal(command: readonly string[]): string | null;
  /** Start *command* at login from now on, or no longer. */
  set(on: boolean, command: readonly string[]): void;
  /** Whether a launch with *argv* is the start at login: its window waits for the user. */
  startedHidden(argv: readonly string[]): boolean;
}

/** The login item of the app whose places are *places*. */
export const loginItemFor: (places: Places) => LoginItem = process.platform === "linux" ? loginItem : () => notBuilt("Surogate Desktop's start at login");
