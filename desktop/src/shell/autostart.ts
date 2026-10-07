// Start at login (spec, Section 7): an entry in the user's autostart folder, as the XDG
// Autostart specification reads one, which starts the app with its window hidden. Electron's
// login-item API does not support Linux. The entry is the setting: on while it is there.

import { existsSync, mkdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

// What the app is started with at login: its window waits for the user.
export const HIDDEN = "--hidden";
// The installed app's launcher (spec, Section 9): it stays where it is through updates.
export const LAUNCHER = "/usr/local/bin/surogate";

// GNOME looks for the program of an Exec whose path holds a % before it reads %% as one, finds
// none, and starts nothing. An argument's % it reads as written.
export const REFUSED = "This build cannot start at login: GNOME does not start a program whose path holds a %.";

/** Why *command* cannot start at login, or null. */
export const loginRefusal = (command: readonly string[]): string | null => (command[0]?.includes("%") ? REFUSED : null);

/** The entry's place: the autostart folder of *configHome*, the user's XDG config folder. */
export const autostartFile = (configHome: string): string => join(configHome, "autostart", "surogate.desktop");

// One argument of Exec, as the Desktop Entry specification reads it: a percent sign doubled, an
// argument with a reserved character quoted, with ", `, $ and \ escaped in it, then every
// backslash doubled again, since a value's own escapes are read before its quoting.
function argument(arg: string): string {
  if (/[\0-\x1f\x7f]/.test(arg)) throw new Error("A command Surogate starts at login cannot hold a control character");
  const field = arg.replaceAll("%", "%%");
  const quoted = /[\s"'\\><~|&;$*?#()`]/.test(field) ? `"${field.replace(/["`$\\]/g, "\\$&")}"` : field;
  return quoted.replaceAll("\\", "\\\\");
}

/** The entry that starts *command* at login, hidden. */
export function autostartEntry(command: readonly string[]): string {
  const refused = loginRefusal(command);
  if (refused) throw new Error(refused);
  const exec = [...command, HIDDEN].map(argument).join(" ");
  return `[Desktop Entry]\nType=Application\nName=Surogate\nExec=${exec}\nTerminal=false\n`;
}

export const startsAtLogin = (file: string): boolean => existsSync(file);

/** Start *command* at login from now on, or no longer. The entry is written whole or not at all. */
export function setStartAtLogin(file: string, on: boolean, command: readonly string[]): void {
  if (!on) {
    rmSync(file, { force: true });
    return;
  }
  const entry = autostartEntry(command);
  mkdirSync(dirname(file), { recursive: true });
  const next = `${file}.${process.pid}.tmp`;
  writeFileSync(next, entry);
  renameSync(next, file);
}
