// Start at login on Windows (spec, Section 7): the app's own command as a value of the user's Run key
// (HKCU\Software\Microsoft\Windows\CurrentVersion\Run; Microsoft, "Run and RunOnce Registry Keys"), which Electron's
// login-item settings write under the app's AppUserModelID (app-identity-win32.ts), with the Startup apps switch that
// Task Manager and Settings show for it turned on (Electron, app.setLoginItemSettings, enabled). The setting is
// Windows's: on while Windows would start the app's program at login, the switch on. The command is started with
// LOGIN after it, which says the start was at login: Electron says that of a launch on macOS alone (wasOpenedAtLogin).

import type { LoginItem, LoginSystem } from "./login-item.js";
import type { Places } from "./places.js";

// What the app is started with at login: its window waits for the user.
export const LOGIN = "--login";

/**
 * *arg* as one argument of a Windows command line, as CommandLineToArgvW and the C runtime read one back (Microsoft,
 * "Parsing C++ command-line arguments"): as it is where it holds no space, tab or quote and is not empty; else quoted,
 * a quote in it and the backslashes before one, or before the closing quote, escaped. Electron writes each argument of
 * a Run key's command as it is given (Electron, app.setLoginItemSettings, args: "Take care to wrap paths in quotes").
 */
export function quoted(arg: string): string {
  if (arg !== "" && !/[\s"]/.test(arg)) return arg;
  return `"${arg.replace(/(\\*)"/g, '$1$1\\"').replace(/(\\+)$/, "$1$1")}"`;
}

/** The login item of the app, as *system* (Electron's app) keeps it in the user's Run key. */
export function loginItem(_places: Places, system: LoginSystem): LoginItem {
  return {
    // Electron's executableWillLaunchAtLogin: the program has a Run key's value, and its Startup apps switch is on,
    // whatever arguments it is given. The program is the app's own, installed or a development build's Electron.
    on: () => system.getLoginItemSettings({ path: process.execPath }).executableWillLaunchAtLogin === true,
    // Windows reads the value back by CommandLineToArgvW's rules, which quote any path: none is refused.
    refusal: () => null,
    set: (on, command) => {
      const [path, ...args] = command;
      system.setLoginItemSettings({ openAtLogin: on, path: path!, args: [...args, LOGIN].map(quoted) });
    },
    startedHidden: (argv) => argv.includes(LOGIN),
  };
}
