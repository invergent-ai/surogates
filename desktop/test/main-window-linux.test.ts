// The main window's controls on Linux: the system's own minimise, maximise and close, drawn over its
// top right corner in the theme's colours, at the window's making and at each change of theme.

import type { BrowserWindow } from "electron";
import { expect, it } from "vitest";

import { chrome } from "../src/shell/appearance.js";
import { windowControls } from "../src/shell/main-window-linux.js";

it("draws the system's controls over a frameless window, in the theme's colours, and paints them again in another's", () => {
  const { overlay } = chrome(true);
  expect(windowControls.options(overlay)).toEqual({ titleBarStyle: "hidden", titleBarOverlay: overlay });
  const painted: unknown[] = [];
  const window = { setTitleBarOverlay: (options: unknown) => painted.push(options) } as unknown as BrowserWindow;
  windowControls.paint(window, chrome(false).overlay);
  expect(painted).toEqual([chrome(false).overlay]);
});

it("drops the menu Linux gives each of the app's other windows", () => {
  const dropped: string[] = [];
  windowControls.dropMenu({ removeMenu: () => dropped.push("menu") } as unknown as BrowserWindow);
  expect(dropped).toEqual(["menu"]);
});
