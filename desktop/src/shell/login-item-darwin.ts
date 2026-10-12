// Start at login on macOS (spec, Section 7): the app itself as the user's login item, through Electron's
// login-item settings, which register the app's own bundle with SMAppService.mainApp on macOS 13 and
// later (Apple, Service Management, SMAppService: mainApp, register(), unregister(), status). The setting
// is the system's: on while the app is registered. No command is written anywhere, so none is refused.

import type { LoginItem, LoginSystem } from "./login-item.js";
import type { Places } from "./places.js";

/** The login item of the app, as *system* (Electron's app) keeps it. */
export function loginItem(_places: Places, system: LoginSystem): LoginItem {
  return {
    on: () => system.getLoginItemSettings().openAtLogin,
    refusal: () => null,
    set: (on) => system.setLoginItemSettings({ openAtLogin: on }),
    // macOS starts a login item with no argument of its own, and never as a second launch: of the app's own
    // launch, the system says whether it was at login (Electron, wasOpenedAtLogin). Unsure: whether it says
    // so for an SMAppService main-app item is not in Apple's documentation, and is checked on a Mac.
    startedHidden: (argv) => argv === process.argv && system.getLoginItemSettings().wasOpenedAtLogin,
  };
}
