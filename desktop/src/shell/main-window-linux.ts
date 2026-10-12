// The main window's controls on Linux, as Claude Desktop draws its own (index.js): the system's own
// minimise, maximise and close over the frameless window's top right corner, in the theme's colours
// (titleBarStyle "hidden" with titleBarOverlay), painted again at each change of theme.

import type { BrowserWindow, TitleBarOverlay } from "electron";

export const windowControls = {
  options: (overlay: TitleBarOverlay) => ({ titleBarStyle: "hidden" as const, titleBarOverlay: overlay }),
  paint: (window: BrowserWindow, overlay: TitleBarOverlay): void => window.setTitleBarOverlay(overlay),
};
