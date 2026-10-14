// The main window's controls on macOS, as a Mac app with its own title bar draws them: the system's
// close, minimise and zoom buttons inset over the frameless window's top left corner (Electron's
// titleBarStyle "hiddenInset"), centred in the pages' top strip. macOS draws them in its own colours,
// so a change of theme paints nothing. The pages leave them room (pages-preload.cts). And no window
// drops a menu: a Mac app has one menu bar, its own, whichever of its windows is in front.

import type { BrowserWindow, BrowserWindowConstructorOptions, TitleBarOverlay } from "electron";

// Where the buttons' top left corner goes: centred in the pages' 36 px top strip (shell.css, --top), and as
// far from the window's edge as from its top. Unsure: the buttons' size is macOS's, not Apple's documentation.
export const BUTTONS = { x: 13, y: 11 };

export const windowControls = {
  options: (_overlay: TitleBarOverlay): BrowserWindowConstructorOptions => ({ titleBarStyle: "hiddenInset", trafficLightPosition: BUTTONS }),
  paint: (_window: BrowserWindow, _overlay: TitleBarOverlay): void => {},
  dropMenu: (_window: BrowserWindow): void => {},
};
